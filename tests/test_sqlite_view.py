import asyncio
import errno
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fake_api import FakeApi, archived_answers, completed_answers

from todoist_takeout import sqlite_view, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import ReadOnlyTarget, StorageError
from todoist_takeout.lock import LOCK_FILE, TargetLock
from todoist_takeout.sqlite_view import (
    ACTIVITY,
    APPLICATION_ID,
    BACKFILL,
    SCHEMA_VERSION,
    TABLES,
    VIEWS,
    Completion,
    read_backup,
)

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


# --- 3.1 the database target --------------------------------------------------------------

TABLE_NAMES = ["comments", "completions", "labels", "projects", "sections", "task_labels", "tasks"]
VIEW_NAMES = [view.name for view in VIEWS]


def query(database: Path, sql: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(database)) as con:
        return con.execute(sql, params).fetchall()


def dump(database: Path) -> list[str]:
    with closing(sqlite3.connect(database)) as con:
        return list(con.iterdump())


def tables_of(database: Path) -> list[str]:
    """The tables, without SQLite's own and the full-text search tables."""
    sql = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'"
    return sorted(name for (name,) in query(database, sql))


def build_db(backup: Path, database: Path) -> Path:
    assert main(["sqlite", "--quiet", str(backup), str(database)]) == 0
    return database


def test_new_database_and_its_parents(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "a" / "b" / "todoist.db")
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    assert tables_of(database) == TABLE_NAMES


def test_empty_file_is_used(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = tmp_path / "todoist.db"
    database.write_bytes(b"")
    build_db(backup, database)
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    assert query(database, "PRAGMA auto_vacuum") == [(1,)]


def test_database_built_before_is_rebuilt(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    put(backup, "sync/items/T2.json", task("T2"))
    build_db(backup, database)
    assert query(database, "SELECT id FROM tasks") == [("T1",), ("T2",)]


def foreign_sqlite(path: Path, application_id: int = 0) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, mine TEXT)")
        con.execute("INSERT INTO tasks VALUES ('T1', 'keep me')")
        con.execute(f"PRAGMA application_id = {application_id}")
        con.commit()


@pytest.mark.parametrize("kind", ["foreign database", "other application id", "text", "short header", "directory"])
def test_other_files_are_left_unchanged(tmp_path, capsys, monkeypatch, kind):
    backup = new_backup(tmp_path / "backup")
    database = tmp_path / "out" / "todoist.db"
    database.parent.mkdir()
    if kind == "foreign database":
        foreign_sqlite(database)
    elif kind == "other application id":
        foreign_sqlite(database, APPLICATION_ID + 1)
    elif kind == "text":
        database.write_bytes(b"my notes\n")
    elif kind == "short header":
        database.write_bytes(b"SQLite format 3\x00\x10\x00")
    else:
        database.mkdir()
        (database / "inside.txt").write_bytes(b"mine\n")
    before = snapshot_of(database.parent)
    connects = []
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connects.append(args))
    assert main(["sqlite", str(backup), str(database)]) == 2
    last = stderr_lines(capsys)[-1]
    assert last.startswith("ERROR ") and str(database) in last
    assert snapshot_of(database.parent) == before
    assert sorted(p.name for p in database.parent.iterdir()) == ["todoist.db"]
    assert connects == []


# --- 3.2 the schema and the rows ----------------------------------------------------------


def test_only_resources_for_display(tmp_path):
    backup = build_backup(tmp_path / "backup")
    reminder = "6X7Vfq5rqPMM5j5q"
    assert (backup / "sync" / "reminders" / f"{reminder}.json").is_file()
    database = build_db(backup, tmp_path / "todoist.db")
    assert tables_of(database) == TABLE_NAMES
    assert [t.name for t in TABLES] == [
        "projects",
        "sections",
        "labels",
        "tasks",
        "task_labels",
        "comments",
        "completions",
    ]
    for name in TABLE_NAMES:
        columns = [row[1] for row in query(database, f"PRAGMA table_info({name})")]
        assert not any("reminder" in column for column in columns)
        for row in query(database, f"SELECT * FROM {name}"):
            assert reminder not in repr(row)
    assert query(database, "PRAGMA application_id") == [(APPLICATION_ID,)]
    assert query(database, "PRAGMA user_version") == [(SCHEMA_VERSION,)]


EXPECTED_KEYS = {
    "projects": ["id"],
    "sections": ["id"],
    "labels": ["id"],
    "tasks": ["id"],
    "task_labels": ["task_id", "label_name"],
    "comments": ["id"],
    "completions": ["id"],
}
EXPECTED_FOREIGN_KEYS = {
    "projects": {("parent_id", "projects")},
    "sections": {("project_id", "projects")},
    "labels": set(),
    "tasks": {("project_id", "projects"), ("section_id", "sections"), ("parent_id", "tasks")},
    "task_labels": {("task_id", "tasks"), ("label_id", "labels")},
    "comments": {("task_id", "tasks"), ("project_id", "projects")},
    "completions": {("task_id", "tasks"), ("project_id", "projects")},
}


