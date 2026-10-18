import re
from importlib import resources

from todoist_takeout import storage


def _format_description() -> str:
    return resources.files("todoist_takeout").joinpath("storage_format.md").read_text(encoding="utf-8")


def test_format_version_matches_code():
    versions = re.findall(r"^Format version: (\d+)$", _format_description(), re.MULTILINE)
    assert versions == [str(storage.FORMAT_VERSION)]


def test_state_example_uses_current_format_version():
    assert f'"format_version": {storage.FORMAT_VERSION},' in _format_description()


def test_documented_names_match_the_encoding():
    text = " ".join(_format_description().split())
    for key, stem in [("Größe", "Gr%C3%B6%C3%9Fe"), ("a.b", "a%2Eb"), ("CON", "%43ON"), ("nul", "%6Eul")]:
        assert f"`{key}` becomes `{stem}`" in text
        assert storage.encode_key(key) == stem
    assert f"at most {storage.MAX_NAME_LENGTH}" in text
