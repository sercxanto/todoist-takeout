import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todoist_takeout import archived, completed, storage
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import RateLimitExhausted, StorageError
from todoist_takeout.sync import run_backup

from fake_api import (
    ACTIVITIES,
    ARCHIVED_FIXTURES,
    ARCHIVED_PROJECTS,
    BY_COMPLETION_DATE,
    COMMENTS,
    TASKS_COMPLETED,
    FakeApi,
    archived_fixture,
    completed_fixture,
)

# --- 3.1 fixtures --------------------------------------------------------------------------

FIXTURE_NAMES = [
    "comments_project",
    "comments_task",
    "completed_1",
    "completed_2",
    "error_404",
    "error_plan_restricted",
    "full_child",
    "full_parent",
    "list_1",
    "list_2",
]
FULL_KEYS = [
    "project",
    "tasks",
    "sections",
    "subprojects",
    "collaborators",
    "collaborator_states",
    "folder",
    "comments_count",
]


def test_every_fixture_parses_and_is_stored_in_the_backup_encoding():
    assert sorted(p.stem for p in ARCHIVED_FIXTURES.glob("*.json")) == FIXTURE_NAMES
    for name in FIXTURE_NAMES:
        data = (ARCHIVED_FIXTURES / f"{name}.json").read_bytes()
        assert storage.encode_json(json.loads(data)) == data, name


def test_fixtures_have_the_recorded_shapes():
    first, last = archived_fixture("list_1"), archived_fixture("list_2")
    assert list(first) == ["results", "next_cursor"] and first["next_cursor"]
    assert last["next_cursor"] is None
    assert all(p["is_archived"] is True for p in first["results"] + last["results"])

    parent, child = archived_fixture("full_parent"), archived_fixture("full_child")
    assert list(parent) == FULL_KEYS and list(child) == FULL_KEYS
    assert parent["project"] == first["results"][0]
    assert len(parent["tasks"]) == 3 and not any(t["checked"] for t in parent["tasks"])
    assert len(parent["sections"]) == 2
    [sub] = parent["subprojects"]
    assert sub["is_archived"] is True and sub == child["project"]
    assert sub["parent_id"] == parent["project"]["id"]
    assert child["subprojects"] == []

    completed = archived_fixture("completed_1")["items"] + archived_fixture("completed_2")["items"]
    assert list(archived_fixture("completed_1")) == ["items", "next_cursor"]
    assert list(archived_fixture("completed_2")) == ["items"]
    assert {t["project_id"] for t in completed} == {parent["project"]["id"]}
    assert all(t["checked"] and t["completed_at"] for t in completed)
    # The sync, `by_completion_date` and `/full` return tasks with the same fields (proposal, API findings).
    fields = list(completed_fixture("by_completion_date_1")["items"][0])
    assert all(list(t) == fields for t in completed + parent["tasks"] + child["tasks"])

    [commented] = [t for t in parent["tasks"] if t["note_count"]]
    comments = archived_fixture("comments_task")["results"]
    assert len(comments) == commented["note_count"]
    assert {c["item_id"] for c in comments} == {commented["id"]}
    assert [c["file_attachment"]["file_name"] for c in comments if c["file_attachment"]] == ["fliesen.jpg"]
    assert {c["project_id"] for c in archived_fixture("comments_project")["results"]} == {parent["project"]["id"]}

    for name, status in [("error_404", 404), ("error_plan_restricted", 403)]:
        assert archived_fixture(name)["http_code"] == status


# --- 3.3 the snapshot fetch -------------------------------------------------------------------

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TOKEN = "0123456789abcdef0123456789abcdef01234567"

PARENT = archived_fixture("full_parent")["project"]
CHILD = archived_fixture("full_child")["project"]
TRIP = archived_fixture("list_2")["results"][0]
TILES = "6Xt3Rk2Wq8Mv4Pz7"  # the task of PARENT with two comments

# The windows of PARENT, created in October 2023 and last updated in June 2026 (design D7).
PARENT_WINDOWS = 11
# The account's first month of completed tasks: the sync fixtures' user joined in August 2026.
ACCOUNT_FIRST_MONTH = "2026-08"


def sync_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def account(*, plan=None):
    """The sync responses of the fixtures, optionally with changed plan flags."""
    full = json.loads(sync_bytes("full_sync"))
    full["user_plan_limits"]["current"].update(plan or {})
    unchanged = {"sync_token": "INC-1", "full_sync": False, "items": [], "projects": []}
    return {
        "*": json.dumps(full).encode(),
        "FULL-1": sync_bytes("incremental_edit"),
        "INC-1": json.dumps(unchanged).encode(),
    }


def full_response(project, tasks=(), subprojects=()):
    return {
        "project": project,
        "tasks": list(tasks),
        "sections": [],
        "subprojects": list(subprojects),
        "collaborators": [],
        "collaborator_states": [],
        "folder": None,
        "comments_count": 0,
    }


def windows(project, now=NOW):
    """The (since, until) windows of a project, as the tool computes them."""
    return list(archived._Backup(Path("."), None, {}, now, completed_tasks=True, comments=True)._windows(project))


def window_params(project_id, since_until):
    since, until = since_until
    return {"since": since, "until": until, "project_id": project_id}


def listing(api, *pages):
    """Answer the list of archived projects with `pages`, each a list of project payloads."""
    bodies = [
        {"results": list(page), "next_cursor": f"page{i + 2}" if i + 1 < len(pages) else None}
        for i, page in enumerate(pages)
    ]
    return api.cursor_pages(ARCHIVED_PROJECTS, {}, bodies)


