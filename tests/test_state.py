import json

import pytest

from todoist_takeout import __version__, storage
from todoist_takeout.errors import ConfigError


def test_read_state_without_file(tmp_path):
    assert storage.read_state(tmp_path) is None


def test_create_state_without_token(tmp_path):
    state = storage.load_or_create_state(tmp_path)
    assert state["format_version"] == storage.FORMAT_VERSION == 1
    assert state["sync_token"] is None
    on_disk = json.loads((tmp_path / "state.json").read_bytes())
    assert on_disk == {
        "format_version": 1,
        "sync_token": None,
        "last_full_sync_utc": None,
        "last_successful_run_utc": None,
        "tool_version": __version__,
    }
    assert (tmp_path / "state.json").read_bytes() == storage.encode_json(on_disk)


def test_load_existing_state_does_not_rewrite_it(tmp_path):
    storage.load_or_create_state(tmp_path)
    path = tmp_path / "state.json"
    before = path.stat().st_mtime_ns
    assert storage.load_or_create_state(tmp_path)["format_version"] == 1
    assert path.stat().st_mtime_ns == before


def test_round_trip_keeps_unknown_fields(tmp_path):
    (tmp_path / "state.json").write_bytes(
        storage.encode_json(
            {
                "format_version": 1,
                "sync_token": "old",
                "future_field": {"added_by": "a newer minor version"},
                "tool_version": "0.2.0",
            }
        )
    )
    state = storage.read_state(tmp_path)
    state["sync_token"] = "new"
    storage.write_state(tmp_path, state)
    reread = json.loads((tmp_path / "state.json").read_bytes())
    assert reread == {
        "format_version": 1,
        "sync_token": "new",
        "future_field": {"added_by": "a newer minor version"},
        "tool_version": __version__,
    }
    assert list(reread) == ["format_version", "sync_token", "future_field", "tool_version"]


def test_write_state_uses_full_sync(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "write_bytes_atomic", lambda path, data, **kw: calls.append((path, kw)))
    storage.write_state(tmp_path, storage.new_state())
    assert calls == [(tmp_path / "state.json", {"full_sync": True})]


def test_newer_format_version_is_rejected(tmp_path):
    content = storage.encode_json({"format_version": 2, "sync_token": "abc"})
    (tmp_path / "state.json").write_bytes(content)
    with pytest.raises(ConfigError, match="format version 2"):
        storage.load_or_create_state(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == content
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b"[]\n",
        b"{}\n",
        b'{"format_version": "1"}\n',
        b'{"format_version": true}\n',
        b'{"format_version": 0}\n',
    ],
)
def test_invalid_state_file_is_a_config_error(tmp_path, content):
    (tmp_path / "state.json").write_bytes(content)
    with pytest.raises(ConfigError, match="state.json"):
        storage.read_state(tmp_path)


# --- the `completed` object -----------------------------------------------------


def test_state_without_completed_object_starts_as_first_run(tmp_path):
    state = storage.load_or_create_state(tmp_path)
    assert "completed" not in state
    assert storage.completed_state(state) == {
        "t0_utc": None,
        "first_month": None,
        "tasks_closed_through": None,
        "backfill_through": None,
        "backfill_status": "in_progress",
        "activity_fetched_utc": None,
    }
    assert "completed" not in state


def test_completed_object_round_trips_with_unknown_fields(tmp_path):
    stored = {
        "t0_utc": "2026-10-18T10:00:05.123456Z",
        "first_month": "2016-03",
        "tasks_closed_through": "2026-09",
        "backfill_through": "2026-10",
        "backfill_status": "complete",
        "activity_fetched_utc": "2026-10-18T10:00:05.123456Z",
        "future_field": [1, 2],
    }
    (tmp_path / "state.json").write_bytes(
        storage.encode_json({"format_version": 1, "sync_token": "t", "completed": stored, "tool_version": "0.1.0"})
    )
    state = storage.read_state(tmp_path)
    assert storage.completed_state(state) == stored

    storage.update_completed_state(tmp_path, state, tasks_closed_through="2026-10")

    reread = storage.read_state(tmp_path)
    assert reread["completed"] == {**stored, "tasks_closed_through": "2026-10"}
    assert list(reread["completed"]) == list(stored)
    assert reread["sync_token"] == "t" and reread["format_version"] == storage.FORMAT_VERSION == 1
    assert state == reread


def test_update_completed_state_creates_the_object(tmp_path):
    state = storage.load_or_create_state(tmp_path)
    storage.update_completed_state(tmp_path, state, t0_utc="2026-10-18T10:00:05.123456Z", first_month="2016-03")
    assert storage.read_state(tmp_path)["completed"] == {
        **storage.new_completed_state(),
        "t0_utc": "2026-10-18T10:00:05.123456Z",
        "first_month": "2016-03",
    }


def test_completed_state_returns_a_copy():
    state = {"format_version": 1, "completed": {"first_month": "2016-03"}}
    storage.completed_state(state)["first_month"] = "2020-01"
    assert state["completed"] == {"first_month": "2016-03"}


@pytest.mark.parametrize("value", [[], "x", 1])
def test_completed_object_must_be_an_object(value):
    with pytest.raises(ConfigError, match="'completed'"):
        storage.completed_state({"format_version": 1, "completed": value})
