import json
import re
from datetime import UTC, datetime
from importlib import resources

import pytest
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

from todoist_takeout import archived, completed, storage
from todoist_takeout.api import Client
from todoist_takeout.resources import COLLECTION, KNOWN, ONE_OF, SINGLE, Classifier
from todoist_takeout.sync import apply_response, run_backup


def _format_description() -> str:
    return resources.files("todoist_takeout").joinpath("storage_format.md").read_text(encoding="utf-8")


def test_format_version_matches_code():
    versions = re.findall(r"^Format version: (\d+)$", _format_description(), re.MULTILINE)
    assert versions == [str(storage.FORMAT_VERSION)]


def test_state_example_uses_current_format_version():
    assert f'"format_version": {storage.FORMAT_VERSION},' in _format_description()


def test_state_example_holds_the_completed_object():
    [example] = re.findall(r"^## State file$.*?^```json$(.*?)^```$", _format_description(), re.M | re.S)
    state = json.loads(example.replace("…", "x"))
    assert list(state["completed"]) == list(storage.new_completed_state())
    table = re.findall(r"^\| `([a-z_0-9]+)` \|", _format_description().split("The `completed` object records")[1], re.M)
    assert table[: len(state["completed"])] == list(state["completed"])


def test_documented_names_match_the_encoding():
    text = " ".join(_format_description().split())
    for key, stem in [("Größe", "Gr%C3%B6%C3%9Fe"), ("a.b", "a%2Eb"), ("CON", "%43ON"), ("nul", "%6Eul")]:
        assert f"`{key}` becomes `{stem}`" in text
        assert storage.encode_key(key) == stem
    assert f"at most {storage.MAX_NAME_LENGTH}" in text


# --- the documented `sync/`, `completed/`, `archived/` and `attachments/` layout ---------


def _documented_paths(prefix):
    # Attachment files have any name; every other documented file is JSON. Directories end in `/`.
    ending = r"[^/`]" if prefix == "attachments" else r"\.json"
    return sorted(set(re.findall(rf"`({prefix}/[^`]*{ending})`", _format_description())))


def _pattern(documented: str) -> re.Pattern:
    parts = re.split(r"(<[^>]+>)", documented)
    return re.compile("".join("[^/]+" if p.startswith("<") else re.escape(p) for p in parts))


def _produced_sync_tree(tmp_path, sync_fixture):
    for name in ["full_sync", "delete_stub", "delete_full", "delete_blanked"]:
        apply_response(tmp_path, sync_fixture(name), Classifier())


def _produced_completed_tree(tmp_path, sync_fixture):
    responses = {"*": json.dumps(sync_fixture("full_sync")).encode(), "FULL-1": b'{"sync_token": "INC-1"}'}
    api = FakeApi(responses)
    september = dict(zip(["since", "until"], completed.month_window("2026-09"), strict=True))
    api.cursor_pages(BY_COMPLETION_DATE, september, [completed_fixture(f"by_completion_date_{i}") for i in (1, 2)])
    api.answer(COMMENTS, {"task_id": "6X8Gq4CfVh9xPm2R"}, completed_fixture("comments"))
    api.cursor_pages(ACTIVITIES, {}, [completed_fixture(f"activities_{i}") for i in (1, 2)])
    api.answer(TASKS_COMPLETED, september, completed_fixture("tasks_completed"))
    run_backup(tmp_path, Client(api), now=lambda: datetime(2026, 10, 18, 12, tzinfo=UTC))


def _produced_archived_tree(tmp_path, sync_fixture):
    """Two listed archived projects and the sub-project of one, with a commented task and a project comment."""
    responses = {"*": json.dumps(sync_fixture("full_sync")).encode(), "FULL-1": b'{"sync_token": "INC-1"}'}
    api = FakeApi(responses)
    api.cursor_pages(ARCHIVED_PROJECTS, {}, [archived_fixture("list_1"), archived_fixture("list_2")])
    for name in ("full_parent", "full_child"):
        full = archived_fixture(name)
        api.answer(archived.full_path(full["project"]["id"]), {}, full)
    trip = archived_fixture("list_2")["results"][0]
    api.answer(archived.full_path(trip["id"]), {}, {**archived_fixture("full_child"), "project": trip, "tasks": []})
    [commented] = [t for t in archived_fixture("full_parent")["tasks"] if t["note_count"]]
    api.answer(COMMENTS, {"task_id": commented["id"]}, archived_fixture("comments_task"))
    api.answer(COMMENTS, {"project_id": commented["project_id"]}, archived_fixture("comments_project"))
    run_backup(tmp_path, Client(api), now=lambda: datetime(2026, 10, 18, 12, tzinfo=UTC))