def renovation(api=None, *, plan=None):
    """The fixture projects: PARENT and TRIP listed in two pages, the /full of each and of CHILD,
    PARENT's completed tasks in two pages of its second window, and the comments of TILES and PARENT."""
    api = api if api is not None else FakeApi(account(plan=plan))
    api.cursor_pages(ARCHIVED_PROJECTS, {}, [archived_fixture("list_1"), archived_fixture("list_2")])
    api.answer(archived.full_path(PARENT["id"]), {}, archived_fixture("full_parent"))
    api.answer(archived.full_path(CHILD["id"]), {}, archived_fixture("full_child"))
    api.answer(archived.full_path(TRIP["id"]), {}, full_response(TRIP))
    second = window_params(PARENT["id"], windows(PARENT)[1])
    api.cursor_pages(BY_COMPLETION_DATE, second, [archived_fixture("completed_1"), archived_fixture("completed_2")])
    api.answer(COMMENTS, {"task_id": TILES}, archived_fixture("comments_task"))
    api.answer(COMMENTS, {"project_id": PARENT["id"]}, archived_fixture("comments_project"))
    return api


def _no_sleep(delay):
    raise AssertionError("the run should not wait")


def run(target, api=None, now=NOW):
    api = api if api is not None else renovation()
    run_backup(target, Client(api, sleep=_no_sleep), now=lambda: now)
    return api


def cli(target, api, *args):
    def factory(token, wait):
        return Client(api, sleep=_no_sleep)

    return main(["backup", "--quiet", *args, str(target)], client_factory=factory)


def snapshot_file(target, project_id, name):
    return json.loads((archived.snapshot_dir(target, project_id) / name).read_bytes())


def archived_files(target):
    root = target / "archived"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def archived_requests(api):
    """The GET requests of this part: the list, /full, and those filtered by a project or task."""
    return [
        (path, params)
        for path, params in api.get_requests
        if path == ARCHIVED_PROJECTS or path.endswith("/full") or "project_id" in params or "task_id" in params
    ]


def completed_task(id, project_id, completed_at, note_count=0):
    entry = dict(archived_fixture("completed_2")["items"][0])
    entry.update(
        id=id,
        project_id=project_id,
        completed_at=completed_at,
        updated_at=completed_at,
        note_count=note_count,
        content=f"task {id}",
    )
    return entry


def comment(id, item_id, posted_at):
    entry = dict(archived_fixture("comments_task")["results"][1])
    entry.update(id=id, item_id=item_id, posted_at=posted_at, content=f"comment {id}")
    return entry


@pytest.fixture(autouse=True)
def _disarm_fault_hook():
    yield
    storage.set_fault_after(None)


# Archived projects are listed


def test_first_run_stores_a_snapshot_of_each_archived_project(tmp_path):
    projects = [{**TRIP, "id": f"6Xr3Zz{n:010d}", "created_at": "2026-09-01T00:00:00.000000Z"} for n in range(56)]
    api = listing(FakeApi(account()), projects[:30], projects[30:])
    for project in projects:
        api.answer(archived.full_path(project["id"]), {}, full_response(project))

    run(tmp_path, api)

    for project in projects:
        assert snapshot_file(tmp_path, project["id"], "project.json") == project
    assert len(list((tmp_path / "archived").iterdir())) == 56


def test_projects_of_every_page_are_backed_up(tmp_path):
    api = run(tmp_path)
    assert [params.get("cursor") for params in api.requested(ARCHIVED_PROJECTS)] == [None, "YXJjaGl2ZWQ6Mg"]
    # CHILD is not listed; it comes from the subprojects of PARENT (design D8).
    assert sorted(p.name for p in (tmp_path / "archived").iterdir()) == sorted([PARENT["id"], TRIP["id"], CHILD["id"]])


def test_without_archived_projects_only_the_list_is_requested(tmp_path, monkeypatch):
    api = FakeApi(account())
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)
    assert cli(tmp_path, api) == 0
    assert archived_requests(api) == [(ARCHIVED_PROJECTS, {"limit": "200"})]
    assert not (tmp_path / "archived").exists()


# Snapshot of an archived project


def test_snapshot_holds_the_open_tasks_and_sections_as_full_returned_them_and_the_completed_tasks(tmp_path):
    project = {**TRIP, "created_at": "2026-04-20T00:00:00.000000Z", "updated_at": "2026-10-01T09:00:00.000000Z"}
    full = {**archived_fixture("full_parent"), "project": project, "subprojects": []}
    done = [
        completed_task(f"6Xt9Aa{n:010d}", project["id"], f"2026-0{5 + n % 5}-1{n % 10}T08:00:00.000000Z")
        for n in range(18)
    ]
    api = listing(FakeApi(account()), [project])
    api.answer(archived.full_path(project["id"]), {}, full)
    for since_until in windows(project):
        months = window_params(project["id"], since_until)
        inside = [t for t in done if months["since"] <= t["completed_at"] <= months["until"]]
        pages = [{"items": inside[:5], "next_cursor": "more"}, {"items": inside[5:]}]
        api.cursor_pages(BY_COMPLETION_DATE, months, pages)

    run(tmp_path, api)

    assert snapshot_file(tmp_path, project["id"], "full.json") == full
    stored = snapshot_file(tmp_path, project["id"], "completed.json")
    assert sorted(t["id"] for t in stored["items"]) == sorted(t["id"] for t in done)
    assert len(full["tasks"]) == 3 and len(full["sections"]) == 2


def test_comments_of_open_and_completed_tasks_are_stored_and_tasks_without_comments_get_no_request(tmp_path):
    api = renovation()
    done = completed_task("6Xt9Bb0000000001", PARENT["id"], "2024-03-20T10:00:00.000000Z", note_count=2)
    window = window_params(PARENT["id"], windows(PARENT)[1])
    api.cursor_pages(BY_COMPLETION_DATE, window, [archived_fixture("completed_1"), {"items": [done]}])
    pair = [
        comment("6Xc9Cc0000000002", done["id"], "2024-03-19T10:00:00.000000Z"),
        comment("6Xc9Cc0000000001", done["id"], "2024-03-18T10:00:00.000000Z"),
    ]
    api.answer(COMMENTS, {"task_id": done["id"]}, {"results": pair, "next_cursor": None})

    run(tmp_path, api)

    listed = archived_fixture("comments_task")["results"]
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json") == {
        "results": sorted(listed, key=lambda c: c["posted_at"])
    }  # without the cursor
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{done['id']}.json")["results"] == pair[::-1]
    assert sorted(p["task_id"] for p in api.requested(COMMENTS) if "task_id" in p) == sorted([TILES, done["id"]])