@pytest.mark.parametrize("table", TABLE_NAMES)
def test_keys_and_links_between_tables(tmp_path, table):
    database = build_db(new_backup(tmp_path / "backup"), tmp_path / "todoist.db")
    info = query(database, f"PRAGMA table_info({table})")
    assert [name for _cid, name, _type, _notnull, _default, pk in sorted(info, key=lambda r: r[5]) if pk] == (
        EXPECTED_KEYS[table]
    )
    foreign = query(database, f"PRAGMA foreign_key_list({table})")
    assert {(row[3], row[2]) for row in foreign} == EXPECTED_FOREIGN_KEYS[table]
    assert {row[4] for row in foreign} <= {"id"}
    assert query(database, "PRAGMA foreign_keys") == [(0,)]


def test_task_of_a_deleted_project_keeps_its_id(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/projects/P9.deleted.json", {"id": "P9", "is_deleted": True})
    put(backup, "sync/items/T.json", task("T", project_id="P9", section_id="S9", parent_id="T9"))
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT project_id, section_id, parent_id FROM tasks") == [("P9", "S9", "T9")]
    assert query(database, "SELECT count(*) FROM projects") == [(0,)]


def test_ids_are_stored_exactly_as_text(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/labels/L.json", {"id": 12345678901234567890123, "name": "big"})
    put(backup, "sync/items/T.json", task("0042", project_id=7))
    put(backup, "completed/activity/2026-10.json", {"results": [event(LARGE_ID, "0042", "2026-10-01T08:00:00Z")]})
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT id, typeof(id) FROM labels") == [("12345678901234567890123", "text")]
    assert query(database, "SELECT id, typeof(id), project_id, typeof(project_id) FROM tasks") == [
        ("0042", "text", "7", "text")
    ]
    assert query(database, "SELECT id, typeof(id), task_id FROM completions") == [(str(LARGE_ID), "text", "0042")]


def test_projects_and_sections(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "archived/P1/project.json", project("P1", is_archived=True, color="red", created_at=T1))
    put(
        backup,
        "archived/P1/full.json",
        {
            "subprojects": [project("P2", parent_id="P1", is_archived=True)],
            "sections": [{"id": "S1", "name": "Old", "project_id": "P1", "is_archived": False, "added_at": T1}],
        },
    )
    put(backup, "sync/projects/P3.json", project("P3", is_archived=False, is_favorite=True, description="Mine"))
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT * FROM projects") == [
        ("P1", "project P1", None, None, 1, 0, "red", T1, T1),
        ("P2", "project P2", None, "P1", 1, 0, None, None, T1),
        ("P3", "project P3", "Mine", None, 0, 1, None, None, T1),
    ]
    assert query(database, "SELECT * FROM sections") == [("S1", "Old", "P1", 0, T1, None)]


def test_labels_of_tasks(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/labels/L2.json", {"id": "L2", "name": "home", "color": "lime_green", "is_favorite": True})
    put(backup, "sync/labels/L1.json", {"id": "L1", "name": "home"})
    put(backup, "sync/items/T.json", task("T", labels=["home", "errand", "home", 7, ""]))
    put(backup, "sync/items/U.json", task("U", labels=None))
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT * FROM task_labels") == [("T", "home", "L1"), ("T", "errand", None)]
    assert query(database, "SELECT * FROM labels") == [("L1", "home", None, 0), ("L2", "home", "lime_green", 1)]


def test_tasks_table(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(
        backup,
        "completed/2026-05.json",
        {"items": [task("T1", checked=True, completed_at=T2, added_at=T1, priority=4, description="Done")]},
    )
    recurring = {"date": "2026-10-26", "string": "every monday", "is_recurring": True}
    put(
        backup,
        "sync/items/T2.json",
        task("T2", checked=False, priority=1, due=recurring, deadline={"date": "2026-12-31", "lang": "en"}),
    )
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT * FROM tasks") == [
        ("T1", "task T1", "Done", None, None, None, "completed", "p1", None, None, 0, None, T1, T2, T1),
        (
            "T2",
            "task T2",
            None,
            None,
            None,
            None,
            "open",
            "p4",
            "2026-10-26",
            "every monday",
            1,
            "2026-12-31",
            None,
            None,
            T1,
        ),
    ]


@pytest.mark.parametrize(("priority", "shown"), [(4, "p1"), (3, "p2"), (2, "p3"), (1, "p4"), (0, None), (True, None)])
def test_priority(tmp_path, priority, shown):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T.json", task("T", priority=priority))
    assert query(build_db(backup, tmp_path / "todoist.db"), "SELECT priority FROM tasks") == [(shown,)]


def upload(name):
    return {"resource_type": "file", "file_name": name, "file_url": f"https://files.todoist.com/x/{name}"}


def test_comments_table(tmp_path):
    backup = new_backup(tmp_path / "backup")
    notes = {
        "C1": comment("C1", item_id="T", posted_at=T1, file_attachment=upload("preise.pdf")),
        "C2": comment("C2", item_id="T", file_attachment=upload("missing.pdf")),
        "C3": comment("C3", project_id="P1"),
        "C4": comment("C4", task_id="T", file_attachment={"resource_type": "url", "url": "https://example.org/a"}),
        "C5": comment("C5", item_id="T", file_attachment=upload("partial.pdf")),
        "c6": comment("c6", item_id="T", file_attachment=upload("case.pdf")),
    }
    for comment_id, value in notes.items():
        resource = "project_notes" if "project_id" in value else "notes"
        put(backup, f"sync/{resource}/{storage.encode_key(comment_id)}.json", value)
    (backup / "attachments" / "C1").mkdir(parents=True)
    (backup / "attachments" / "C1" / "preise.pdf").write_bytes(b"%PDF")
    (backup / "attachments" / "C5").mkdir()
    (backup / "attachments" / "C5" / ".partial.pdf.0a1b2c3d.tmp").write_bytes(b"%PD")
    # The directory of another comment, whose ID differs only in letter case.
    (backup / "attachments" / "C6").mkdir()
    (backup / "attachments" / "C6" / "case.pdf").write_bytes(b"%PDF")
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT * FROM comments") == [
        ("C1", "T", None, "comment C1", T1, "preise.pdf", "attachments/C1/preise.pdf", None),
        ("C2", "T", None, "comment C2", None, "missing.pdf", None, None),
        ("C3", None, "P1", "comment C3", None, None, None, None),
        ("C4", "T", None, "comment C4", None, None, None, "https://example.org/a"),
        ("C5", "T", None, "comment C5", None, "partial.pdf", None, None),
        ("c6", "T", None, "comment c6", None, "case.pdf", None, None),
    ]


def test_attachment_path_of_an_encoded_id(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/notes/a%2Eb.json", comment("a.b", item_id="T", file_attachment=upload("x.pdf")))
    (backup / "attachments" / "a%2Eb").mkdir(parents=True)
    (backup / "attachments" / "a%2Eb" / "x.pdf").write_bytes(b"%PDF")
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT id, attachment_path FROM comments") == [("a.b", "attachments/a%2Eb/x.pdf")]


def test_completions_table(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "completed/activity/2026-10.json", {"results": [event(1, "T", "2026-10-01T08:00:00Z", content="Pay")]})
    entry = {"id": "b-1", "task_id": "T", "content": "Pay rent", "project_id": "P", "completed_at": T1}
    put(backup, "completed/backfill/2026-05.json", {"items": [entry]})
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT * FROM completions") == [
        ("1", "T", "Pay", "P", "2026-10-01T08:00:00Z", "activity"),
        ("b-1", "T", "Pay rent", "P", T1, "backfill"),
    ]


# --- 3.3 the rebuild ----------------------------------------------------------------------


def fail_after_tasks(monkeypatch, error: BaseException) -> None:
    real_insert = sqlite_view._insert

    def failing(con, table, rows):
        count = real_insert(con, table, rows)
        if table.name == "tasks":
            raise error
        return count

    monkeypatch.setattr(sqlite_view, "_insert", failing)


@pytest.mark.parametrize(("error", "code"), [(RuntimeError("killed"), 1), (KeyboardInterrupt(), 130)])
def test_failed_build_keeps_the_previous_content(tmp_path, monkeypatch, capsys, error, code):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    before = dump(database)
    metadata = (tmp_path / "todoist.metadata.json").read_bytes()
    put(backup, "sync/items/T2.json", task("T2"))
    put(backup, "sync/labels/L.json", {"id": "L", "name": "new"})
    fail_after_tasks(monkeypatch, error)
    assert main(["sqlite", str(backup), str(database)]) == code
    assert stderr_lines(capsys)[-1].startswith("ERROR ")
    assert dump(database) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup", "todoist.db", "todoist.metadata.json"]
    assert (tmp_path / "todoist.metadata.json").read_bytes() == metadata


def test_failed_first_build_leaves_no_tables(tmp_path, monkeypatch):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = tmp_path / "todoist.db"
    with monkeypatch.context() as patch:
        fail_after_tasks(patch, RuntimeError("killed"))
        assert main(["sqlite", "--quiet", str(backup), str(database)]) == 1
    assert query(database, "SELECT name FROM sqlite_master") == []
    # The file is still the tool's to use.
    assert database.read_bytes() == b""
    build_db(backup, database)
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    assert query(database, "PRAGMA auto_vacuum") == [(1,)]


def test_older_schema_is_replaced(tmp_path):
    database = tmp_path / "todoist.db"
    with closing(sqlite3.connect(database)) as con:
        con.executescript(
            f"""
            PRAGMA auto_vacuum = FULL;
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE old (id INTEGER PRIMARY KEY AUTOINCREMENT, x TEXT);
            CREATE INDEX old_x ON old (x);
            CREATE VIEW old_view AS SELECT * FROM old;
            CREATE TRIGGER old_trigger AFTER INSERT ON tasks BEGIN INSERT INTO old (x) VALUES (new.id); END;
            CREATE VIRTUAL TABLE old_fts USING fts5(title, content='tasks');
            INSERT INTO tasks VALUES ('T0', 'old');
            PRAGMA application_id = {APPLICATION_ID};
            PRAGMA user_version = 0;
            """
        )
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    build_db(backup, database)
    names = query(database, "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type != 'table'")
    assert names == [("index", "completions_task_id"), *(("view", view.name) for view in VIEWS)]
    assert tables_of(database) == TABLE_NAMES
    assert not query(database, "SELECT name FROM sqlite_master WHERE name LIKE 'old%'")
    # SQLite does not let its own table of AUTOINCREMENT counters be dropped; dropping `old` empties it.
    assert query(database, "SELECT * FROM sqlite_sequence") == []
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    assert query(database, "PRAGMA user_version") == [(SCHEMA_VERSION,)]


def test_index_of_completions_by_task(tmp_path):
    database = build_db(new_backup(tmp_path / "backup"), tmp_path / "todoist.db")
    assert query(
        database, "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'completions_task_id'"
    ) == [("completions",)]
    assert [row[2] for row in query(database, "PRAGMA index_info(completions_task_id)")] == ["task_id"]


def test_same_backup_twice(tmp_path):
    backup = build_backup(tmp_path / "backup")
    database = build_db(backup, tmp_path / "todoist.db")
    first = dump(database)
    size = database.stat().st_size
    build_db(backup, database)
    assert dump(database) == first
    assert database.stat().st_size == size
    assert dump(build_db(backup, tmp_path / "other.db")) == first


def test_file_shrinks_with_fewer_rows(tmp_path):
    backup = new_backup(tmp_path / "backup")
    many = [task(f"T{i:05}", description="x" * 500) for i in range(3000)]
    put(backup, "completed/2026-05.json", {"items": many})
    database = build_db(backup, tmp_path / "todoist.db")
    large = database.stat().st_size
    put(backup, "completed/2026-05.json", {"items": many[:10]})
    build_db(backup, database)
    assert database.stat().st_size < large / 10
    assert query(database, "SELECT count(*) FROM tasks") == [(10,)]


# --- 3.4 an open database -----------------------------------------------------------------


def test_open_database_sees_the_new_content(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    with closing(sqlite3.connect(database)) as reader:
        assert reader.execute("SELECT id FROM tasks").fetchall() == [("T1",)]
        put(backup, "sync/items/T2.json", task("T2"))
        build_db(backup, database)
        assert reader.execute("SELECT id FROM tasks").fetchall() == [("T1",), ("T2",)]
        assert reader.execute("SELECT count(*) FROM tasks_fts").fetchall() == [(2,)]


@pytest.mark.parametrize("hold", ["write", "read"])
def test_database_kept_locked(tmp_path, monkeypatch, capsys, hold):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    before = dump(database)
    put(backup, "sync/items/T2.json", task("T2"))
    monkeypatch.setattr(sqlite_view, "BUSY_TIMEOUT", 0.2)
    with closing(sqlite3.connect(database, isolation_level=None)) as holder:
        if hold == "write":
            holder.execute("BEGIN IMMEDIATE")
        else:
            holder.execute("BEGIN")
            assert holder.execute("SELECT count(*) FROM tasks").fetchall() == [(1,)]
        assert main(["sqlite", str(backup), str(database)]) == 1
        holder.execute("ROLLBACK")
    last = stderr_lines(capsys)[-1]
    assert last.startswith("ERROR ") and str(database) in last and "previous content" in last
    assert dump(database) == before


# --- 3.5 full-text search -----------------------------------------------------------------


def search(database: Path, table: str, word: str) -> list[str]:
    sql = f"SELECT t.id FROM {table}_fts f JOIN {table} t ON t.rowid = f.rowid WHERE {table}_fts MATCH ? ORDER BY 1"
    return [row_id for (row_id,) in query(database, sql, word)]


def test_search_a_word(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1", description="Termin beim Steuerberater"))
    put(backup, "sync/items/T2.json", task("T2", content="Steuerberater anrufen"))
    put(backup, "sync/items/T3.json", task("T3", description="Zahnarzt"))
    put(backup, "sync/notes/C1.json", comment("C1", item_id="T3", content="Größe 🍎 beim Steuerberater"))
    put(backup, "sync/notes/C2.json", comment("C2", item_id="T3"))
    database = build_db(backup, tmp_path / "todoist.db")
    assert search(database, "tasks", "Steuerberater") == ["T1", "T2"]
    assert search(database, "comments", "Steuerberater") == ["C1"]
    assert search(database, "comments", "Größe") == ["C1"]


class _NoFts5(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.startswith("CREATE VIRTUAL TABLE"):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, *args)


def test_no_full_text_search_available(tmp_path, monkeypatch, capsys):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1", description="Steuerberater"))
    database = tmp_path / "todoist.db"
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, factory=_NoFts5, **kwargs))
    assert main(["sqlite", str(backup), str(database)]) == 0
    warnings = [line for line in stderr_lines(capsys) if line.startswith("WARNING ")]
    assert len(warnings) == 1 and "full-text search" in warnings[0] and "no such module: fts5" in warnings[0]
    monkeypatch.undo()
    assert query(database, "SELECT name FROM sqlite_master WHERE name LIKE '%fts%'") == []
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    assert sqlite_view.build(database, read_backup(backup)).search is True


def test_other_errors_of_the_search_tables_fail_the_build(tmp_path, monkeypatch, capsys):
    class Broken(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("CREATE VIRTUAL TABLE"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    backup = new_backup(tmp_path / "backup")
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, factory=Broken, **kwargs))
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 1
    assert "disk I/O error" in stderr_lines(capsys)[-1]


# --- 4.1 the metadata file ----------------------------------------------------------------


def metadata_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["todoist.db", "todoist", "todoist.sqlite"])
def test_name_of_the_metadata_file(tmp_path, name):
    backup = new_backup(tmp_path / "backup")
    build_db(backup, tmp_path / name)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted({"backup", name, "todoist.metadata.json"})
    assert list(metadata_of(tmp_path / "todoist.metadata.json")["databases"]) == ["todoist"]


