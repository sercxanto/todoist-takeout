import errno
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_api import FakeApi, archived_answers, completed_answers

from todoist_takeout import sqlite_view, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import ReadOnlyTarget, StorageError
from todoist_takeout.lock import LOCK_FILE, TargetLock
from todoist_takeout.sqlite_view import ACTIVITY, BACKFILL, Completion, read_backup

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
TOKEN = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 10, 18, 10, 0, 5, tzinfo=UTC)
SYNC_RESPONSES = {
    "*": (SYNC_FIXTURES / "full_sync.json").read_bytes(),
    "FULL-1": (SYNC_FIXTURES / "incremental_edit.json").read_bytes(),
}


def build_backup(target: Path) -> Path:
    """A backup of the fixtures, with active data, completed tasks, archived projects and attachments."""
    api = archived_answers(completed_answers(FakeApi(SYNC_RESPONSES), NOW))
    sync.run_backup(target, Client(api, sleep=lambda delay: None), now=lambda: NOW)
    return target


def new_backup(target: Path) -> Path:
    """An empty backup, for hand-made files."""
    storage.write_state(target, storage.new_state())
    return target


def put(backup: Path, path: str, value) -> None:
    file = backup / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(storage.encode_json(value))


def snapshot_of(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()
    }


def stderr_lines(capsys) -> list[str]:
    return capsys.readouterr().err.splitlines()


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)


# --- 1.1 the sqlite command ---------------------------------------------------------------


@pytest.mark.parametrize("argv", [["sqlite"], ["sqlite", "backup"], ["sqlite", "a", "b", "c"]])
def test_missing_or_extra_arguments_exit_with_status_2(tmp_path, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        main([*argv[:1], *[str(tmp_path / name) for name in argv[1:]]])
    assert exc.value.code == 2
    assert "usage: todoist-takeout" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_module_invocation_help():
    result = subprocess.run(
        [sys.executable, "-m", "todoist_takeout", "sqlite", "--help"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 0
    for text in ["BACKUP", "DATABASE", "--quiet", "--verbose"]:
        assert text in result.stdout
    assert "--max-rate-limit-wait" not in result.stdout


def test_runs_without_the_token(tmp_path, capsys, no_token):
    backup = build_backup(tmp_path / "backup")
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert "TODOIST_API_TOKEN" not in capsys.readouterr().err


def test_does_not_read_the_token(tmp_path, monkeypatch):
    backup = new_backup(tmp_path / "backup")
    read = []
    real_get = os.environ.get
    monkeypatch.setattr(os.environ, "get", lambda key, *default: read.append(key) or real_get(key, *default))
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert "TODOIST_API_TOKEN" not in read


# --- 1.2 checking the backup --------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "file", "no state"])
def test_not_a_backup_exits_with_status_2(tmp_path, capsys, kind):
    backup = tmp_path / "backup"
    if kind == "file":
        backup.write_bytes(b"")
    elif kind == "no state":
        backup.mkdir()
        (backup / "notes.txt").write_bytes(b"mine\n")
    before = snapshot_of(tmp_path)
    database = tmp_path / "out" / "todoist.db"
    assert main(["sqlite", str(backup), str(database)]) == 2
    lines = stderr_lines(capsys)
    assert lines[-1].startswith("ERROR ") and str(backup) in lines[-1]
    assert not database.exists() and not database.parent.exists()
    assert snapshot_of(tmp_path) == before


def test_backup_from_a_newer_version_exits_with_status_2(tmp_path, capsys):
    backup = tmp_path / "backup"
    put(backup, "state.json", {"format_version": storage.FORMAT_VERSION + 1, "sync_token": "X"})
    database = tmp_path / "todoist.db"
    assert main(["sqlite", str(backup), str(database)]) == 2
    assert f"format version {storage.FORMAT_VERSION + 1}" in stderr_lines(capsys)[-1]
    assert not database.exists()
    assert sorted(p.name for p in backup.iterdir()) == ["state.json"]


def test_backup_unchanged(tmp_path):
    backup = build_backup(tmp_path / "backup")
    before = snapshot_of(backup)
    assert LOCK_FILE in before and "attachments/6X7rfFVQXwRmcvFp/list.pdf" in before
    assert main(["sqlite", "--quiet", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert snapshot_of(backup) == before


# --- 1.3 the backup lock ------------------------------------------------------------------


def test_backup_in_progress_exits_with_status_1(tmp_path, capsys):
    backup = build_backup(tmp_path / "backup")
    database = tmp_path / "todoist.db"
    with TargetLock(backup):
        assert main(["sqlite", str(backup), str(database)]) == 1
    assert stderr_lines(capsys)[-1].startswith("ERROR another run is in progress")
    assert not database.exists()


def test_backup_run_stops_while_the_database_is_built(tmp_path, capsys, monkeypatch):
    backup = build_backup(tmp_path / "backup")
    before = snapshot_of(backup)
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)
    api = FakeApi(SYNC_RESPONSES)
    results = []
    real_read = sqlite_view.read_backup

    def read_while_a_backup_starts(path):
        results.append(main(["backup", str(backup)], client_factory=lambda token, wait: Client(api)))
        results.append(stderr_lines(capsys)[-1])
        return real_read(path)

    monkeypatch.setattr(sqlite_view, "read_backup", read_while_a_backup_starts)
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert results[0] == 1
    assert results[1].startswith("ERROR another run is in progress")
    assert api.requests == []
    assert snapshot_of(backup) == before


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES])
def test_read_only_backup_is_read_without_the_lock(tmp_path, capsys, monkeypatch, code):
    backup = build_backup(tmp_path / "backup")
    real_open = os.open

    def read_only(path, *args, **kwargs):
        if Path(path).name == LOCK_FILE:
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only)
    read = []
    real_read = sqlite_view.read_backup
    monkeypatch.setattr(sqlite_view, "read_backup", lambda path: read.append(path) or real_read(path))
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 0
    warnings = [line for line in stderr_lines(capsys) if line.startswith("WARNING ")]
    assert len(warnings) == 1
    assert "cannot open lock file" in warnings[0] and "read without the lock" in warnings[0]
    assert read == [backup]


def test_other_lock_failures_still_fail_the_backup_command(tmp_path, monkeypatch):
    # A read-only target is a storage error for the backup command, as before.
    assert issubclass(ReadOnlyTarget, StorageError)
    real_open = os.open

    def failing(path, *args, **kwargs):
        if Path(path).name == LOCK_FILE:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing)
    with pytest.raises(StorageError) as info:
        TargetLock(tmp_path).acquire()
    assert not isinstance(info.value, ReadOnlyTarget)