def test_project_comments_are_stored(tmp_path):
    api = run(tmp_path)
    expected = archived_fixture("comments_project")
    del expected["next_cursor"]
    assert snapshot_file(tmp_path, PARENT["id"], "project_comments.json") == expected
    assert snapshot_file(tmp_path, TRIP["id"], "project_comments.json") == {"results": []}
    assert [p["project_id"] for p in api.requested(COMMENTS) if "project_id" in p] == [
        PARENT["id"],
        TRIP["id"],
        CHILD["id"],
    ]


# Completed tasks over the project's lifetime


def test_windows_cover_three_months_each_from_the_creation_month_through_the_current_one():
    found = windows({"created_at": "2023-10-05T09:00:00.000000Z"})
    assert len(found) == 13
    assert found[0] == ("2023-10-01T00:00:00.000000Z", "2023-12-31T23:59:59.999999Z")
    assert found[-1] == ("2026-10-01T00:00:00.000000Z", "2026-12-31T23:59:59.999999Z")
    for (since, until), (next_since, _) in zip(found, found[1:]):
        assert completed.parse_time(next_since) - completed.parse_time(until) == timedelta(microseconds=1)
    assert max(completed.parse_time(u) - completed.parse_time(s) for s, u in found) < timedelta(days=92)


def test_a_window_ending_in_the_current_month_is_the_last():
    assert windows({"created_at": "2026-08-31T23:00:00.000000Z"}) == [
        ("2026-08-01T00:00:00.000000Z", "2026-10-31T23:59:59.999999Z")
    ]


def test_completed_tasks_are_requested_in_windows_through_the_last_update(tmp_path):
    api = run(tmp_path)
    requested = [p for p in api.requested(BY_COMPLETION_DATE) if p.get("project_id") == PARENT["id"]]
    firsts = [p for p in requested if "cursor" not in p]
    assert len(firsts) == PARENT_WINDOWS
    assert firsts[0]["since"] == "2023-10-01T00:00:00.000000Z"
    assert firsts[-1]["until"] == "2026-06-30T23:59:59.999999Z"
    assert len(requested) == PARENT_WINDOWS + 1  # the second window has two pages
    stored = snapshot_file(tmp_path, PARENT["id"], "completed.json")
    assert list(stored) == ["items"]
    assert [t["completed_at"] for t in stored["items"]] == sorted(t["completed_at"] for t in stored["items"])
    assert len(stored["items"]) == 3


@pytest.mark.parametrize(
    "updated_at, count, until",
    [
        pytest.param("2025-06-14T10:00:00.000000Z", 7, "2025-06-30T23:59:59.999999Z", id="three years ago"),
        pytest.param(None, 13, "2026-12-31T23:59:59.999999Z", id="archive date unknown"),
        pytest.param("not a time", 13, "2026-12-31T23:59:59.999999Z", id="archive date unparseable"),
        pytest.param("2027-03-01T00:00:00.000000Z", 13, "2026-12-31T23:59:59.999999Z", id="after the current month"),
    ],
)
def test_windows_end_at_the_month_of_the_projects_last_update(tmp_path, updated_at, count, until):
    project = {**TRIP, "created_at": "2023-10-05T09:00:00.000000Z", "updated_at": updated_at}
    api = listing(FakeApi(account()), [project])
    api.answer(archived.full_path(project["id"]), {}, full_response(project))

    run(tmp_path, api)

    firsts = [p for p in api.requested(BY_COMPLETION_DATE) if p.get("project_id") == project["id"]]
    assert len(firsts) == count
    assert firsts[0]["since"] == "2023-10-01T00:00:00.000000Z"
    assert firsts[-1]["until"] == until


@pytest.mark.parametrize("created_at", [None, "", "last spring"])
def test_unknown_creation_date_starts_at_the_accounts_first_month(tmp_path, created_at):
    project = {**TRIP, "created_at": created_at}
    api = listing(FakeApi(account()), [project])
    api.answer(archived.full_path(project["id"]), {}, full_response(project))

    run(tmp_path, api)

    assert json.loads((tmp_path / "state.json").read_bytes())["completed"]["first_month"] == ACCOUNT_FIRST_MONTH
    firsts = [p for p in api.requested(BY_COMPLETION_DATE) if p.get("project_id") == project["id"]]
    assert [p["since"][:7] for p in firsts] == [ACCOUNT_FIRST_MONTH]


def test_without_a_first_month_in_the_state_the_windows_start_at_the_accounts_creation(tmp_path):
    backup = archived._Backup(tmp_path, None, {}, NOW, completed_tasks=True, comments=True)
    assert list(backup._windows({}))[0][0] == "2007-01-01T00:00:00.000000Z"  # no stored user either
    state = {"completed": {"first_month": "2025-02"}}
    backup = archived._Backup(tmp_path, None, state, NOW, completed_tasks=True, comments=True)
    assert list(backup._windows({}))[0][0] == "2025-02-01T00:00:00.000000Z"


# Attachments are not downloaded