@pytest.mark.parametrize("kind", ["text", "other json", "json list", "directory"])
def test_foreign_metadata_file(tmp_path, capsys, monkeypatch, kind):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    path = tmp_path / "todoist.metadata.json"
    path.unlink()
    if kind == "text":
        path.write_bytes(b"my notes\n")
    elif kind == "other json":
        path.write_bytes(b'{"title": "mine", "source": "me"}\n')
    elif kind == "json list":
        path.write_bytes(b'["todoist-takeout"]\n')
    else:
        path.mkdir()
        (path / "inside.txt").write_bytes(b"mine\n")
    put(backup, "sync/items/T2.json", task("T2"))
    before = snapshot_of(tmp_path)
    connects = []
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connects.append(args))
    assert main(["sqlite", str(backup), str(database)]) == 2
    last = stderr_lines(capsys)[-1]
    assert last.startswith("ERROR ") and str(path) in last
    assert snapshot_of(tmp_path) == before
    assert connects == []


def test_foreign_metadata_file_before_a_new_database(tmp_path, capsys):
    backup = new_backup(tmp_path / "backup")
    (tmp_path / "todoist.metadata.json").write_bytes(b"{}\n")
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup", "todoist.metadata.json"]


def test_every_table_view_and_column_is_described(tmp_path):
    database = build_db(build_backup(tmp_path / "backup"), tmp_path / "todoist.db")
    tables = metadata_of(tmp_path / "todoist.metadata.json")["databases"]["todoist"]["tables"]
    assert sorted(tables) == sorted(TABLE_NAMES + VIEW_NAMES)
    assert tables_of(database) == TABLE_NAMES
    assert sorted(name for (name,) in query(database, "SELECT name FROM sqlite_master WHERE type = 'view'")) == sorted(
        VIEW_NAMES
    )
    for name, options in tables.items():
        assert options["description"]
        columns = [row[1] for row in query(database, f"PRAGMA table_info({name})")]
        assert list(options["columns"]) == columns
        assert all(options["columns"].values())