# --- 2.1 the places of the copies ---------------------------------------------------------


def task(task_id, updated_at="2026-05-01T10:00:00.000000Z", **fields):
    return {"id": task_id, "content": f"task {task_id}", "updated_at": updated_at, **fields}


def project(project_id, updated_at="2026-05-01T10:00:00.000000Z", **fields):
    return {"id": project_id, "name": f"project {project_id}", "updated_at": updated_at, **fields}


def comment(comment_id, **fields):
    return {"id": comment_id, "content": f"comment {comment_id}", **fields}


def test_every_place_is_read(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T1.json", task("T1"))
    put(backup, "completed/2026-05.json", {"items": [task("T2")]})
    put(backup, "archived/A/project.json", project("A", is_archived=True))
    put(
        backup,
        "archived/A/full.json",
        {"tasks": [task("T3")], "sections": [{"id": "S2"}], "subprojects": [project("P2")]},
    )
    put(backup, "archived/A/completed.json", {"items": [task("T4")]})
    put(backup, "archived/A/comments/T3.json", {"results": [comment("C1", item_id="T3")]})
    put(backup, "archived/A/project_comments.json", {"results": [comment("C2", project_id="A")]})
    put(backup, "completed/comments/T2.json", {"results": [comment("C3", item_id="T2")]})
    put(backup, "sync/notes/C4.json", comment("C4", item_id="T1"))
    put(backup, "sync/project_notes/C5.json", comment("C5", project_id="P1"))
    put(backup, "sync/projects/P1.json", project("P1"))
    put(backup, "sync/sections/S1.json", {"id": "S1"})
    put(backup, "sync/labels/L1.json", {"id": "L1", "name": "home"})

    content = read_backup(backup)
    assert list(content.tasks) == ["T1", "T2", "T3", "T4"]
    assert list(content.projects) == ["A", "P1", "P2"]
    assert list(content.sections) == ["S1", "S2"]
    assert list(content.comments) == ["C1", "C2", "C3", "C4", "C5"]
    assert list(content.labels) == ["L1"]
    assert content.tasks["T3"] == task("T3")


def test_temporary_and_hidden_files_are_ignored(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T1.json", task("T1"))
    (backup / "sync" / "items" / ".T2.json.0a1b2c3d.tmp").write_bytes(b'{"id": "T2", "upd')
    put(backup, "sync/items/.T3.json", task("T3"))
    put(backup, "completed/.2026-05.json.0a1b2c3d.tmp", {"items": [task("T4")]})
    put(backup, "archived/.A/full.json", {"tasks": [task("T5")]})
    put(backup, "archived/B/.full.json.0a1b2c3d.tmp", {"tasks": [task("T6")]})
    assert list(read_backup(backup).tasks) == ["T1"]


def test_response_not_applied_yet(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T1.json", task("T1"))
    pending = {"sync_token": "X", "full_sync": False, "items": [task("T1", content="new"), task("T2")]}
    put(backup, "pending/sync-response.json", pending)
    content = read_backup(backup)
    assert content.tasks == {"T1": task("T1")}


def test_backup_built_by_a_run(tmp_path):
    content = read_backup(build_backup(tmp_path))
    assert len(content.tasks) > 0 and len(content.projects) > 0 and len(content.comments) > 0
    assert len(content.completions) > 0


# --- 2.2 the current copy -----------------------------------------------------------------

T1 = "2026-05-01T10:00:00.000000Z"
T2 = "2026-05-02T10:00:00.000000Z"
T3 = "2026-05-03T10:00:00.000000Z"


def test_task_completed_and_archived(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", T1, checked=False))
    put(backup, "completed/2026-05.json", {"items": [task("T", T2, checked=True, content="month")]})
    put(backup, "archived/A/project.json", project("A", T3, is_archived=True))
    put(backup, "archived/A/completed.json", {"items": [task("T", T2, checked=True, content="snapshot")]})
    assert read_backup(backup).tasks == {"T": task("T", T2, checked=True, content="snapshot")}


def test_comment_stored_twice(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "completed/comments/T.json", {"results": [comment("C", item_id="T", content="before")]})
    put(backup, "archived/A/project.json", project("A"))
    put(backup, "archived/A/comments/T.json", {"results": [comment("C", item_id="T", content="snapshot")]})
    put(backup, "sync/notes/C.json", comment("C", item_id="T", content="sync"))
    assert read_backup(backup).comments == {"C": comment("C", item_id="T", content="snapshot")}


@pytest.mark.parametrize("newer", ["2026-05", "2026-07"])
def test_task_in_two_month_files(tmp_path, newer):
    backup = new_backup(tmp_path)
    for month in ["2026-05", "2026-07"]:
        put(backup, f"completed/{month}.json", {"items": [task("T", T2 if month == newer else T1, content=month)]})
    assert read_backup(backup).tasks["T"]["content"] == newer


def test_newer_copy_beats_a_better_place(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", T3, content="sync"))
    put(backup, "archived/A/full.json", {"tasks": [task("T", T2, content="snapshot")]})
    assert read_backup(backup).tasks["T"]["content"] == "sync"


def test_times_are_compared_as_instants(tmp_path):
    backup = new_backup(tmp_path)
    # 09:00 at UTC-2 is 11:00 UTC, later than 10:00 UTC, although its text sorts first.
    put(backup, "sync/items/T.json", task("T", "2026-05-01T09:00:00.000000-02:00", content="sync"))
    put(backup, "completed/2026-05.json", {"items": [task("T", "2026-05-01T10:00:00.000000Z", content="month")]})
    assert read_backup(backup).tasks["T"]["content"] == "sync"


@pytest.mark.parametrize("value", [None, "not a time", 17, "missing"])
def test_copy_without_a_valid_update_time_is_older(tmp_path, value):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", T1, content="sync"))
    month = task("T", value, content="month")
    if value == "missing":
        del month["updated_at"]
    put(backup, "completed/2026-05.json", {"items": [month]})
    assert read_backup(backup).tasks["T"]["content"] == "sync"


@pytest.mark.parametrize(
    ("updated_a", "updated_b", "winner"),
    [(T2, T1, "A"), (T1, T2, "B"), (T1, T1, "B"), (None, T1, "B"), (T1, None, "A")],
)
def test_two_snapshots_of_the_same_task(tmp_path, updated_a, updated_b, winner):
    backup = new_backup(tmp_path)
    for name, updated in [("A", updated_a), ("B", updated_b)]:
        put(backup, f"archived/{name}/project.json", project(name, updated, is_archived=True))
        put(backup, f"archived/{name}/full.json", {"tasks": [task("T", T1, project_id=name)]})
    assert read_backup(backup).tasks["T"]["project_id"] == winner


def test_project_and_section_follow_the_task_rule(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/projects/P.json", project("P", T1, is_archived=False))
    put(backup, "archived/P/project.json", project("P", T1, is_archived=True))
    put(backup, "archived/P/full.json", {"sections": [{"id": "S", "updated_at": T1, "name": "snapshot"}]})
    put(backup, "sync/sections/S.json", {"id": "S", "updated_at": T1, "name": "sync"})
    content = read_backup(backup)
    assert content.projects["P"]["is_archived"] is True
    assert content.sections["S"]["name"] == "snapshot"


def test_project_json_beats_the_subprojects_of_another_snapshot(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "archived/A/project.json", project("A", T1))
    put(backup, "archived/A/full.json", {"subprojects": [project("B", T1, name="listed in A")]})
    put(backup, "archived/B/project.json", project("B", T1, name="own snapshot"))
    assert read_backup(backup).projects["B"]["name"] == "own snapshot"


# --- 2.3 deleted objects ------------------------------------------------------------------


def test_deleted_task(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", T1))
    put(backup, "sync/items/T.deleted.json", task("T", None, content="", is_deleted=True))
    put(backup, "completed/2026-05.json", {"items": [task("T", T2)]})
    assert read_backup(backup).tasks == {}


def test_restored_task(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", T3, content="restored"))
    put(backup, "completed/2026-05.json", {"items": [task("T", T2)]})
    assert read_backup(backup).tasks["T"]["content"] == "restored"


def test_blanked_deletion_payload_is_neither_a_copy_nor_live(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.json", task("T", None, content="", is_deleted=True))
    put(backup, "completed/2026-05.json", {"items": [task("T", T2)]})
    put(backup, "sync/items/U.json", task("U", T1, is_deleted=False))
    assert list(read_backup(backup).tasks) == ["U"]


def test_deleted_project_section_label_and_comments(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/projects/P.json", {"id": "P", "is_deleted": True})
    put(backup, "archived/P/project.json", project("P", T3))
    put(backup, "sync/sections/S.json", {"id": "S", "name": "kept"})
    put(backup, "sync/sections/S.deleted.json", {"id": "S", "name": "", "is_deleted": True})
    put(backup, "archived/P/full.json", {"sections": [{"id": "S", "updated_at": T3}]})
    put(backup, "sync/labels/L.json", {"id": "L", "name": "", "is_deleted": True})
    put(backup, "sync/labels/K.json", {"id": "K", "name": "kept"})
    put(backup, "sync/notes/C.json", comment("C", item_id="T"))
    put(backup, "sync/notes/C.deleted.json", comment("C", content="", is_deleted=True))
    put(backup, "completed/comments/T.json", {"results": [comment("C", item_id="T")]})
    put(backup, "sync/project_notes/D.json", comment("D", project_id="P", is_deleted=True))
    put(backup, "archived/P/project_comments.json", {"results": [comment("D", project_id="P"), comment("E")]})
    content = read_backup(backup)
    assert content.projects == {}
    assert content.sections == {}
    assert list(content.labels) == ["K"]
    assert list(content.comments) == ["E"]


def test_deletion_applies_to_its_own_type_only(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/projects/X.deleted.json", {"id": "X", "is_deleted": True})
    put(backup, "sync/items/X.json", task("X"))
    assert list(read_backup(backup).tasks) == ["X"]


def test_deleted_ids_are_decoded_from_file_names(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "completed/2026-05.json", {"items": [task("a.b"), task("c")]})
    put(backup, f"sync/items/{storage.encode_key('a.b')}.deleted.json", {"id": "a.b", "is_deleted": True})
    put(backup, "sync/items/%zz.deleted.json", {"id": "c", "is_deleted": True})  # not a name the tool writes
    assert list(read_backup(backup).tasks) == ["c"]


# --- 2.4 completions ----------------------------------------------------------------------

LARGE_ID = 152735830295847392018475629384756102938


def event(event_id, task_id, date, event_type="item:completed", content="Water the plants", project_id="P"):
    return {
        "id": event_id,
        "object_type": "item",
        "object_id": task_id,
        "event_type": event_type,
        "event_date": date,
        "parent_project_id": project_id,
        "extra_data": {"content": content, "is_recurring": True},
    }


def test_recurring_task_completed_three_times(tmp_path):
    backup = new_backup(tmp_path)
    events = [event(LARGE_ID + i, "T", f"2026-10-0{i + 1}T08:00:00.000000Z") for i in range(3)]
    put(backup, "completed/activity/2026-10.json", {"results": events})
    completions = read_backup(backup).completions
    assert [c.completed_at for c in completions.values() if c.task_id == "T"] == [
        "2026-10-01T08:00:00.000000Z",
        "2026-10-02T08:00:00.000000Z",
        "2026-10-03T08:00:00.000000Z",
    ]
    assert {c.source for c in completions.values()} == {ACTIVITY}


def test_large_event_id_is_exact(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "completed/activity/2026-10.json", {"results": [event(LARGE_ID, "T", "2026-10-01T08:00:00Z")]})
    assert list(read_backup(backup).completions) == ["152735830295847392018475629384756102938"]


def test_completion_before_t0_and_of_a_deleted_task(tmp_path):
    backup = new_backup(tmp_path)
    put(backup, "sync/items/T.deleted.json", {"id": "T", "is_deleted": True})
    entries = [
        {"id": "b-1", "task_id": "T", "content": "Pay rent", "project_id": "P", "completed_at": "2026-09-01T08:00:00Z"},
        {
            "id": "b-2",
            "task_id": "U",
            "item_object": {"content": "From the task"},
            "completed_at": "2026-09-02T08:00:00Z",
        },
    ]
    put(backup, "completed/backfill/2026-09.json", {"items": entries, "projects": {}, "sections": {}})
    put(backup, "completed/activity/2026-10.json", {"results": [event(1, "T", "2026-10-01T08:00:00Z", content="Pay")]})
    assert read_backup(backup).completions == {
        "1": Completion("1", "T", "Pay", "P", "2026-10-01T08:00:00Z", ACTIVITY),
        "b-1": Completion("b-1", "T", "Pay rent", "P", "2026-09-01T08:00:00Z", BACKFILL),
        "b-2": Completion("b-2", "U", "From the task", None, "2026-09-02T08:00:00Z", BACKFILL),
    }


def test_other_event_types_are_left_out(tmp_path):
    backup = new_backup(tmp_path)
    events = [
        event(1, "T", "2026-10-01T08:00:00Z"),
        event(2, "T", "2026-10-02T08:00:00Z", event_type="item:uncompleted"),
        event(3, "T", "2026-10-03T08:00:00Z", event_type=None),
        {"event_type": "item:completed", "object_id": "T"},  # no id
    ]
    put(backup, "completed/activity/2026-10.json", {"results": events})
    assert list(read_backup(backup).completions) == ["1"]
