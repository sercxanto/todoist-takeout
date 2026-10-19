import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todoist_takeout import archived, completed, storage
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import StorageError
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
FULL_KEYS = ["project", "tasks", "sections", "subprojects", "collaborators", "collaborator_states", "folder",
             "comments_count"]


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

# The windows of PARENT, created in October 2023, at NOW (design D7).
PARENT_WINDOWS = 13
# The account's first month of completed tasks: the sync fixtures' user joined in August 2026.
ACCOUNT_FIRST_MONTH = "2026-08"


def sync_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def account(*, plan=None):
    """The sync responses of the fixtures, optionally with changed plan flags."""
    full = json.loads(sync_bytes("full_sync"))
    full["user_plan_limits"]["current"].update(plan or {})
    unchanged = {"sync_token": "INC-1", "full_sync": False, "items": [], "projects": []}
    return {"*": json.dumps(full).encode(), "FULL-1": sync_bytes("incremental_edit"),
            "INC-1": json.dumps(unchanged).encode()}


def full_response(project, tasks=(), subprojects=()):
    return {"project": project, "tasks": list(tasks), "sections": [], "subprojects": list(subprojects),
            "collaborators": [], "collaborator_states": [], "folder": None, "comments_count": 0}


def windows(project, now=NOW):
    """The (since, until) windows of a project, as the tool computes them."""
    return list(archived._Backup(Path("."), None, {}, now, completed_tasks=True, comments=True)._windows(project))


def window_params(project_id, since_until):
    since, until = since_until
    return {"since": since, "until": until, "project_id": project_id}


def listing(api, *pages):
    """Answer the list of archived projects with `pages`, each a list of project payloads."""
    bodies = [{"results": list(page), "next_cursor": f"page{i + 2}" if i + 1 < len(pages) else None}
              for i, page in enumerate(pages)]
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
    return main(["backup", "--quiet", *args, str(target)], client_factory=lambda token, wait: Client(api, sleep=_no_sleep))


def snapshot_file(target, project_id, name):
    return json.loads((archived.snapshot_dir(target, project_id) / name).read_bytes())


def archived_files(target):
    root = target / "archived"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def archived_requests(api):
    """The GET requests of this part: the list, /full, and those filtered by a project or task."""
    return [(path, params) for path, params in api.get_requests
            if path == ARCHIVED_PROJECTS or path.endswith("/full") or "project_id" in params or "task_id" in params]


def completed_task(id, project_id, completed_at, note_count=0):
    entry = dict(archived_fixture("completed_2")["items"][0])
    entry.update(id=id, project_id=project_id, completed_at=completed_at, updated_at=completed_at,
                 note_count=note_count, content=f"task {id}")
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
    assert sorted(p.name for p in (tmp_path / "archived").iterdir()) == sorted([PARENT["id"], TRIP["id"]])


def test_without_archived_projects_only_the_list_is_requested(tmp_path, monkeypatch):
    api = FakeApi(account())
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)
    assert cli(tmp_path, api) == 0
    assert archived_requests(api) == [(ARCHIVED_PROJECTS, {"limit": "200"})]
    assert not (tmp_path / "archived").exists()


# Snapshot of an archived project


def test_snapshot_holds_the_open_tasks_and_sections_as_full_returned_them_and_the_completed_tasks(tmp_path):
    project = {**TRIP, "created_at": "2026-04-20T00:00:00.000000Z"}
    full = {**archived_fixture("full_parent"), "project": project, "subprojects": []}
    done = [completed_task(f"6Xt9Aa{n:010d}", project["id"], f"2026-0{5 + n % 5}-1{n % 10}T08:00:00.000000Z")
            for n in range(18)]
    api = listing(FakeApi(account()), [project])
    api.answer(archived.full_path(project["id"]), {}, full)
    for since_until in windows(project):
        months = window_params(project["id"], since_until)
        inside = [t for t in done if months["since"] <= t["completed_at"] <= months["until"]]
        api.cursor_pages(BY_COMPLETION_DATE, months, [{"items": inside[:5], "next_cursor": "more"}, {"items": inside[5:]}])

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
    pair = [comment("6Xc9Cc0000000002", done["id"], "2024-03-19T10:00:00.000000Z"),
            comment("6Xc9Cc0000000001", done["id"], "2024-03-18T10:00:00.000000Z")]
    api.answer(COMMENTS, {"task_id": done["id"]}, {"results": pair, "next_cursor": None})

    run(tmp_path, api)

    listed = archived_fixture("comments_task")["results"]
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json") == {
        "results": sorted(listed, key=lambda c: c["posted_at"])}  # without the cursor
    assert snapshot_file(tmp_path, PARENT["id"], f"comments/{done['id']}.json")["results"] == pair[::-1]
    assert sorted(p["task_id"] for p in api.requested(COMMENTS) if "task_id" in p) == sorted([TILES, done["id"]])


