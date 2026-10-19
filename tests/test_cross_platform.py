"""Tests whose result depends on the platform: file-system case handling and the exact bytes of a backup.

CI runs them on Linux, macOS and Windows (.github/workflows/tests.yml).
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from todoist_takeout import archived, completed, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import StorageError
from todoist_takeout.lock import LOCK_FILE
from todoist_takeout.resources import Classifier

from fake_api import (
    ACTIVITIES,
    ARCHIVED_PROJECTS,
    BY_COMPLETION_DATE,
    COMMENTS,
    TASKS_COMPLETED,
    FakeApi,
    archived_fixture,
    completed_fixture,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_TREE = FIXTURES / "expected_tree"
UPDATE_VARIABLE = "TODOIST_TAKEOUT_UPDATE_EXPECTED"
TOKEN = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 10, 18, 10, 0, 5, tzinfo=timezone.utc)


def case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_bytes(b"")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_case_insensitive_file_system_refuses_to_merge_keys(tmp_path):
    if not case_insensitive(tmp_path):
        pytest.skip("the file system of the temporary directory is case-sensitive")
    response = {
        "full_sync": True,
        "projects": [
            {"id": "6Jf8VQXxpwv5XvQ2", "name": "Work"},
            {"id": "6jf8vqxxpwv5xvq2", "name": "Another project"},
        ],
    }
    with pytest.raises(StorageError) as info:
        sync.apply_response(tmp_path, response, Classifier())
    assert "'6Jf8VQXxpwv5XvQ2'" in str(info.value) and "'6jf8vqxxpwv5xvq2'" in str(info.value)
    [stored] = (tmp_path / "sync" / "projects").iterdir()
    assert json.loads(stored.read_bytes()) == response["projects"][0]


def files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != LOCK_FILE  # the lock file is empty and not part of the format
    }


def completed_answers(api: FakeApi) -> FakeApi:
    """Completed tasks of 2026-09 in two pages with the comments of one task, the backfill of
    2026-09, and an event before and one at T0. The other months are empty."""
    since, until = completed.month_window("2026-09")
    september = {"since": since, "until": until}
    pages = [completed_fixture("by_completion_date_1"), completed_fixture("by_completion_date_2")]
    api.cursor_pages(BY_COMPLETION_DATE, september, pages)
    api.answer(COMMENTS, {"task_id": "6X8Gq4CfVh9xPm2R"}, completed_fixture("comments"))
    api.answer(TASKS_COMPLETED, september, completed_fixture("tasks_completed"))
    before_t0 = completed_fixture("activities_2")["results"][0]
    at_t0 = {**completed_fixture("activities_1")["results"][0], "event_date": completed.precise_timestamp(NOW)}
    return api.answer(ACTIVITIES, {}, {"results": [before_t0, at_t0], "next_cursor": None})


def archived_answers(api: FakeApi) -> FakeApi:
    """One archived project in the second of two list pages, with an archived sub-project that only its
    /full names, completed tasks in two pages of one window, a commented task and a project comment."""
    parent = archived_fixture("full_parent")
    project_id = parent["project"]["id"]
    api.cursor_pages(ARCHIVED_PROJECTS, {}, [{"results": [], "next_cursor": "YXJjaGl2ZWQ6Mg"},
                                             {"results": [parent["project"]], "next_cursor": None}])
    for full in (parent, archived_fixture("full_child")):
        api.answer(archived.full_path(full["project"]["id"]), {}, full)
    window = {"since": "2024-01-01T00:00:00.000000Z", "until": "2024-03-31T23:59:59.999999Z", "project_id": project_id}
    api.cursor_pages(BY_COMPLETION_DATE, window, [archived_fixture("completed_1"), archived_fixture("completed_2")])
    [commented] = [task for task in parent["tasks"] if task["note_count"]]
    api.answer(COMMENTS, {"task_id": commented["id"]}, archived_fixture("comments_task"))
    return api.answer(COMMENTS, {"project_id": project_id}, archived_fixture("comments_project"))


def test_cli_backup_matches_the_expected_tree(tmp_path, monkeypatch):
    responses = {
        "*": (FIXTURES / "sync" / "full_sync.json").read_bytes(),
        "FULL-1": (FIXTURES / "sync" / "incremental_edit.json").read_bytes(),
    }
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)
    monkeypatch.setattr(sync, "_utc_now", lambda: NOW)
    target = tmp_path / "backup"

    def factory(token, max_rate_limit_wait):
        return Client(archived_answers(completed_answers(FakeApi(responses))), sleep=lambda delay: None)

    assert main(["backup", "--quiet", str(target)], client_factory=factory) == 0
    produced = files(target)

    if os.environ.get(UPDATE_VARIABLE) == "1":
        shutil.rmtree(EXPECTED_TREE, ignore_errors=True)
        for name, data in produced.items():
            path = EXPECTED_TREE / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    expected = files(EXPECTED_TREE)

    hint = f"if the change is intended, regenerate the tree with {UPDATE_VARIABLE}=1 pytest {__file__}"
    assert produced.keys() == expected.keys(), hint
    for name in produced:
        assert produced[name] == expected[name], f"{name} differs; {hint}"
    assert json.loads(produced[storage.STATE_FILE])["last_successful_run_utc"] == "2026-10-18T10:00:05Z"
