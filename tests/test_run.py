import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todoist_takeout import storage, sync
from todoist_takeout.api import Client
from todoist_takeout.errors import ConfigError, RunError
from todoist_takeout.sync import format_description, run_backup

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
OLD = 1_000_000_000
PENDING = "pending/sync-response.json"


def fixture_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def unchanged(token):
    return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()


# The account as the fixtures describe it: a full sync returns FULL-1, the follow-up incremental
# sync returns one edited task and INC-1, and nothing changes after that.
ACCOUNT = {
    "*": fixture_bytes("full_sync"),
    "FULL-1": fixture_bytes("incremental_edit"),
    "INC-1": unchanged("INC-1"),
}


class FixtureTransport:
    """Answers each sync request with the response stored for its sync token."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def send(self, method, path, form):
        assert (method, path) == ("POST", "sync")
        token = form["sync_token"]
        self.requests.append(token)
        return 200, {}, self.responses[token]


def _no_sleep(delay):
    raise AssertionError("the run should not wait")


def run(target, responses=ACCOUNT, now=NOW):
    transport = FixtureTransport(responses)
    stats = run_backup(target, Client(transport, sleep=_no_sleep), now=lambda: now)
    return transport.requests, stats


def tree(target):
    return {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}


def snapshot(target):
    return {path: (target / path).read_bytes() for path in tree(target)}


def state(target):
    return json.loads((target / "state.json").read_bytes())


@pytest.fixture(autouse=True)
def _disarm_fault_hook():
    yield
    storage.set_fault_after(None)


# --- 5.1 run sequence ----------------------------------------------------------


def test_first_run_makes_a_full_and_a_follow_up_request(tmp_path):
    requests, stats = run(tmp_path)
    assert requests == ["*", "FULL-1"]
    assert state(tmp_path)["sync_token"] == "INC-1"
    assert state(tmp_path)["last_full_sync_utc"] == "2026-10-18T10:00:00Z"
    assert state(tmp_path)["last_successful_run_utc"] == "2026-10-18T12:00:00Z"
    assert (tmp_path / "pending").is_dir()
    assert list((tmp_path / "pending").iterdir()) == []
    assert {"state.json", "README.md", ".lock", "sync/items/6X7rM8997g3RQmvh.json"} <= tree(tmp_path)
    edited = json.loads(fixture_bytes("incremental_edit"))["items"][0]
    assert json.loads((tmp_path / "sync/items/6X7rM8997g3RQmvh.json").read_bytes()) == edited
    assert stats.written == 29  # 28 objects of the full sync, then the edited task


def test_second_run_makes_one_incremental_request(tmp_path):
    run(tmp_path)
    requests, _ = run(tmp_path)
    assert requests == ["INC-1"]
    assert list((tmp_path / "pending").iterdir()) == []


def test_server_initiated_full_sync_gets_a_follow_up_request(tmp_path):
    run(tmp_path)
    account = {"INC-1": fixture_bytes("server_full_sync"), "FULL-2": unchanged("FULL-2")}
    requests, stats = run(tmp_path, account)
    assert requests == ["INC-1", "FULL-2"]
    assert stats.missing == 3
    assert state(tmp_path)["sync_token"] == "FULL-2"
    assert state(tmp_path)["last_full_sync_utc"] == "2026-10-20T10:00:00Z"


def test_rounds_are_limited_when_the_server_keeps_sending_full_syncs(tmp_path):
    full = fixture_bytes("full_sync")
    requests, _ = run(tmp_path, {"*": full, "FULL-1": full})
    assert requests == ["*", "FULL-1"]
    assert state(tmp_path)["sync_token"] == "FULL-1"


def test_leftover_pending_response_is_applied_before_requesting(tmp_path):
    run(tmp_path, {"*": fixture_bytes("full_sync"), "FULL-1": unchanged("FULL-1")})
    # A run that downloaded the incremental edit and was killed before applying it.
    storage.write_bytes_atomic(tmp_path / PENDING, fixture_bytes("incremental_edit"))
    requests, stats = run(tmp_path)
    assert requests == ["INC-1"]
    assert stats.written == 1
    assert not (tmp_path / PENDING).exists()


def test_pending_file_holds_the_body_as_received(tmp_path, monkeypatch):
    saved = []
    real_apply = sync.apply_response

    def apply_response(target, response, classifier):
        saved.append((target / PENDING).read_bytes())
        return real_apply(target, response, classifier)

    monkeypatch.setattr(sync, "apply_response", apply_response)
    run(tmp_path)
    assert saved == [ACCOUNT["*"], ACCOUNT["FULL-1"]]


def test_each_checkpoint_follows_its_data_and_state_is_written_last(tmp_path, monkeypatch):
    operations = []
    real_write, real_remove = storage.write_bytes_atomic, storage.remove_file

    def write(path, data, **kwargs):
        if real_write(path, data, **kwargs):
            operations.append(("write", path.relative_to(tmp_path).as_posix()))
            return True
        return False

    def remove(path):
        operations.append(("remove", path.relative_to(tmp_path).as_posix()))
        return real_remove(path)

    monkeypatch.setattr(storage, "write_bytes_atomic", write)
    monkeypatch.setattr(storage, "remove_file", remove)
    run(tmp_path)

    assert operations[0] == ("write", "state.json")  # before any other file
    assert operations[-1] == ("write", "state.json")
    removals = [i for i, op in enumerate(operations) if op == ("remove", PENDING)]
    assert len(removals) == 2
    for i in removals:
        assert operations[i - 1] == ("write", "state.json")
    # README.md is written with the first response, before its checkpoint, and unchanged after that.
    assert operations.index(("write", PENDING)) < operations.index(("write", "README.md")) < removals[0]


def test_invalid_response_is_discarded_without_changing_the_backup(tmp_path):
    run(tmp_path)
    before = snapshot(tmp_path)
    with pytest.raises(RunError, match="not valid JSON"):
        run(tmp_path, {"INC-1": b"<html>Bad gateway</html>"})
    assert snapshot(tmp_path) == before
    requests, _ = run(tmp_path)
    assert requests == ["INC-1"]


def test_response_without_sync_token_is_rejected(tmp_path):
    with pytest.raises(RunError, match="no sync_token"):
        run(tmp_path, {"*": b'{"items": []}'})
    assert state(tmp_path)["sync_token"] is None
    assert not (tmp_path / PENDING).exists()


def test_new_target_and_parents_are_created(tmp_path):
    target = tmp_path / "a" / "b" / "backup"
    run(target)
    assert state(target)["sync_token"] == "INC-1"


@pytest.mark.parametrize("leftover", [".lock", ".state.json.1a2b3c4d.tmp"])
def test_directory_left_by_an_interrupted_first_run_is_accepted(tmp_path, leftover):
    (tmp_path / leftover).write_bytes(b"")
    requests, _ = run(tmp_path)
    assert requests == ["*", "FULL-1"]
    assert not (tmp_path / ".state.json.1a2b3c4d.tmp").exists()


def test_foreign_directory_is_rejected_unchanged(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"mine\n")
    with pytest.raises(ConfigError, match="not a todoist-takeout backup"):
        run(tmp_path, {})
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


def test_file_target_is_rejected(tmp_path):
    (tmp_path / "file").write_bytes(b"")
    with pytest.raises(ConfigError, match="not a directory"):
        run(tmp_path / "file", {})


def test_newer_format_is_rejected_unchanged(tmp_path):
    content = storage.encode_json({"format_version": 2, "sync_token": "X"})
    (tmp_path / "state.json").write_bytes(content)
    with pytest.raises(ConfigError, match="format version 2"):
        run(tmp_path, {})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# --- 5.2 format description in the backup ----------------------------------------


def test_completed_backup_holds_the_format_description(tmp_path):
    run(tmp_path)
    readme = (tmp_path / "README.md").read_bytes()
    assert readme == format_description()
    assert b"\r\n" not in readme
    assert f"Format version: {state(tmp_path)['format_version']}\n".encode() in readme


def test_unchanged_format_description_is_not_rewritten(tmp_path):
    run(tmp_path)
    os.utime(tmp_path / "README.md", ns=(OLD, OLD))
    run(tmp_path)
    assert (tmp_path / "README.md").stat().st_mtime_ns == OLD


# --- 5.3 plan limits --------------------------------------------------------------


def _plan_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "plan" in r.getMessage()]


def test_plan_without_comments_warns_and_the_run_succeeds(tmp_path, caplog):
    run(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        requests, _ = run(tmp_path, {"INC-1": fixture_bytes("plan_limits_no_comments"), "INC-5": unchanged("INC-5")})
    assert requests == ["INC-1"]
    comments = [m for m in _plan_warnings(caplog) if "comments" in m]
    assert comments == [
        "the Todoist plan 'free' does not include comments; task and project comments cannot be backed up "
        "on this plan"
    ]
    assert state(tmp_path)["sync_token"] == "INC-5"
    assert state(tmp_path)["last_successful_run_utc"] is not None


def test_each_disabled_feature_warns_once(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        run(tmp_path)
    # The full-sync fixture's free plan includes comments but not reminders.
    assert _plan_warnings(caplog) == [
        "the Todoist plan 'free' does not include reminders; reminders cannot be backed up on this plan"
    ]


def test_full_plan_or_missing_plan_information_does_not_warn(tmp_path, caplog):
    full = json.loads(fixture_bytes("full_sync"))
    full["user_plan_limits"]["current"].update(comments=True, reminders=True)
    without = json.loads(fixture_bytes("full_sync"))
    del without["user_plan_limits"]
    for i, response in enumerate([full, without]):
        with caplog.at_level(logging.WARNING):
            run(tmp_path / str(i), {"*": json.dumps(response).encode(), "FULL-1": unchanged("FULL-1")})
    assert _plan_warnings(caplog) == []


# --- 5.4 crash and resume ---------------------------------------------------------

# Writes and removals of an uninterrupted first run: state.json, then for the full sync the
# pending response, 28 objects, README.md, state.json and the pending removal, then for the
# follow-up the pending response, the edited task, state.json and the pending removal, and finally
# state.json.
FIRST_RUN_WRITES = 38
# For each request of the first run, the number of writes after which its response is saved in
# `pending/`. From then on, it must never be requested again.
RESPONSE_SAVED_AFTER = {"*": 2, "FULL-1": 34}


@pytest.fixture(scope="module")
def reference_tree(tmp_path_factory):
    target = tmp_path_factory.mktemp("reference")
    run(target)
    return snapshot(target)


def crash(target, fault_after):
    storage.set_fault_after(fault_after)
    with pytest.raises(storage.InjectedFault):
        run(target)
    storage.set_fault_after(None)


def test_crash_points_cover_the_whole_first_run(tmp_path):
    crash(tmp_path / "last", FIRST_RUN_WRITES - 1)
    storage.set_fault_after(FIRST_RUN_WRITES)
    run(tmp_path / "beyond")
    assert storage._fault_after == 0  # armed, never reached


@pytest.mark.parametrize("fault_after", range(FIRST_RUN_WRITES))
def test_resume_after_a_crash_gives_the_uninterrupted_result(tmp_path, reference_tree, fault_after):
    crash(tmp_path, fault_after)

    requests, _ = run(tmp_path)

    assert snapshot(tmp_path) == reference_tree
    for token, saved_after in RESPONSE_SAVED_AFTER.items():
        assert (token in requests) == (fault_after < saved_after), token


def test_saved_full_response_is_not_requested_again(tmp_path, reference_tree):
    crash(tmp_path, RESPONSE_SAVED_AFTER["*"])  # state.json and the pending response are written
    assert (tmp_path / PENDING).read_bytes() == ACCOUNT["*"]
    assert state(tmp_path)["sync_token"] is None

    requests, _ = run(tmp_path)

    assert requests == ["FULL-1"]
    assert snapshot(tmp_path) == reference_tree


def test_crash_while_writing_objects_keeps_the_previous_checkpoint(tmp_path):
    run(tmp_path, {"*": fixture_bytes("full_sync"), "FULL-1": unchanged("FULL-1")})
    storage.set_fault_after(2)  # the pending response and one object, then the crash
    with pytest.raises(storage.InjectedFault):
        run(tmp_path, {"FULL-1": fixture_bytes("delete_full")})
    assert state(tmp_path)["sync_token"] == "FULL-1"


# --- 5.5 idempotence --------------------------------------------------------------


def test_repeat_run_changes_only_the_state_file(tmp_path):
    run(tmp_path)
    for path in tree(tmp_path):
        os.utime(tmp_path / path, ns=(OLD, OLD))
    before = snapshot(tmp_path)
    before_state = state(tmp_path)

    run(tmp_path, now=NOW + timedelta(hours=1))

    after = snapshot(tmp_path)
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert changed == {"state.json"}
    touched = {path for path in tree(tmp_path) if (tmp_path / path).stat().st_mtime_ns != OLD}
    assert touched == {"state.json"}
    assert state(tmp_path) == {**before_state, "last_successful_run_utc": "2026-10-18T13:00:00Z"}