LAYOUTS = {
    "sync": (_produced_sync_tree, 8),
    "completed": (_produced_completed_tree, 8),
    "archived": (_produced_archived_tree, 7),
    # The archived tree holds the files of a comment in the sync and of one in a snapshot.
    "attachments": (_produced_archived_tree, 2),
}


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request, tmp_path, sync_fixture):
    """A documented top-level directory, its documented paths and the paths a backup produces in it."""
    produce, minimum = LAYOUTS[request.param]
    produce(tmp_path, sync_fixture)
    prefix = request.param
    produced = sorted(
        path
        for p in tmp_path.rglob("*")
        if p.is_file() and (path := p.relative_to(tmp_path).as_posix()).startswith(f"{prefix}/")
    )
    documented = _documented_paths(prefix)
    assert len(documented) >= minimum
    return documented, produced


def test_every_documented_path_occurs_in_a_produced_tree(layout):
    documented, produced = layout
    for path in documented:
        pattern = _pattern(path)
        assert any(pattern.fullmatch(p) for p in produced), path


def test_every_produced_path_matches_a_documented_pattern(layout):
    documented, produced = layout
    assert produced
    patterns = [_pattern(p) for p in documented if "<" in p]
    for path in produced:
        assert any(p.fullmatch(path) for p in patterns), path


def test_documented_kinds_match_the_resource_table():
    text = _format_description()
    composite = {}
    for names, fields in re.findall(r"^\| (`[a-z_]+`(?:, `[a-z_]+`)*) \| (`[a-z_]+`(?:, `[a-z_]+`)*) \|$", text, re.M):
        for name in re.findall(r"`([a-z_]+)`", names):
            composite[name] = tuple(re.findall(r"`([a-z_]+)`", fields))
    expected = {k: v.fields for k, v in KNOWN.items() if v.name == COLLECTION and v.fields != ("id",)}
    assert composite == expected

    def listed(heading):
        [block] = re.findall(rf"^{heading}: (.*?)\.$", text, re.M | re.S)
        return set(re.findall(r"`([a-z_]+)`", block))

    assert listed("Resources keyed by one of several fields") == {k for k, v in KNOWN.items() if v.name == ONE_OF}
    assert listed("Resources stored as single values") == {k for k, v in KNOWN.items() if v.name == SINGLE}


def test_description_warns_about_large_event_ids():
    text = " ".join(_format_description().split())
    assert "The `id` of an event is an integer larger than 64 bits" in text
    assert "JavaScript (`JSON.parse`) and an SQLite `INTEGER` column" in text


def test_description_names_every_copy_of_a_task_and_the_rule_for_the_current_one():
    [section] = re.findall(r"^## Copies of the same task$(.*?)^## ", _format_description(), re.M | re.S)
    text = " ".join(section.split())
    for place in [
        "`sync/items/<task id>.json`",
        "`completed/<YYYY-MM>.json`",
        "`archived/<project id>/full.json`",
        "`archived/<project id>/completed.json`",
    ]:
        assert f"| {place} |" in section, place
    for place in ["`completed/comments/<task id>.json`", "`archived/<project id>/comments/<task id>.json`"]:
        assert place in text, place
    assert "All copies share the task's `id`" in text
    assert (
        "The current copy is the one with the newest `updated_at`. If several copies have the same "
        "`updated_at`, a copy under `archived/` is current, then one under `completed/`, then one under "
        "`sync/`."
    ) in text


def _section(heading: str) -> str:
    [section] = re.findall(rf"^## {heading}$(.*?)^## ", _format_description(), re.M | re.S)
    return " ".join(section.split())


def test_description_tells_how_to_find_the_file_of_a_comment():
    text = _section("Attachment files")
    assert "The file attached to a stored comment is the only file in `attachments/<comment id>/`" in text
    assert "its original name is the comment's `file_attachment.file_name`" in text
    assert (
        "A missing `attachments/<comment id>/` directory" in text
        and "means that the comment's file is not downloaded" in text
    )
    for part in ["sync/", "completed/comments/", "archived/"]:
        assert f"`{part}`" in text


def test_documented_attachment_names_match_the_rules():
    text = _section("Attachment files")
    examples = re.findall(r"`([^`]+)` is stored as `([^`]+)`", text)
    assert len(examples) == 4
    for name, stored in examples:
        assert storage.attachment_file_name(name) == stored
    assert (
        f"longer than {storage.MAX_ATTACHMENT_NAME_CHARS} characters or {storage.MAX_ATTACHMENT_NAME_BYTES} bytes"
        in text
    )
    assert f"at most {storage.MAX_EXTENSION_LENGTH} characters" in text
    assert f"becomes `{storage.DEFAULT_ATTACHMENT_NAME}`" in text


def test_comment_sections_point_to_the_attachment_files():
    text = " ".join(_format_description().split())
    assert "not downloaded. On a plan" not in text and "files themselves are not downloaded" not in text
    for heading in ["Active data", "Completed tasks", "Archived projects"]:
        assert "[Attachment files](#attachment-files)" in _section(heading), heading