def test_project_comments_are_stored(tmp_path):
    api = run(tmp_path)
    expected = archived_fixture("comments_project")
    del expected["next_cursor"]
    assert snapshot_file(tmp_path, PARENT["id"], "project_comments.json") == expected
    assert snapshot_file(tmp_path, TRIP["id"], "project_comments.json") == {"results": []}
    assert [p["project_id"] for p in api.requested(COMMENTS) if "project_id" in p] == [PARENT["id"], TRIP["id"]]


# Completed tasks over the project's lifetime


def test_windows_cover_three_months_each_from_the_creation_month_through_the_current_one():
    found = windows({"created_at": "2023-10-05T09:00:00.000000Z"})
    assert len(found) == PARENT_WINDOWS
    assert found[0] == ("2023-10-01T00:00:00.000000Z", "2023-12-31T23:59:59.999999Z")
    assert found[-1] == ("2026-10-01T00:00:00.000000Z", "2026-12-31T23:59:59.999999Z")
    for (since, until), (next_since, _) in zip(found, found[1:]):
        assert completed.parse_time(next_since) - completed.parse_time(until) == timedelta(microseconds=1)
    assert max(completed.parse_time(u) - completed.parse_time(s) for s, u in found) < timedelta(days=92)


def test_a_window_ending_in_the_current_month_is_the_last():
    assert windows({"created_at": "2026-08-31T23:00:00.000000Z"}) == [
        ("2026-08-01T00:00:00.000000Z", "2026-10-31T23:59:59.999999Z")]


def test_completed_tasks_are_requested_in_thirteen_windows(tmp_path):
    api = run(tmp_path)
    requested = [p for p in api.requested(BY_COMPLETION_DATE) if p.get("project_id") == PARENT["id"]]
    firsts = [p for p in requested if "cursor" not in p]
    assert len(firsts) == PARENT_WINDOWS
    assert firsts[0]["since"] == "2023-10-01T00:00:00.000000Z"
    assert firsts[-1]["until"] == "2026-12-31T23:59:59.999999Z"
    assert len(requested) == PARENT_WINDOWS + 1  # the second window has two pages
    stored = snapshot_file(tmp_path, PARENT["id"], "completed.json")
    assert list(stored) == ["items"]
    assert [t["completed_at"] for t in stored["items"]] == sorted(t["completed_at"] for t in stored["items"])
    assert len(stored["items"]) == 3


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
    [stored] = [c for c in snapshot_file(tmp_path, PARENT["id"], f"comments/{TILES}.json")["results"]
                if c["file_attachment"]]
    [listed] = [c for c in archived_fixture("comments_task")["results"] if c["file_attachment"]]
    assert stored == listed
    paths = {path for path, _ in api.get_requests}
    assert paths <= {ARCHIVED_PROJECTS, BY_COMPLETION_DATE, COMMENTS, ACTIVITIES, TASKS_COMPLETED,
                     *(archived.full_path(p["id"]) for p in (PARENT, TRIP))}


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
    parent, trip = PARENT["id"], TRIP["id"]
    assert archived_files(tmp_path) == sorted([
        f"{parent}/project.json", f"{parent}/full.json", f"{parent}/completed.json",
        f"{parent}/project_comments.json", f"{parent}/comments/{TILES}.json",
        f"{trip}/project.json", f"{trip}/full.json", f"{trip}/completed.json", f"{trip}/project_comments.json",
    ])
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
    assert [(p[len(parent):], full) for p, full in writes if p.startswith(parent)] == [
        (f"comments/{TILES}.json", False),
        ("project_comments.json", False),
        ("completed.json", False),
        ("full.json", False),
        ("project.json", True),
    ]


@pytest.mark.parametrize("name, stored", [
    ("full.json", {**archived_fixture("full_parent"), "project": {**PARENT, "id": PARENT["id"].lower()}}),
])
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
