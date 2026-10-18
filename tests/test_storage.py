import json
import os

import pytest

from todoist_takeout import storage
from todoist_takeout.errors import StorageError


@pytest.fixture(autouse=True)
def _disarm_fault_hook():
    yield
    storage.set_fault_after(None)


# --- 2.1 JSON encoding -------------------------------------------------------


def test_encode_json_exact_bytes():
    value = {"id": "6X7", "content": "Größe 🍎", "labels": ["été"], "priority": 1, "due": None}
    expected = (
        "{\n"
        '  "id": "6X7",\n'
        '  "content": "Größe 🍎",\n'
        '  "labels": [\n'
        '    "été"\n'
        "  ],\n"
        '  "priority": 1,\n'
        '  "due": null\n'
        "}\n"
    ).encode("utf-8")
    assert storage.encode_json(value) == expected


def test_encode_json_has_no_bom_and_no_crlf(tmp_path):
    data = storage.encode_json({"a": "Größe 🍎", "b": {"c": [1, 2]}})
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"}\n")
    # Written in binary mode, so Windows does not translate newlines.
    path = tmp_path / "x.json"
    storage.write_bytes_atomic(path, data)
    assert path.read_bytes() == data


def test_encode_json_round_trips():
    value = {"z": 1, "a": [True, False, None], "big": 12345678901234567890, "f": 0.1, "s": "\u0000\t"}
    assert json.loads(storage.encode_json(value)) == value
    assert list(json.loads(storage.encode_json(value))) == ["z", "a", "big", "f", "s"]


def test_encode_json_keeps_lone_surrogate_as_escape():
    value = json.loads('{"s": "a\\ud800b 🍎"}')
    data = storage.encode_json(value)
    assert data == '{\n  "s": "a\\ud800b 🍎"\n}\n'.encode("utf-8")
    assert json.loads(data) == value


# --- 2.2 File names ----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "stem"),
    [
        ("6X7rM8997g3RQmvh", "6X7rM8997g3RQmvh"),
        ("2203306141", "2203306141"),
        ("a_b-c", "a_b-c"),
        ("CON", "%43ON"),
        ("nul", "%6Eul"),
        ("COM1", "%43OM1"),
        ("lpt9", "%6Cpt9"),
        ("CONX", "CONX"),
        ("COM10", "COM10"),
        ("Größe", "Gr%C3%B6%C3%9Fe"),
        ("🍎", "%F0%9F%8D%8E"),
        ("a~b", "a%7Eb"),
        ("a.b", "a%2Eb"),
        ("..", "%2E%2E"),
        ("a b/c\\d:e", "a%20b%2Fc%5Cd%3Ae"),
        ("%41", "%2541"),
    ],
)
def test_encode_component(key, stem):
    assert storage.encode_component(key) == stem
    assert storage.encode_key(key) == stem
    assert storage.decode_key(stem) == (key,)


def test_encoded_names_use_only_portable_characters():
    for key in ["CON", "Größe", "a.b~c", "x/y", "🍎", "\ud800"]:
        stem = storage.encode_key(key)
        assert set(stem) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-%~")
        assert "." not in stem


def test_composite_key():
    stem = storage.encode_key(["project", "6Jf8VQXxpwv56VQ7"])
    assert stem == "project~6Jf8VQXxpwv56VQ7"
    assert storage.decode_key(stem) == ("project", "6Jf8VQXxpwv56VQ7")


def test_composite_key_with_separator_and_dot_inside_components():
    key = ("a~b", "c.d", "CON")
    stem = storage.encode_key(key)
    assert stem == "a%7Eb~c%2Ed~%43ON"
    assert storage.decode_key(stem) == key


def test_composite_key_with_empty_component():
    stem = storage.encode_key(["today", ""])
    assert stem == "today~"
    assert storage.decode_key(stem) == ("today", "")


@pytest.mark.parametrize(
    "key",
    ["%", "%%", "~", "~~", "Größe 🍎", "CON", "con", "Nul", "\ud800", "a" * 200, "é" * 33],
)
def test_round_trip(key):
    assert storage.decode_key(storage.encode_key(key)) == (key,)


def test_empty_key_is_rejected():
    with pytest.raises(StorageError):
        storage.encode_key("")


def test_length_limit():
    assert storage.encode_key("a" * 200) == "a" * 200
    with pytest.raises(StorageError, match="200"):
        storage.encode_key("a" * 201)
    # 34 characters of 2 UTF-8 bytes each encode to 204 characters.
    with pytest.raises(StorageError):
        storage.encode_key("é" * 34)
    with pytest.raises(StorageError):
        storage.encode_key(["a" * 100, "b" * 100])


# --- 2.3 Atomic writes -------------------------------------------------------


def _temp_files(root):
    return sorted(p for p in root.rglob(".*.tmp"))


def test_write_creates_file_and_parent_directories(tmp_path):
    path = tmp_path / "sync" / "items" / "a.json"
    assert storage.write_bytes_atomic(path, b"{}\n") is True
    assert path.read_bytes() == b"{}\n"
    assert _temp_files(tmp_path) == []


def test_write_replaces_existing_file(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes(b"old\n")
    assert storage.write_bytes_atomic(path, b"new\n") is True
    assert path.read_bytes() == b"new\n"
    assert _temp_files(tmp_path) == []


def test_unchanged_file_keeps_mtime(tmp_path):
    path = tmp_path / "a.json"
    storage.write_bytes_atomic(path, b"same\n")
    os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    assert storage.write_bytes_atomic(path, b"same\n") is False
    assert path.stat().st_mtime_ns == 1_000_000_000_000_000_000


def test_failure_before_replace_keeps_old_content(tmp_path, monkeypatch):
    path = tmp_path / "a.json"
    path.write_bytes(b"old\n")

    def failing_sync(fd, full_sync):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "_sync_file", failing_sync)
    with pytest.raises(StorageError, match="a.json"):
        storage.write_bytes_atomic(path, b"new\n")
    assert path.read_bytes() == b"old\n"
    assert _temp_files(tmp_path) == []