def test_comment_attachments_are_stored_as_metadata_and_not_downloaded(tmp_path):
    api = run(tmp_path)
    [stored] = [
        c for c in snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"] if c["file_attachment"]
    ]
    [listed] = [c for c in archived_fixture("comments_task")["results"] if c["file_attachment"]]
    assert stored == listed
    paths = {path for path, _ in api.get_requests}
    assert paths <= {
        ARCHIVED_PROJECTS,
        BY_COMPLETION_DATE,
        COMMENTS,
        ACTIVITIES,
        TASKS_COMPLETED,
        *(archived.full_path(p["id"]) for p in (PARENT, TRIP, CHILD)),
    }


# Project gone during the run


def test_project_deleted_between_list_and_fetch_is_skipped(tmp_path, monkeypatch):
    api = renovation()
    api.answer(archived.full_path(TRIP["id"]), {}, archived_fixture("error_404"), status=404)
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)

    assert cli(tmp_path, api) == 0

    assert not archived.snapshot_dir(tmp_path, TRIP["id"]).exists()
    assert (archived.snapshot_dir(tmp_path, PARENT["id"]) / "project.json").is_file()
    assert [p for p in archived_requests(api) if p[1].get("project_id") == TRIP["id"]] == []


# Archived-projects layout and write order


def test_snapshot_files_follow_the_layout(tmp_path):
    run(tmp_path)
    parent, trip, child = PARENT["id"], TRIP["id"], CHILD["id"]
    assert archived_files(tmp_path) == sorted(
        [
            f"{parent}/project.json",
            f"{parent}/full.json",
            f"{parent}/completed.json",
            f"{parent}/project_comments.json",
            f"{parent}/comments/{TILES}.json",
            *(
                f"{project}/{name}"
                for project in (trip, child)
                for name in ("project.json", "full.json", "completed.json", "project_comments.json")
            ),
        ]
    )
    assert snapshot_file(tmp_path, parent, "project.json") == PARENT
    assert snapshot_file(tmp_path, parent, "full.json") == archived_fixture("full_parent")
    # Every file is in the backup encoding.
    for path in (tmp_path / "archived").rglob("*.json"):
        assert storage.encode_json(json.loads(path.read_bytes())) == path.read_bytes()


def test_project_id_that_needs_encoding_names_its_directory_like_an_object_file(tmp_path):
    project = {**TRIP, "id": "CON"}
    api = listing(FakeApi(account()), [project])
    api.answer(archived.full_path("CON"), {}, full_response(project))
    run(tmp_path, api)
    assert [p.name for p in (tmp_path / "archived").iterdir()] == [storage.encode_key("CON")] == ["%43ON"]


def test_project_json_is_written_last_and_flushed_as_the_checkpoint(tmp_path, monkeypatch):
    writes = []
    real_write = storage.write_bytes_atomic

    def write(path, data, **kwargs):
        if real_write(path, data, **kwargs):
            writes.append((path.relative_to(tmp_path).as_posix(), kwargs.get("full_sync", False)))
            return True
        return False

    monkeypatch.setattr(storage, "write_bytes_atomic", write)
    run(tmp_path)

    parent = f"archived/{PARENT['id']}/"
    assert [(p[len(parent) :], full) for p, full in writes if p.startswith(parent)] == [
        (f"comments/{TILES}.json", False),
        ("project_comments.json", False),
        ("completed.json", False),
        ("full.json", False),
        ("project.json", True),
    ]


@pytest.mark.parametrize(
    "name, stored",
    [
        ("full.json", {**archived_fixture("full_parent"), "project": {**PARENT, "id": PARENT["id"].lower()}}),
    ],
)
def test_snapshot_of_another_project_in_the_same_directory_is_refused(tmp_path, name, stored):
    # A case-insensitive file system maps both IDs to one directory; the test writes the other
    # project's file under this project's ID to get the same situation on any file system.
    storage.load_or_create_state(tmp_path)
    directory = archived.snapshot_dir(tmp_path, PARENT["id"])
    directory.mkdir(parents=True)
    (directory / name).write_bytes(storage.encode_json(stored))

    with pytest.raises(StorageError, match="share a directory"):
        run(tmp_path)
    assert sorted(p.name for p in directory.iterdir()) == [name]
    assert json.loads((directory / name).read_bytes()) == stored


# --- 4.1 detection and refetch ------------------------------------------------------------------

OLD = 1_000_000_000
LATER = NOW + timedelta(days=1)


def age_files(target):
    """Give every file under archived/ an old modification time, and return the tree's bytes."""
    files = {}
    for path in sorted((target / "archived").rglob("*")):
        if path.is_file():
            os.utime(path, ns=(OLD, OLD))
            files[path.relative_to(target).as_posix()] = path.read_bytes()
    return files


def rewritten(target):
    return [
        p.relative_to(target).as_posix()
        for p in (target / "archived").rglob("*")
        if p.is_file() and p.stat().st_mtime_ns != OLD
    ]


def rearchived(project, **changes):
    """The listed payload of a project that was unarchived, changed and archived again."""
    return {**project, "updated_at": "2026-10-18T18:00:00.000000Z", **changes}


def test_repeat_run_requests_only_the_list_and_rewrites_no_file(tmp_path):
    run(tmp_path)
    before = age_files(tmp_path)

    api = run(tmp_path, renovation(), now=LATER)

    assert archived_requests(api) == [
        (ARCHIVED_PROJECTS, {"limit": "200"}),
        (ARCHIVED_PROJECTS, {"limit": "200", "cursor": "YXJjaGl2ZWQ6Mg"}),
    ]
    assert age_files(tmp_path) == before
    assert rewritten(tmp_path) == []