def test_datasette_options(tmp_path):
    backup = new_backup(tmp_path / "backup")
    storage.write_state(backup, {**storage.new_state(), "last_successful_run_utc": "2026-10-18T10:00:05Z"})
    build_db(backup, tmp_path / "todoist.db")
    value = metadata_of(tmp_path / "todoist.metadata.json")
    assert value["source"] == "todoist-takeout"
    assert str(backup) in value["description"] and "2026-10-18T10:00:05Z" in value["description"]
    tables = value["databases"]["todoist"]["tables"]
    assert tables["tasks"]["facets"] == ["status", "project_id", "priority", "is_recurring"]
    assert tables["tasks"]["sort_desc"] == "updated_at"
    assert tables["completions"]["facets"] == ["source", "project_id"]
    assert tables["completions"]["sort_desc"] == "completed_at"
    assert tables["comments"]["sort_desc"] == "posted_at"
    labels = {name: options.get("label_column") for name, options in tables.items()}
    assert labels == {name: None for name in TABLE_NAMES + VIEW_NAMES} | {
        "projects": "name",
        "sections": "name",
        "labels": "name",
        "tasks": "content",
    }
    search = {name: options.get("fts_table") for name, options in tables.items()}
    assert search == {name: None for name in TABLE_NAMES + VIEW_NAMES} | {
        "tasks": "tasks_fts",
        "comments": "comments_fts",
    }
    for view in VIEWS:
        assert tables[view.name]["facets"] == list(view.facets)
        assert not {"sort", "sort_desc", "label_column", "fts_table"} & set(tables[view.name])


