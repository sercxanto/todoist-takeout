import json
import os

import pytest

from todoist_takeout import storage
from todoist_takeout.errors import SizeMismatch, StorageError


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
    # F_FULLFSYNC replaces the file's fsync; only when it fails does the file get a plain fsync.
    # The directory is fsynced on POSIX only (design D9).
    assert len(fsyncs) == (1 if fail else 0) + (0 if storage._WINDOWS else 1)
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


def test_temp_name_rule_matches_the_temp_files_that_writes_create(tmp_path, monkeypatch):
    # The download step uses the rule to tell a stored attachment from a leftover temp file.
    created = []
    real = storage._create_temp
    monkeypatch.setattr(storage, "_create_temp", lambda path: created.append(real(path)) or created[-1])
    storage.write_bytes_atomic(tmp_path / "preise.pdf", b"x")
    storage.write_stream_atomic(tmp_path / ".env", [b"y"], None)
    assert len(created) == 2 and all(storage.is_temp_name(tmp.name) for _fd, tmp in created)
    assert not any(storage.is_temp_name(name) for name in ["preise.pdf", ".env", "notes.tmp", ".lock"])


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


def test_remove_file(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes(b"x\n")
    assert storage.remove_file(path) is True
    assert not path.exists()
    assert storage.remove_file(path) is False


def test_remove_file_counts_as_a_write_for_the_fault_hook(tmp_path):
    path = tmp_path / "a.json"
    path.write_bytes(b"x\n")
    storage.set_fault_after(0)
    with pytest.raises(storage.InjectedFault):
        storage.remove_file(path)
    assert path.exists()


def test_remove_file_retries_sharing_violations_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_WINDOWS", True)
    monkeypatch.setattr(storage, "_sleep", lambda delay: None)
    real_unlink = os.unlink
    calls = []

    def unlink(path):
        calls.append(path)
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation")
        real_unlink(path)

    monkeypatch.setattr(storage.os, "unlink", unlink)
    path = tmp_path / "a.json"
    path.write_bytes(b"x\n")
    assert storage.remove_file(path) is True
    assert len(calls) == 3
    assert not path.exists()


# --- Attachment file names (add-attachment-downloads 2.1) ---------------------


@pytest.mark.parametrize(
    ("file_name", "stored"),
    [
        # Spec scenarios of "Attachment file names".
        ("Preise 2024.pdf", "Preise 2024.pdf"),
        ("Rechnung: März/April.pdf", "Rechnung_ März_April.pdf"),
        ("con.txt", "_con.txt"),
        (".env", "_env"),
        ("bericht.pdf.", "bericht.pdf"),
        ("Grün.txt", "Grün.txt"),
        (None, "attachment"),
        ("", "attachment"),
        (42, "attachment"),
        # Further cases of design D8.
        ("CONIN$", "_CONIN$"),
        ("conout$.log", "_conout$.log"),
        ("COM¹", "_COM¹"),
        ("lpt³.txt", "_lpt³.txt"),
        ("con .txt", "_con .txt"),
        ("NUL", "_NUL"),
        ("CONX.txt", "CONX.txt"),
        ("COM10.txt", "COM10.txt"),
        ("...", "attachment"),
        (".", "attachment"),
        ("..x", "_.x"),
        ("a<b>c\"d\\e|f?g*h", "a_b_c_d_e_f_g_h"),
        ("tab\there\nnew\x7f.txt", "tab_here_new_.txt"),
        ("trailing .  . ", "trailing"),
        ("  leading spaces.txt", "  leading spaces.txt"),
        ("a\ud800b.txt", "a_b.txt"),
        ("Größe 🍎.jpg", "Größe 🍎.jpg"),
    ],
)
def test_attachment_file_name(file_name, stored):
    assert storage.attachment_file_name(file_name) == stored


def test_attachment_file_name_is_nfc():
    name = storage.attachment_file_name("Müller.pdf")
    assert name == "Müller.pdf"
    assert "̈" not in name


def test_long_ascii_name_keeps_extension():
    name = storage.attachment_file_name("a" * 176 + ".pdf")
    assert len(name) == 100
    assert name == "a" * 96 + ".pdf"


def test_long_multibyte_name_stays_within_200_bytes():
    name = storage.attachment_file_name("🍎" * 150 + ".pdf")
    encoded = name.encode("utf-8")
    assert len(encoded) <= 200
    assert name.endswith(".pdf")
    assert encoded.decode("utf-8") == name
    assert name == "🍎" * 49 + ".pdf"


def test_long_name_with_three_byte_characters_hits_the_byte_limit_first():
    # 196 bytes are left for the stem: 65 characters of 3 bytes, far below 96 characters.
    name = storage.attachment_file_name("€" * 120 + ".txt")
    assert name == "€" * 65 + ".txt"
    assert len(name.encode("utf-8")) == 199


def test_extension_longer_than_16_characters_counts_as_stem():
    name = storage.attachment_file_name("a" * 90 + "." + "b" * 20)
    assert name == ("a" * 90 + "." + "b" * 20)[:100]
    # 16 characters including the dot are still kept.
    kept = storage.attachment_file_name("a" * 120 + "." + "b" * 15)
    assert kept == "a" * 84 + "." + "b" * 15


def test_cut_stem_loses_trailing_spaces_and_dots():
    name = storage.attachment_file_name("a" * 95 + " . " + "b" * 50 + ".pdf")
    assert name == "a" * 95 + ".pdf"


def test_cut_stem_that_ends_empty_does_not_produce_a_hidden_file():
    assert storage.attachment_file_name(" " * 150 + ".pdf") == "_pdf"


def test_cut_stem_that_becomes_a_device_name_is_prefixed():
    assert storage.attachment_file_name("AUX" + " " * 120 + "x.pdf") == "_AUX.pdf"


def test_short_name_is_not_cut():
    name = "b" * 96 + ".pdf"
    assert storage.attachment_file_name(name) == name


ATTACHMENT_NAMES = [
    "Preise 2024.pdf",
    "Rechnung: März/April.pdf",
    "con.txt",
    "CONIN$",
    "COM¹",
    "con .txt",
    ".env",
    "bericht.pdf.",
    "Grün.txt",
    "...",
    "a<b>c\"d\\e|f?g*h",
    "tab\there\x7f.txt",
    "a\ud800b.txt",
    "a" * 176 + ".pdf",
    "🍎" * 150 + ".pdf",
    "a" * 90 + "." + "b" * 20,
    " " * 150 + ".pdf",
    "AUX" + " " * 120 + "x.pdf",
    None,
]


@pytest.mark.parametrize("file_name", ATTACHMENT_NAMES)
def test_every_attachment_name_can_be_created(tmp_path, file_name):
    name = storage.attachment_file_name(file_name)
    directory = tmp_path / "attachments" / storage.encode_key("6X7rfFVQXwRmcvFp")
    path = directory / name
    storage.write_bytes_atomic(path, b"x")
    assert [p.name for p in directory.iterdir()] == [name]
    assert path.read_bytes() == b"x"


# --- Streaming atomic writes (add-attachment-downloads 2.2) -------------------


def test_write_stream_writes_chunks_in_order(tmp_path):
    path = tmp_path / "attachments" / "N1" / "file.bin"
    chunks = [bytes([i]) * 1000 for i in range(10)]
    assert storage.write_stream_atomic(path, iter(chunks), 10_000) == 10_000
    assert path.read_bytes() == b"".join(chunks)
    assert _temp_files(tmp_path) == []


def test_write_stream_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    assert storage.write_stream_atomic(path, iter([]), 0) == 0
    assert path.read_bytes() == b""


@pytest.mark.parametrize("expected", [20480, 5])
def test_write_stream_size_mismatch_leaves_nothing(tmp_path, expected):
    path = tmp_path / "attachments" / "N1" / "file.bin"
    with pytest.raises(SizeMismatch) as info:
        storage.write_stream_atomic(path, iter([b"x" * 10240]), expected)
    assert (info.value.expected, info.value.actual) == (expected, 10240)
    assert str(expected) in str(info.value) and "10240" in str(info.value)
    assert not path.exists()
    assert _temp_files(tmp_path) == []
    assert list(path.parent.iterdir()) == []


def test_write_stream_without_expected_size_skips_the_check(tmp_path):
    path = tmp_path / "file.bin"
    assert storage.write_stream_atomic(path, iter([b"abc", b"de"]), None) == 5
    assert path.read_bytes() == b"abcde"


class _SourceFailed(Exception):
    pass


def _failing_source():
    yield b"first"
    raise _SourceFailed("connection dropped")


def test_write_stream_source_error_propagates_and_leaves_nothing(tmp_path):
    path = tmp_path / "attachments" / "N1" / "file.bin"
    with pytest.raises(_SourceFailed):
        storage.write_stream_atomic(path, _failing_source(), None)
    assert not path.exists()
    assert _temp_files(tmp_path) == []


def test_write_stream_source_os_error_is_not_a_storage_error(tmp_path):
    def source():
        yield b"x"
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        storage.write_stream_atomic(tmp_path / "file.bin", source(), None)
    assert _temp_files(tmp_path) == []


def test_write_stream_disk_error_is_a_storage_error(tmp_path, monkeypatch):
    def failing_sync(fd, full_sync):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "_sync_file", failing_sync)
    path = tmp_path / "file.bin"
    with pytest.raises(StorageError, match="file.bin"):
        storage.write_stream_atomic(path, iter([b"x"]), 1)
    assert not path.exists()
    assert _temp_files(tmp_path) == []


def test_write_stream_injected_fault_leaves_only_a_temp_file(tmp_path):
    path = tmp_path / "attachments" / "N1" / "file.bin"
    storage.set_fault_after(0)
    with pytest.raises(storage.InjectedFault):
        storage.write_stream_atomic(path, iter([b"abc"]), 3)
    assert not path.exists()
    [temp] = _temp_files(tmp_path)
    assert temp.parent == path.parent
    assert storage.remove_temp_files(tmp_path) == 1
    assert list(path.parent.iterdir()) == []


def test_write_stream_counts_as_one_write_for_the_fault_hook(tmp_path):
    storage.set_fault_after(1)
    storage.write_stream_atomic(tmp_path / "a.bin", iter([b"a"]), 1)
    with pytest.raises(storage.InjectedFault):
        storage.write_bytes_atomic(tmp_path / "b.json", b"{}\n")