def test_unarchived_and_archived_again_is_fetched_again(tmp_path):
    run(tmp_path)
    age_files(tmp_path)
    parent = rearchived(PARENT)
    new_task = {**archived_fixture("full_parent")["tasks"][2], "id": "6Xt3Rk5Zt3Qy7Sc1", "content": "Fix the door"}
    full = {**archived_fixture("full_parent"), "project": parent}
    full["tasks"] = [*full["tasks"], new_task]
    api = renovation()
    listing(api, [parent], [TRIP])
    api.answer(archived.full_path(PARENT["id"]), {}, full)
    later = completed_task("6Xt9Dd0000000001", PARENT["id"], "2026-10-18T17:00:00.000000Z")
    last = window_params(PARENT["id"], windows(parent, LATER)[-1])
    api.cursor_pages(BY_COMPLETION_DATE, last, [{"items": [later]}])

    run(tmp_path, api, now=LATER)

    assert snapshot_file(tmp_path, PARENT["id"], "project.json") == parent
    assert new_task in snapshot_file(tmp_path, PARENT["id"], "full.json")["tasks"]
    assert later in snapshot_file(tmp_path, PARENT["id"], "completed.json")["items"]
    # Only the changed project is fetched again; TRIP and CHILD keep their snapshots.
    assert [path for path, _ in archived_requests(api) if path.endswith("/full")] == [archived.full_path(PARENT["id"])]
    assert all(path.startswith(f"archived/{PARENT['id']}/") for path in rewritten(tmp_path))


def test_comment_deleted_before_the_project_was_archived_again_stays(tmp_path):
    run(tmp_path)
    kept = snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"]
    parent = rearchived(PARENT)
    full = {**archived_fixture("full_parent"), "project": parent}
    full["tasks"] = [{**t, "note_count": 1} if t["id"] == TILES else t for t in full["tasks"]]
    api = renovation()
    listing(api, [parent], [TRIP])
    api.answer(archived.full_path(PARENT["id"]), {}, full)
    one = {"results": [archived_fixture("comments_task")["results"][1]], "next_cursor": None}
    api.answer(COMMENTS, {"task_id": TILES}, one)
    api.answer(COMMENTS, {"project_id": PARENT["id"]}, {"results": [], "next_cursor": None})

    run(tmp_path, api, now=LATER)

    assert {"task_id": TILES, "limit": "100"} in api.requested(COMMENTS)  # its note_count changed
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"] == kept
    assert len(snapshot_file(tmp_path, PARENT["id"], "project_comments.json")["results"]) == 1


def test_stored_project_json_of_another_project_is_refused(tmp_path):
    storage.load_or_create_state(tmp_path)
    directory = archived.snapshot_dir(tmp_path, PARENT["id"])
    directory.mkdir(parents=True)
    other = storage.encode_json({**PARENT, "id": PARENT["id"].lower()})
    (directory / "project.json").write_bytes(other)

    with pytest.raises(StorageError, match="share a directory"):
        run(tmp_path)
    assert [p.name for p in directory.iterdir()] == ["project.json"]
    assert (directory / "project.json").read_bytes() == other


# --- 4.2 fill-in and plan limits ------------------------------------------------------------------


def set_plan(target, **flags):
    """Change the stored plan flags, as a sync after a plan change would."""
    path = target / "sync" / "user_plan_limits.json"
    limits = json.loads(path.read_bytes())
    limits["current"].update(flags)
    path.write_bytes(storage.encode_json(limits))


def plan_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_plan_without_completed_tasks_stores_snapshots_without_them(tmp_path, monkeypatch):
    api = renovation(plan={"completed_tasks": False})
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)

    assert cli(tmp_path, api) == 0

    assert api.requested(BY_COMPLETION_DATE) == []
    assert not any(name.endswith("completed.json") for name in archived_files(tmp_path))
    assert (archived.snapshot_dir(tmp_path, PARENT["id"]) / "project.json").is_file()
    assert (archived.snapshot_dir(tmp_path, PARENT["id"]) / f"comments/{TILES}.json").is_file()


def test_plan_upgraded_fills_in_the_completed_tasks_only(tmp_path):
    run(tmp_path, renovation(plan={"completed_tasks": False}))
    set_plan(tmp_path, completed_tasks=True)
    age_files(tmp_path)
    api = renovation()
    done = completed_task("6Xt9Ee0000000001", PARENT["id"], "2024-02-20T10:00:00.000000Z", note_count=1)
    api.cursor_pages(BY_COMPLETION_DATE, window_params(PARENT["id"], windows(PARENT)[1]), [{"items": [done]}])
    remark = comment("6Xc9Ff0000000001", done["id"], "2024-02-20T09:00:00.000000Z")
    api.answer(COMMENTS, {"task_id": done["id"]}, {"results": [remark], "next_cursor": None})

    run(tmp_path, api, now=LATER)

    for project in (PARENT, TRIP, CHILD):
        assert (archived.snapshot_dir(tmp_path, project["id"]) / "completed.json").is_file()
    assert snapshot_file(tmp_path, PARENT["id"], "completed.json") == {"items": [done]}
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{done['id']}.json") == {"results": [remark]}
    # The rest of the snapshots is not fetched again.
    assert [path for path, _ in archived_requests(api) if path.endswith("/full")] == []
    # The new task's comments complete the comments part again, with the project's comments last (design D6).
    assert api.requested(COMMENTS) == [
        {"task_id": done["id"], "limit": "100"},
        {"project_id": PARENT["id"], "limit": "100"},
    ]
    assert sorted(rewritten(tmp_path)) == sorted(
        [
            *(f"archived/{p['id']}/completed.json" for p in (PARENT, TRIP, CHILD)),
            f"archived/{PARENT['id']}/comments/{done['id']}.json",
        ]
    )


def test_plan_without_comments_then_with_comments_fills_in_the_comments(tmp_path):
    api = run(tmp_path, renovation(plan={"comments": False}))
    assert api.requested(COMMENTS) == []
    assert not any("comments" in name for name in archived_files(tmp_path))

    set_plan(tmp_path, comments=True)
    api = run(tmp_path, renovation(), now=LATER)

    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"]
    for project in (PARENT, TRIP, CHILD):
        assert (archived.snapshot_dir(tmp_path, project["id"]) / "project_comments.json").is_file()
    assert [p for p in api.requested(COMMENTS) if "task_id" in p] == [{"task_id": TILES, "limit": "100"}]
    assert [path for path, _ in archived_requests(api) if path.endswith("/full")] == []
    assert [p for p in api.requested(BY_COMPLETION_DATE) if "project_id" in p] == []