def test_metadata_without_full_text_search(tmp_path, monkeypatch):
    backup = new_backup(tmp_path / "backup")
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, factory=_NoFts5, **kwargs))
    build_db(backup, tmp_path / "todoist.db")
    tables = metadata_of(tmp_path / "todoist.metadata.json")["databases"]["todoist"]["tables"]
    assert all("fts_table" not in options for options in tables.values())


def test_metadata_is_the_same_on_two_runs(tmp_path):
    backup = build_backup(tmp_path / "backup")
    path = tmp_path / "todoist.metadata.json"
    build_db(backup, tmp_path / "todoist.db")
    first = path.read_bytes()
    build_db(backup, tmp_path / "todoist.db")
    assert path.read_bytes() == first


def test_failed_metadata_write_is_repeated_by_the_next_run(tmp_path, monkeypatch, capsys):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = tmp_path / "todoist.db"

    def failing(path, data, **kwargs):
        raise StorageError(f"cannot write {path}: No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write_bytes_atomic", failing)
        assert main(["sqlite", str(backup), str(database)]) == 1
    assert "todoist.metadata.json" in stderr_lines(capsys)[-1]
    assert query(database, "SELECT id FROM tasks") == [("T1",)]
    build_db(backup, database)
    assert metadata_of(tmp_path / "todoist.metadata.json")["source"] == "todoist-takeout"


