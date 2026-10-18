import json
import logging
from pathlib import Path

import pytest

from todoist_takeout import resources
from todoist_takeout.resources import COLLECTION, META, ONE_OF, SINGLE, Classifier

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"

EXPECTED = {
    "sync_token": (META, ()),
    "full_sync": (META, ()),
    "full_sync_date_utc": (META, ()),
    "temp_id_mapping": (META, ()),
    "items": (COLLECTION, ("id",)),
    "projects": (COLLECTION, ("id",)),
    "sections": (COLLECTION, ("id",)),
    "labels": (COLLECTION, ("id",)),
    "notes": (COLLECTION, ("id",)),
    "project_notes": (COLLECTION, ("id",)),
    "filters": (COLLECTION, ("id",)),
    "reminders": (COLLECTION, ("id",)),
    "collaborators": (COLLECTION, ("id",)),
    "live_notifications": (COLLECTION, ("id",)),
    "collaborator_states": (COLLECTION, ("project_id", "user_id")),
    "view_options": (COLLECTION, ("view_type", "object_id")),
    "project_view_options_defaults": (COLLECTION, ("project_id",)),
    "workspace_users": (COLLECTION, ("workspace_id", "user_id")),
    "workspaces": (COLLECTION, ("id",)),
    "completed_info": (ONE_OF, ("project_id", "section_id", "item_id")),
    "user": (SINGLE, ()),
    "user_settings": (SINGLE, ()),
    "user_plan_limits": (SINGLE, ()),
    "settings_notifications": (SINGLE, ()),
    "stats": (SINGLE, ()),
    "day_orders": (SINGLE, ()),
    "locations": (SINGLE, ()),
    "role_actions": (SINGLE, ()),
    "live_notifications_last_read_id": (SINGLE, ()),
    # unknown keys, classified by shape
    "brand_new_things": (COLLECTION, ("id",)),
    "brand_new_setting": (SINGLE, ()),
}


def _fixture_files():
    return sorted(SYNC_FIXTURES.glob("*.json"))


def test_fixtures_exist():
    names = {p.stem for p in _fixture_files()}
    assert names == {
        "full_sync",
        "incremental_edit",
        "delete_full",
        "delete_stub",
        "delete_blanked",
        "unknown_key",
        "plan_limits_no_comments",
        "server_full_sync",
    }


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture_parses_as_json(path):
    assert isinstance(json.loads(path.read_bytes()), dict)


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_every_fixture_key_is_classified_as_expected(path):
    classifier = Classifier()
    for key, value in json.loads(path.read_bytes()).items():
        kind = classifier.classify(key, value)
        assert (kind.name, kind.fields) == EXPECTED[key], key


def test_known_keys_are_classified_without_warning(sync_fixture, caplog):
    with caplog.at_level(logging.WARNING):
        classifier = Classifier()
        for key, value in sync_fixture("full_sync").items():
            classifier.classify(key, value)
    assert caplog.records == []


def test_both_spellings_of_inconsistent_keys_are_known():
    for a, b in [
        ("collaborator_states", "collaborators_states"),
        ("notification_settings", "settings_notifications"),
        ("live_notifications_last_read_id", "live_notifications_last_read"),
    ]:
        assert resources.KNOWN[a] == resources.KNOWN[b]


def test_unknown_key_logs_exactly_one_warning(caplog):
    classifier = Classifier()
    value = [{"id": "N1"}]
    with caplog.at_level(logging.WARNING):
        classifier.classify("brand_new_things", value)
        classifier.classify("brand_new_things", value)
    [record] = caplog.records
    assert record.levelname == "WARNING"
    assert "'brand_new_things'" in record.getMessage()


def test_unknown_array_without_ids_is_single():
    kind = Classifier().classify("brand_new_list", [{"name": "x"}])
    assert kind.name == SINGLE


def test_known_key_with_unexpected_shape_falls_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        kind = Classifier().classify("collaborators", {"2671362": {"id": "2671362"}})
    assert kind.name == SINGLE
    [record] = caplog.records
    assert "unexpected shape" in record.getMessage()
    assert "'collaborators'" in record.getMessage()


def test_workspaces_given_as_mapping_falls_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        kind = Classifier().classify("workspaces", {"28173": {"id": "28173"}})
    assert kind.name == SINGLE
    [record] = caplog.records
    assert "unexpected shape" in record.getMessage()


def test_one_of_key_is_the_present_field_and_its_value():
    kind = resources.KNOWN["completed_info"]
    assert resources.object_key(kind, {"project_id": "P1", "completed_items": 3}) == ("project_id", "P1")
    assert resources.object_key(kind, {"section_id": 7, "project_id": None}) == ("section_id", "7")
    assert resources.object_key(kind, {"item_id": "T1", "completed_items": 1}) == ("item_id", "T1")
    assert resources.object_key(kind, {"completed_items": 3}) is None
    assert resources.object_key(kind, {"project_id": "P1", "section_id": "S1"}) is None


@pytest.mark.parametrize(
    "entry", [{"completed_items": 3}, {"project_id": "P1", "section_id": "S1"}, {"section_id": "S1", "item_id": "T1"}]
)
def test_one_of_entry_with_neither_or_both_fields_falls_back_with_one_warning(entry, caplog):
    classifier = Classifier()
    with caplog.at_level(logging.WARNING):
        kind = classifier.classify("completed_info", [{"project_id": "P1"}, entry])
        classifier.classify("completed_info", [entry])
    assert kind.name == SINGLE
    [record] = caplog.records
    assert "'completed_info'" in record.getMessage() and "one of project_id | section_id | item_id" in record.getMessage()


RECORDED = sorted((Path(__file__).parent / "fixtures" / "recorded").glob("*/*.json"))


@pytest.mark.parametrize("path", RECORDED, ids=[p.relative_to(p.parents[1]).as_posix() for p in RECORDED])
def test_recorded_keys_are_classified_without_fallback(path, caplog):
    classifier = Classifier()
    with caplog.at_level(logging.WARNING):
        for key, value in json.loads(path.read_bytes()).items():
            kind = classifier.classify(key, value)
            assert resources.KNOWN.get(key) == kind, key
    assert not caplog.records


def test_integer_key_fields_become_strings():
    kind = resources.KNOWN["workspace_users"]
    assert resources.object_key(kind, {"workspace_id": 28173, "user_id": "7"}) == ("28173", "7")
    assert resources.object_key(kind, {"workspace_id": True, "user_id": "7"}) is None