def test_task_comments_not_found_get_an_empty_file_and_are_not_requested_again(tmp_path):
    api = renovation()
    api.answer(COMMENTS, {"task_id": TILES}, archived_fixture("error_404"), status=404)
    run(tmp_path, api)
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json") == {"results": []}
    assert (archived.snapshot_dir(tmp_path, PARENT["id"]) / "project_comments.json").is_file()

    api = renovation()
    api.answer(COMMENTS, {"task_id": TILES}, archived_fixture("error_404"), status=404)
    run(tmp_path, api, now=LATER)
    assert api.requested(COMMENTS) == []


def test_task_comments_not_found_on_a_refetch_keep_the_stored_ones(tmp_path):
    run(tmp_path)
    kept = snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")
    parent = rearchived(PARENT)
    full = {**archived_fixture("full_parent"), "project": parent}
    full["tasks"] = [{**t, "note_count": 3} if t["id"] == TILES else t for t in full["tasks"]]
    api = listing(renovation(), [parent], [TRIP])
    api.answer(archived.full_path(PARENT["id"]), {}, full)
    api.answer(COMMENTS, {"task_id": TILES}, archived_fixture("error_404"), status=404)

    run(tmp_path, api, now=LATER)

    assert {"task_id": TILES, "limit": "100"} in api.requested(COMMENTS)
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json") == kept


HALL_TASK = {
    **archived_fixture("full_parent")["tasks"][2],
    "id": "6Xt3Rk5Zt3Qy7Sc2",
    "content": "Paint the hall",
    "note_count": 1,
}
HALL_COMMENT = comment("6Xc9Hh0000000001", HALL_TASK["id"], "2026-10-18T16:00:00.000000Z")


def test_comments_allowed_again_after_a_refetch_without_them_are_fetched(tmp_path):
    run(tmp_path)
    set_plan(tmp_path, comments=False)
    parent = rearchived(PARENT)
    full = {**archived_fixture("full_parent"), "project": parent}
    full["tasks"] = [*full["tasks"], HALL_TASK]

    def refetched():
        api = listing(renovation(), [parent], [TRIP])
        api.answer(archived.full_path(PARENT["id"]), {}, full)
        api.answer(COMMENTS, {"task_id": HALL_TASK["id"]}, {"results": [HALL_COMMENT], "next_cursor": None})
        return api

    api = run(tmp_path, refetched(), now=LATER)
    assert full_requests(api) == [archived.full_path(PARENT["id"])]
    assert api.requested(COMMENTS) == []

    set_plan(tmp_path, comments=True)
    api = run(tmp_path, refetched(), now=LATER + timedelta(days=1))

    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{HALL_TASK['id']}.json") == {"results": [HALL_COMMENT]}
    assert [p for p in api.requested(COMMENTS) if "task_id" in p] == [{"task_id": HALL_TASK["id"], "limit": "100"}]
    assert [p["project_id"] for p in api.requested(COMMENTS) if "project_id" in p] == [PARENT["id"]]
    assert full_requests(api) == []


def test_comments_of_completed_tasks_filled_in_without_comments_are_fetched_later(tmp_path):
    run(tmp_path, renovation(plan={"completed_tasks": False}))
    set_plan(tmp_path, completed_tasks=True, comments=False)
    done = completed_task("6Xt9Hh0000000001", PARENT["id"], "2024-02-20T10:00:00.000000Z", note_count=1)
    api = renovation()
    api.cursor_pages(BY_COMPLETION_DATE, window_params(PARENT["id"], windows(PARENT)[1]), [{"items": [done]}])
    api = run(tmp_path, api, now=LATER)
    assert snapshot_file(tmp_path, PARENT["id"], "completed.json") == {"items": [done]}
    assert api.requested(COMMENTS) == []

    set_plan(tmp_path, comments=True)
    remark = comment("6Xc9Hh0000000002", done["id"], "2024-02-20T09:00:00.000000Z")
    api = renovation()
    api.answer(COMMENTS, {"task_id": done["id"]}, {"results": [remark], "next_cursor": None})
    api = run(tmp_path, api, now=LATER + timedelta(days=1))

    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{done['id']}.json") == {"results": [remark]}
    assert [p for p in api.requested(COMMENTS) if "task_id" in p] == [{"task_id": done["id"], "limit": "100"}]
    assert [p for p in api.requested(BY_COMPLETION_DATE) if "project_id" in p] == []
    assert full_requests(api) == []


def test_list_restricted_by_the_plan_warns_and_the_run_succeeds(tmp_path, monkeypatch, capsys):
    api = FakeApi(account()).answer(ARCHIVED_PROJECTS, {}, archived_fixture("error_plan_restricted"), status=403)
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)

    assert cli(tmp_path, api) == 0

    assert capsys.readouterr().err.splitlines() == [
        "WARNING the Todoist plan does not allow listing archived projects; they cannot be backed up on this plan"
    ]
    assert not (tmp_path / "archived").exists()
    assert (tmp_path / "sync" / "items" / "6X7rM8997g3RQmvh.json").is_file()
    assert json.loads((tmp_path / "state.json").read_bytes())["last_successful_run_utc"] is not None