# --- 4.2 Datasette ------------------------------------------------------------------------


def test_opened_in_datasette(tmp_path):
    from datasette.app import Datasette

    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/projects/P1.json", project("P1", name="Steuern"))
    put(backup, "sync/items/T1.json", task("T1", project_id="P1", description="Termin beim Steuerberater"))
    put(backup, "sync/items/T2.json", task("T2", project_id="P1", description="Zahnarzt"))
    database = build_db(backup, tmp_path / "todoist.db")

    async def get(ds, path):
        response = await ds.client.get(path)
        assert response.status_code == 200, path
        return response

    async def browse():
        ds = Datasette(files=[str(database)], metadata=metadata_of(tmp_path / "todoist.metadata.json"))
        await ds.invoke_startup()
        page = (await get(ds, "/todoist/tasks")).text
        table = (await get(ds, "/todoist/tasks.json?_labels=on&_shape=objects")).json()
        found = (await get(ds, "/todoist/tasks.json?_search=Steuerberater&_shape=objects")).json()
        return page, table, found

    page, table, found = asyncio.run(browse())
    tasks = next(t for t in TABLES if t.name == "tasks")
    assert tasks.description in page
    assert 'data-column-description="open or completed."' in page
    assert list(table["facet_results"]) == ["status", "project_id", "priority", "is_recurring"]
    assert [row["project_id"] for row in table["rows"]] == [{"value": "P1", "label": "Steuern"}] * 2
    assert [row["id"] for row in found["rows"]] == ["T1"]


# --- 5.1 messages and exit status ---------------------------------------------------------


def test_successful_run(tmp_path, capsys):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/projects/P1.json", project("P1"))
    put(backup, "sync/items/T1.json", task("T1", labels=["home"]))
    put(backup, "sync/items/T2.json", task("T2"))
    database = tmp_path / "todoist.db"
    assert main(["sqlite", str(backup), str(database)]) == 0
    assert stderr_lines(capsys) == [
        f"INFO reading the backup in {backup}",
        f"INFO building {database}",
        f"INFO view it with: datasette {database} -m {tmp_path / 'todoist.metadata.json'}",
        "INFO database complete: 1 project, 0 sections, 0 labels, 2 tasks, 1 task label, 0 comments, "
        "0 completions, 0 warnings",
    ]


