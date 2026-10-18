import json
import re
from datetime import datetime, timezone
from importlib import resources

import pytest

from todoist_takeout import completed, storage
from todoist_takeout.api import Client
from todoist_takeout.resources import COLLECTION, KNOWN, ONE_OF, SINGLE, Classifier
from todoist_takeout.sync import apply_response, run_backup

from fake_api import ACTIVITIES, BY_COMPLETION_DATE, COMMENTS, FakeApi, completed_fixture


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


# --- the documented `sync/` and `completed/` layout ------------------------------


def _documented_paths(prefix):
    return sorted(set(re.findall(rf"`({prefix}/[^`]*\.json)`", _format_description())))


def _pattern(documented: str) -> re.Pattern:
    parts = re.split(r"(<[^>]+>)", documented)
    return re.compile("".join("[^/]+" if p.startswith("<") else re.escape(p) for p in parts))


def _produced_sync_tree(tmp_path, sync_fixture):
    for name in ["full_sync", "delete_stub", "delete_full", "delete_blanked"]:
        apply_response(tmp_path, sync_fixture(name), Classifier())


def _produced_completed_tree(tmp_path, sync_fixture):
    responses = {"*": json.dumps(sync_fixture("full_sync")).encode(), "FULL-1": b'{"sync_token": "INC-1"}'}
    api = FakeApi(responses)
    september = dict(zip(["since", "until"], completed.month_window("2026-09")))
    api.cursor_pages(BY_COMPLETION_DATE, september, [completed_fixture(f"by_completion_date_{i}") for i in (1, 2)])
    api.answer(COMMENTS, {"task_id": "6X8Gq4CfVh9xPm2R"}, completed_fixture("comments"))
    api.cursor_pages(ACTIVITIES, {}, [completed_fixture(f"activities_{i}") for i in (1, 2)])
    run_backup(tmp_path, Client(api), now=lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc))


LAYOUTS = {"sync": (_produced_sync_tree, 8), "completed": (_produced_completed_tree, 6)}


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request, tmp_path, sync_fixture):
    """A documented top-level directory, its documented paths and the paths a backup produces in it."""
    produce, minimum = LAYOUTS[request.param]
    produce(tmp_path, sync_fixture)
    prefix = request.param
    produced = sorted(
        path for p in tmp_path.rglob("*") if p.is_file() and (path := p.relative_to(tmp_path).as_posix()).startswith(f"{prefix}/")
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