def test_full_restricted_by_the_plan_warns_once_and_ends_this_part(tmp_path, caplog):
    api = renovation()
    for project in (PARENT, TRIP):
        api.answer(archived.full_path(project["id"]), {}, archived_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    assert plan_warnings(caplog) == [
        "the Todoist plan does not allow fetching archived projects; they cannot be backed up on this plan"
    ]
    assert [path for path, _ in archived_requests(api) if path.endswith("/full")] == [archived.full_path(PARENT["id"])]
    assert not (tmp_path / "archived").exists()


def test_completed_tasks_restricted_by_the_plan_are_skipped_and_filled_in_later(tmp_path, caplog):
    api = renovation()
    api.answer(BY_COMPLETION_DATE, {"project_id": PARENT["id"]}, archived_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    assert plan_warnings(caplog) == [
        "the Todoist plan does not allow fetching completed tasks; the completed tasks of archived projects cannot "
        "be backed up on this plan"
    ]
    # The snapshots are finished without the part, and no other project asks for it in this run.
    assert [p["project_id"] for p in api.requested(BY_COMPLETION_DATE) if "project_id" in p] == [PARENT["id"]]
    assert not any(name.endswith("completed.json") for name in archived_files(tmp_path))
    for project in (PARENT, TRIP, CHILD):
        assert (archived.snapshot_dir(tmp_path, project["id"]) / "project.json").is_file()

    run(tmp_path, renovation(), now=LATER)
    for project in (PARENT, TRIP, CHILD):
        assert (archived.snapshot_dir(tmp_path, project["id"]) / "completed.json").is_file()


def test_comments_restricted_by_the_plan_are_skipped_and_filled_in_later(tmp_path, caplog):
    api = renovation()
    api.answer(COMMENTS, {"task_id": TILES}, archived_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    assert plan_warnings(caplog) == [
        "the Todoist plan does not allow fetching comments; the comments of archived projects cannot be backed up "
        "on this plan"
    ]
    assert not any("comments" in name for name in archived_files(tmp_path))
    assert api.requested(COMMENTS) == [{"task_id": TILES, "limit": "100"}]

    api = run(tmp_path, renovation(), now=LATER)
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"]
    assert len([p for p in api.requested(COMMENTS) if "project_id" in p]) == 3


# --- 4.3 sub-projects ---------------------------------------------------------------------------


def full_requests(api):
    return [path for path, _ in archived_requests(api) if path.endswith("/full")]


def test_sub_project_only_in_its_parent_gets_a_snapshot_of_its_own(tmp_path):
    api = run(tmp_path)
    assert snapshot_file(tmp_path, CHILD["id"], "project.json") == archived_fixture("full_parent")["subprojects"][0]
    assert snapshot_file(tmp_path, CHILD["id"], "full.json") == archived_fixture("full_child")
    listed = archived_fixture("list_1")["results"] + archived_fixture("list_2")["results"]
    assert CHILD["id"] not in {p["id"] for p in listed}
    assert full_requests(api).count(archived.full_path(CHILD["id"])) == 1


@pytest.mark.parametrize("pages", [[[PARENT, CHILD], [TRIP]], [[CHILD], [TRIP, PARENT]]])
def test_sub_project_listed_too_is_fetched_once(tmp_path, pages):
    api = listing(renovation(), *pages)
    run(tmp_path, api)
    assert sorted(full_requests(api)) == sorted(archived.full_path(p["id"]) for p in (PARENT, CHILD, TRIP))


def test_sub_projects_naming_each_other_are_fetched_once_each(tmp_path):
    parent_full = {**archived_fixture("full_parent"), "subprojects": [CHILD]}
    child_full = {**archived_fixture("full_child"), "subprojects": [PARENT]}
    api = listing(FakeApi(account()), [PARENT])
    api.answer(archived.full_path(PARENT["id"]), {}, parent_full)
    api.answer(archived.full_path(CHILD["id"]), {}, child_full)

    run(tmp_path, api)

    assert full_requests(api) == [archived.full_path(PARENT["id"]), archived.full_path(CHILD["id"])]


def test_active_sub_projects_are_left_to_the_sync(tmp_path):
    active = {**CHILD, "is_archived": False}
    api = listing(FakeApi(account()), [PARENT])
    api.answer(archived.full_path(PARENT["id"]), {}, {**archived_fixture("full_parent"), "subprojects": [active]})
    run(tmp_path, api)
    assert full_requests(api) == [archived.full_path(PARENT["id"])]


def test_sub_projects_of_a_stored_snapshot_are_read_from_its_full_json(tmp_path):
    run(tmp_path)
    child_dir = archived.snapshot_dir(tmp_path, CHILD["id"])
    (child_dir / "project.json").unlink()  # as if a run stopped before finishing CHILD
    api = run(tmp_path, renovation(), now=LATER)
    assert full_requests(api) == [archived.full_path(CHILD["id"])]
    assert (child_dir / "project.json").is_file()


# --- 4.4 the INFO line ----------------------------------------------------------------------------


def test_info_line_counts_new_and_refetched_snapshots_and_filled_in_parts(tmp_path, caplog):
    first, second = ({**TRIP, "id": f"6Xr4Gg000000000{n}"} for n in (1, 2))
    api = listing(FakeApi(account(plan={"comments": False})), [first, second])
    for project in (first, second):
        api.answer(archived.full_path(project["id"]), {}, full_response(project))
    run(tmp_path, api)
    set_plan(tmp_path, comments=True)

    third, fourth = ({**TRIP, "id": f"6Xr4Gg000000000{n}"} for n in (3, 4))
    changed = rearchived(second)
    api = listing(FakeApi(account()), [first, changed, third], [fourth])
    for project in (changed, third, fourth):
        api.answer(archived.full_path(project["id"]), {}, full_response(project))
    with caplog.at_level(logging.INFO, logger="todoist_takeout.archived"):
        run(tmp_path, api, now=LATER)

    # `first` gets its comments part, `changed` is fetched again, `third` and `fourth` are new.
    assert [r.getMessage() for r in caplog.records if r.name == "todoist_takeout.archived"] == [
        "archived projects: 4 listed, 3 snapshots fetched (2 new, 1 again), 1 parts filled in; 4 comment files written"
    ]


def test_info_line_is_logged_when_the_run_fails(tmp_path, caplog):
    api = renovation()
    api.answer(archived.full_path(TRIP["id"]), {}, {"error": "Too many requests", "http_code": 429}, status=429)
    client = Client(api, max_rate_limit_wait=0, sleep=_no_sleep, jitter=lambda: 0.0)
    with caplog.at_level(logging.INFO, logger="todoist_takeout.archived"), pytest.raises(RateLimitExhausted):
        run_backup(tmp_path, client, now=lambda: NOW)
    assert [r.getMessage() for r in caplog.records if r.name == "todoist_takeout.archived"] == [
        "archived projects: 2 listed, 1 snapshots fetched (1 new, 0 again), 0 parts filled in; 2 comment files written"
    ]


# --- 6.1 crash and resume -----------------------------------------------------------------------

GARDEN = {**TRIP, "id": "6Xr2Pq5Ht8Jw2Ga4", "name": "Garden", "created_at": "2026-04-12T08:00:00.000000Z"}
WEEDS = completed_task("6Xt9Gg0000000001", GARDEN["id"], "2026-05-10T07:30:00.000000Z", note_count=1)


def three_projects():
    """PARENT, TRIP and GARDEN listed, and CHILD in the subprojects of PARENT. GARDEN has a completed
    task with a comment, so that its snapshot, like PARENT's, starts with a comment file."""
    api = listing(renovation(), [PARENT], [TRIP, GARDEN])
    api.answer(archived.full_path(GARDEN["id"]), {}, full_response(GARDEN))
    api.cursor_pages(BY_COMPLETION_DATE, window_params(GARDEN["id"], windows(GARDEN)[0]), [{"items": [WEEDS]}])
    remark = comment("6Xc9Gg0000000001", WEEDS["id"], "2026-05-09T18:00:00.000000Z")
    api.answer(COMMENTS, {"task_id": WEEDS["id"]}, {"results": [remark], "next_cursor": None})
    return api


def tree(target):
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}


# Writes of an uninterrupted first run with three_projects(): 53 for the sync and the completed-tasks
# part, then this part in the order of design D3: PARENT (comments of TILES, project_comments.json,
# completed.json, full.json, project.json), TRIP (the last four), GARDEN (like PARENT), CHILD (like
# TRIP), the attachment files of the comment in the sync, of the comment of TILES and of PARENT's
# project comment, and finally state.json.
ARCHIVED_START = 53
FIRST_RUN_WRITES = 75


@pytest.fixture(scope="module")
def first_run_tree(tmp_path_factory):
    target = tmp_path_factory.mktemp("reference")
    run(target, three_projects())
    return tree(target)


def crash(target, fault_after):
    storage.set_fault_after(fault_after)
    with pytest.raises(storage.InjectedFault):
        run(target, three_projects())
    storage.set_fault_after(None)


def finished(target):
    """The IDs of the projects whose snapshot is finished."""
    root = target / "archived"
    return (
        {json.loads(p.read_bytes())["id"] for p in root.glob(f"*/{archived.PROJECT_FILE}")} if root.exists() else set()
    )


def test_crash_points_cover_this_part_of_the_first_run(tmp_path, first_run_tree, monkeypatch):
    started = []
    real_run = archived.run

    def counting_run(*args, **kwargs):
        started.append(storage._fault_after)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(archived, "run", counting_run)
    storage.set_fault_after(FIRST_RUN_WRITES)
    run(tmp_path / "beyond", three_projects())
    assert storage._fault_after == 0  # armed, never reached: the run makes exactly FIRST_RUN_WRITES writes
    assert started == [FIRST_RUN_WRITES - ARCHIVED_START]
    monkeypatch.undo()

    crash(tmp_path / "first", ARCHIVED_START)
    # The first write of this part failed and left only its temporary file.
    assert [p.name.endswith(".tmp") for p in (tmp_path / "first" / "archived").rglob("*") if p.is_file()] == [True]
    crash(tmp_path / "last", FIRST_RUN_WRITES - 1)
    assert finished(tmp_path / "last") == {p["id"] for p in (PARENT, TRIP, GARDEN, CHILD)}
    assert {f"archived/{p['id']}/project.json" for p in (PARENT, TRIP, GARDEN, CHILD)} <= first_run_tree.keys()
    assert f"archived/{GARDEN['id']}/comments/{WEEDS['id']}.json" in first_run_tree


@pytest.mark.parametrize("fault_after", range(ARCHIVED_START, FIRST_RUN_WRITES))
def test_resume_after_a_crash_in_this_part_gives_the_uninterrupted_result(tmp_path, first_run_tree, fault_after):
    crash(tmp_path, fault_after)
    done = finished(tmp_path)

    api = run(tmp_path, three_projects())

    assert tree(tmp_path) == first_run_tree
    fetched = {path for path, _ in archived_requests(api) if path.endswith("/full")}
    assert fetched == {archived.full_path(p["id"]) for p in (PARENT, TRIP, GARDEN, CHILD) if p["id"] not in done}


# --- 6.2 idempotence ----------------------------------------------------------------------------


def test_repeat_run_leaves_every_snapshot_file_unchanged(tmp_path):
    run(tmp_path, three_projects())
    paths = [p for p in (tmp_path / "archived").rglob("*") if p.is_file()]
    before = age_files(tmp_path)

    api = run(tmp_path, three_projects(), now=LATER)

    assert age_files(tmp_path) == before
    assert [p for p in paths if p.stat().st_mtime_ns != OLD] == []
    assert len(paths) == 18  # the snapshots of four projects, two of them with a comment file
    assert [path for path, _ in archived_requests(api)] == [ARCHIVED_PROJECTS, ARCHIVED_PROJECTS]