def test_quiet_run(tmp_path, capsys):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    assert main(["sqlite", "--quiet", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert capsys.readouterr().err == ""


def test_warnings_are_counted(tmp_path, capsys, monkeypatch):
    backup = new_backup(tmp_path / "backup")
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, factory=_NoFts5, **kwargs))
    assert main(["sqlite", "--quiet", str(backup), str(tmp_path / "todoist.db")]) == 0
    lines = stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("WARNING ")
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 0
    assert stderr_lines(capsys)[-1].endswith(", 1 warning")


def test_view_hint_quotes_paths_with_spaces(tmp_path, capsys):
    backup = new_backup(tmp_path / "backup")
    database = tmp_path / "my data" / "todoist.db"
    assert main(["sqlite", str(backup), str(database)]) == 0
    hint = f'INFO view it with: datasette "{database}" -m "{database.parent / "todoist.metadata.json"}"'
    assert hint in stderr_lines(capsys)


def test_interrupted_while_reading(tmp_path, capsys, monkeypatch):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    before = dump(database)

    def interrupted(backup):
        raise KeyboardInterrupt

    monkeypatch.setattr(sqlite_view, "read_backup", interrupted)
    assert main(["sqlite", str(backup), str(database)]) == 130
    assert stderr_lines(capsys)[-1] == "ERROR interrupted; the database was left unchanged"
    assert dump(database) == before
    # The lock was released.
    with TargetLock(backup):
        pass


def test_unexpected_error_explains_itself(tmp_path, capsys, monkeypatch):
    backup = new_backup(tmp_path / "backup")

    def broken(backup):
        raise ValueError("bad value")

    monkeypatch.setattr(sqlite_view, "read_backup", broken)
    assert main(["sqlite", str(backup), str(tmp_path / "todoist.db")]) == 1
    assert stderr_lines(capsys)[-1] == (
        "ERROR unexpected error: ValueError: bad value; rerun with --verbose for details"
    )
    assert not (tmp_path / "todoist.db").exists()


# --- 5.2 the whole command ----------------------------------------------------------------


def test_whole_command_on_paths_with_spaces_and_non_ascii(tmp_path, capsys):
    backup = build_backup(tmp_path / "Sicherung März 2026")
    database = tmp_path / "Datenbank ä" / "todoist ü.db"
    assert main(["sqlite", str(backup), str(database)]) == 0
    assert stderr_lines(capsys)[-1] == (
        "INFO database complete: 5 projects, 3 sections, 2 labels, 13 tasks, 2 task labels, 7 comments, "
        "3 completions, 0 warnings"
    )
    counts = {name: query(database, f"SELECT count(*) FROM {name}")[0][0] for name in TABLE_NAMES}
    assert counts == {
        "projects": 5,
        "sections": 3,
        "labels": 2,
        "tasks": 13,
        "task_labels": 2,
        "comments": 7,
        "completions": 3,
    }
    paths = [
        path for (path,) in query(database, "SELECT attachment_path FROM comments WHERE attachment_path IS NOT NULL")
    ]
    assert len(paths) == 4
    for path in paths:
        assert "\\" not in path and path.startswith("attachments/")
        assert (backup / path).is_file()
    assert list(metadata_of(database.parent / "todoist ü.metadata.json")["databases"]) == ["todoist ü"]


# --- the views ----------------------------------------------------------------------------


def view_rows(database: Path, view: str) -> list[dict]:
    with closing(sqlite3.connect(database)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(f"SELECT * FROM {view}")]


@pytest.mark.parametrize("view", VIEWS, ids=lambda view: view.name)
def test_columns_of_a_view_match_its_definition(tmp_path, view):
    database = build_db(new_backup(tmp_path / "backup"), tmp_path / "todoist.db")
    assert [row[1] for row in query(database, f"PRAGMA table_info({view.name})")] == [c.name for c in view.columns]


def test_failed_rebuild_keeps_the_previous_views(tmp_path, monkeypatch, capsys):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T1.json", task("T1"))
    database = build_db(backup, tmp_path / "todoist.db")
    views = "SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY rowid"
    before = query(database, views)
    assert [name for name, _sql in before] == [view.name for view in VIEWS]
    put(backup, "sync/items/T2.json", task("T2"))

    def failing(con):
        raise RuntimeError("killed")

    # The search tables come after the views, so the views were dropped and created anew.
    monkeypatch.setattr(sqlite_view, "_create_search", failing)
    assert main(["sqlite", "--quiet", str(backup), str(database)]) == 1
    assert query(database, views) == before
    assert [row["id"] for row in view_rows(database, "open_tasks")] == ["T1"]


