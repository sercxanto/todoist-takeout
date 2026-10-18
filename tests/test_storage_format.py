import json
import re
from importlib import resources

from todoist_takeout import storage
from todoist_takeout.resources import COLLECTION, KNOWN, ONE_OF, SINGLE, Classifier
from todoist_takeout.sync import apply_response


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


# --- 4.6 the documented `sync/` layout ----------------------------------------


def _documented_paths():
    return sorted(set(re.findall(r"`(sync/[^`]*\.json)`", _format_description())))


def _pattern(documented: str) -> re.Pattern:
    parts = re.split(r"(<[^>]+>)", documented)
    return re.compile("".join("[^/]+" if p.startswith("<") else re.escape(p) for p in parts))


def _produced_tree(tmp_path, sync_fixture):
    for name in ["full_sync", "delete_stub", "delete_full", "delete_blanked"]:
        apply_response(tmp_path, sync_fixture(name), Classifier())
    return sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())


def test_every_documented_path_occurs_in_a_produced_tree(tmp_path, sync_fixture):
    produced = _produced_tree(tmp_path, sync_fixture)
    documented = _documented_paths()
    assert len(documented) >= 8
    for path in documented:
        pattern = _pattern(path)
        assert any(pattern.fullmatch(p) for p in produced), path


def test_every_produced_path_matches_a_documented_pattern(tmp_path, sync_fixture):
    patterns = [_pattern(p) for p in _documented_paths() if "<" in p]
    for path in _produced_tree(tmp_path, sync_fixture):
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