def test_failed_replace_keeps_old_content(tmp_path, monkeypatch):
    path = tmp_path / "a.json"
    path.write_bytes(b"old\n")

    def failing_replace(src, dst):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(storage.os, "replace", failing_replace)
    with pytest.raises(StorageError):
        storage.write_bytes_atomic(path, b"new\n")
    assert path.read_bytes() == b"old\n"
    assert _temp_files(tmp_path) == []


def _flaky_replace(monkeypatch, failures):
    real_replace = os.replace
    calls = []

    def replace(src, dst):
        calls.append(src)
        if len(calls) <= failures:
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", replace)
    return calls


def test_windows_sharing_violation_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_WINDOWS", True)
    sleeps = []
    monkeypatch.setattr(storage, "_sleep", sleeps.append)
    calls = _flaky_replace(monkeypatch, failures=2)
    path = tmp_path / "a.json"
    path.write_bytes(b"old\n")
    assert storage.write_bytes_atomic(path, b"new\n") is True
    assert path.read_bytes() == b"new\n"
    assert len(calls) == 3
    assert sleeps == [0.05, 0.1]
    assert _temp_files(tmp_path) == []


def test_windows_sharing_violation_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_WINDOWS", True)
    sleeps = []
    monkeypatch.setattr(storage, "_sleep", sleeps.append)
    calls = _flaky_replace(monkeypatch, failures=100)
    path = tmp_path / "a.json"
    path.write_bytes(b"old\n")
    with pytest.raises(StorageError):
        storage.write_bytes_atomic(path, b"new\n")
    assert len(calls) == 11
    assert max(sleeps) == 0.5
    assert path.read_bytes() == b"old\n"
    assert _temp_files(tmp_path) == []


def test_permission_error_is_not_retried_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_WINDOWS", False)
    calls = _flaky_replace(monkeypatch, failures=1)
    with pytest.raises(StorageError):
        storage.write_bytes_atomic(tmp_path / "a.json", b"new\n")
    assert len(calls) == 1


class _FakeFcntl:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def fcntl(self, fd, op):
        self.calls.append(op)
        if self.fail:
            raise OSError(45, "Operation not supported")


@pytest.mark.parametrize("fail", [False, True])
def test_full_sync_uses_f_fullfsync_where_available(tmp_path, monkeypatch, fail):
    fake = _FakeFcntl(fail=fail)
    fsyncs = []
    monkeypatch.setattr(storage, "fcntl", fake)
    monkeypatch.setattr(storage, "_F_FULLFSYNC", 51)
    real_fsync = os.fsync
    monkeypatch.setattr(storage.os, "fsync", lambda fd: (fsyncs.append(fd), real_fsync(fd)))

    storage.write_bytes_atomic(tmp_path / "plain.json", b"1\n")
    assert fake.calls == []

    fsyncs.clear()
    storage.write_bytes_atomic(tmp_path / "state.json", b"2\n", full_sync=True)
    assert fake.calls == [51]
    # Without F_FULLFSYNC support only the directory is fsynced; with the fallback the file is too.
    assert len(fsyncs) == (2 if fail else 1)
    assert (tmp_path / "state.json").read_bytes() == b"2\n"


def test_remove_temp_files(tmp_path):
    (tmp_path / "sync" / "items").mkdir(parents=True)
    leftovers = [
        tmp_path / ".state.json.0a1b2c3d.tmp",
        tmp_path / "sync" / "items" / ".6X7.json.deadbeef.tmp",
    ]
    keep = [
        tmp_path / ".lock",
        tmp_path / "sync" / "items" / "6X7.json",
        tmp_path / "notes.tmp",
    ]
    for path in leftovers + keep:
        path.write_bytes(b"x")
    assert storage.remove_temp_files(tmp_path) == 2
    assert not any(p.exists() for p in leftovers)
    assert all(p.exists() for p in keep)


# --- 2.4 Fault injection -----------------------------------------------------


def test_fault_hook_is_inactive_by_default(tmp_path):
    assert storage._fault_after is None
    for i in range(20):
        storage.write_bytes_atomic(tmp_path / f"{i}.json", b"x\n")


def test_fault_hook_fires_after_configured_writes(tmp_path):
    storage.set_fault_after(2)
    storage.write_bytes_atomic(tmp_path / "1.json", b"x\n")
    # An unchanged file is not a write and does not count.
    storage.write_bytes_atomic(tmp_path / "1.json", b"x\n")
    storage.write_bytes_atomic(tmp_path / "2.json", b"x\n")
    with pytest.raises(storage.InjectedFault):
        storage.write_bytes_atomic(tmp_path / "3.json", b"x\n")
    assert not (tmp_path / "3.json").exists()
    # The simulated crash leaves the temp file behind, like a killed process.
    assert len(_temp_files(tmp_path)) == 1
    # The hook fires once.
    storage.write_bytes_atomic(tmp_path / "3.json", b"x\n")
    assert (tmp_path / "3.json").read_bytes() == b"x\n"


def test_fault_hook_is_not_an_exception_subclass():
    assert not issubclass(storage.InjectedFault, Exception)