# --- the view open_tasks ------------------------------------------------------------------

FAR_FUTURE = "2999-12-31"


def due(value, **fields):
    return {"date": value, "string": value, "is_recurring": False, **fields}


def test_open_task_with_names(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/projects/P1.json", project("P1", name="Home"))
    put(backup, "sync/sections/S1.json", {"id": "S1", "name": "Garden", "project_id": "P1"})
    put(
        backup,
        "sync/items/T1.json",
        task(
            "T1",
            project_id="P1",
            section_id="S1",
            labels=["weekend", "errand"],
            priority=4,
            due=due(FAR_FUTURE, string="every year", is_recurring=True),
            deadline={"date": "2999-12-30"},
            added_at=T1,
        ),
    )
    database = build_db(backup, tmp_path / "todoist.db")
    assert view_rows(database, "open_tasks") == [
        {
            "id": "T1",
            "content": "task T1",
            "project_name": "Home",
            "section_name": "Garden",
            "labels": "errand, weekend",
            "priority": "p1",
            "due_date": FAR_FUTURE,
            "due_string": "every year",
            "is_recurring": 1,
            "deadline_date": "2999-12-30",
            "overdue": 0,
            "added_at": T1,
        }
    ]


def test_open_tasks_leave_out_completed_tasks_and_archived_projects(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/projects/P1.json", project("P1", is_archived=False))
    put(backup, "archived/P2/project.json", project("P2", is_archived=True))
    put(backup, "sync/items/T1.json", task("T1", project_id="P1"))
    put(backup, "completed/2026-05.json", {"items": [task("T2", project_id="P1", checked=True)]})
    put(backup, "archived/P2/full.json", {"tasks": [task("T3", project_id="P2")]})
    put(backup, "sync/items/T4.json", task("T4", project_id="P9"))  # a project not in the database
    database = build_db(backup, tmp_path / "todoist.db")
    rows = [(row["id"], row["project_name"]) for row in view_rows(database, "open_tasks")]
    assert rows == [("T1", "project P1"), ("T4", None)]


def test_order_of_open_tasks(tmp_path):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/A.json", task("A", due=due("2026-10-25"), priority=3))
    put(backup, "sync/items/B.json", task("B", due=due("2026-10-25"), priority=4))
    put(backup, "sync/items/C.json", task("C", due=due("2026-10-22"), priority=1))
    put(backup, "sync/items/D.json", task("D", priority=4))
    # A due time on the same day: the priority decides before the time.
    put(backup, "sync/items/E.json", task("E", due=due("2026-10-25T08:00:00"), priority=4))
    database = build_db(backup, tmp_path / "todoist.db")
    assert [row["id"] for row in view_rows(database, "open_tasks")] == ["C", "B", "E", "A", "D"]


@pytest.mark.parametrize(
    ("due_date", "overdue"),
    [
        (None, 0),
        ("2000-01-01", 1),
        (FAR_FUTURE, 0),
        ("2000-01-01T09:00:00", 1),
        ("2000-01-01T09:00:00Z", 1),
        ("2000-01-01T09:00:00.000000Z", 1),
        ("2999-12-31T09:00:00", 0),
        ("2999-12-31T09:00:00Z", 0),
    ],
)
def test_overdue(tmp_path, due_date, overdue):
    backup = new_backup(tmp_path / "backup")
    put(backup, "sync/items/T.json", task("T", due=due(due_date) if due_date else None))
    database = build_db(backup, tmp_path / "todoist.db")
    assert [row["overdue"] for row in view_rows(database, "open_tasks")] == [overdue]


def test_overdue_is_computed_when_queried(tmp_path):
    # SQLite's clock cannot be set from Python, so a day passing is tested by its two halves: the
    # view computes overdue when it is queried, and a task due yesterday is overdue, one due today not.
    today = date.today()  # the local date, as SQLite's 'localtime'
    backup = new_backup(tmp_path / "backup")
    for name, day in [("T1", today - timedelta(days=1)), ("T2", today), ("T3", today + timedelta(days=1))]:
        put(backup, f"sync/items/{name}.json", task(name, due=due(day.isoformat())))
    database = build_db(backup, tmp_path / "todoist.db")
    assert query(database, "SELECT type FROM sqlite_master WHERE name = 'open_tasks'") == [("view",)]
    assert {row["id"]: row["overdue"] for row in view_rows(database, "open_tasks")} == {"T1": 1, "T2": 0, "T3": 0}
