import json

import pytest

from todoist_takeout import storage
from todoist_takeout.api import Client, cursor_pages

from fake_api import ACTIVITIES, BY_COMPLETION_DATE, COMMENTS, COMPLETED_FIXTURES, FakeApi, completed_fixture

# --- 3.1 fixtures --------------------------------------------------------------------------

FIXTURE_NAMES = [
    "activities_1",
    "activities_2",
    "by_completion_date_1",
    "by_completion_date_2",
    "comments",
    "error_404",
    "error_410",
    "error_plan_restricted",
    "tasks_completed",
]


def test_every_fixture_parses_and_is_stored_in_the_backup_encoding():
    assert sorted(p.stem for p in COMPLETED_FIXTURES.glob("*.json")) == FIXTURE_NAMES
    for name in FIXTURE_NAMES:
        data = (COMPLETED_FIXTURES / f"{name}.json").read_bytes()
        assert storage.encode_json(json.loads(data)) == data, name


def test_fixtures_have_the_recorded_shapes():
    first, last = completed_fixture("by_completion_date_1"), completed_fixture("by_completion_date_2")
    assert list(first) == ["items", "next_cursor"] and list(last) == ["items"]
    backfill = completed_fixture("tasks_completed")
    assert list(backfill) == ["items", "projects", "sections"]
    assert all("item_object" in entry for entry in backfill["items"])
    [attached] = [c for c in completed_fixture("comments")["results"] if c["file_attachment"]]
    assert attached["file_attachment"]["file_name"] == "preise.pdf"
    for name, status in [("error_404", 404), ("error_410", 410), ("error_plan_restricted", 403)]:
        assert completed_fixture(name)["http_code"] == status


def test_large_event_ids_survive_parsing_and_encoding():
    data = (COMPLETED_FIXTURES / "activities_1.json").read_bytes()
    events = json.loads(data)["results"]
    assert all(event["id"] > 2**64 for event in events)
    assert events[0]["id"] == 152735830295847392018475629384756102938
    assert b"152735830295847392018475629384756102938" in storage.encode_json(events[0])
    assert json.loads(storage.encode_json(events)) == events


def test_activity_fixtures_hold_an_event_before_the_test_t0():
    t0 = "2026-10-18T10:00:05.123456Z"
    dates = [e["event_date"] for name in ["activities_1", "activities_2"] for e in completed_fixture(name)["results"]]
    assert any(d < t0 for d in dates) and any(d >= t0 for d in dates)


# --- 3.2 fake API ---------------------------------------------------------------------------


def test_fake_api_answers_by_path_and_parameters_and_records_requests():
    api = FakeApi({"*": b'{"sync_token": "S"}'})
    api.cursor_pages(BY_COMPLETION_DATE, {"since": "A"}, [completed_fixture("by_completion_date_1"), completed_fixture("by_completion_date_2")])
    api.answer(COMMENTS, {"task_id": "T"}, completed_fixture("error_404"), status=404)
    client = Client(api, sleep=lambda delay: None)

    assert client.sync("*") == b'{"sync_token": "S"}'
    pages = list(cursor_pages(client, BY_COMPLETION_DATE, {"since": "A"}, 200))
    assert [len(p["items"]) for p in pages] == [2, 1]
    assert list(cursor_pages(client, BY_COMPLETION_DATE, {"since": "B"}, 200)) == [{"items": []}]
    assert json.loads(client.get(ACTIVITIES, {})) == {"results": [], "next_cursor": None}
    with pytest.raises(Exception, match="HTTP 404"):
        client.get(COMMENTS, {"task_id": "T"})

    assert api.requests == ["*"]
    assert api.requested(BY_COMPLETION_DATE) == [
        {"since": "A", "limit": "200"},
        {"since": "A", "limit": "200", "cursor": "Y3Vyc29yOjI"},
        {"since": "B", "limit": "200"},
    ]


def test_fake_api_refuses_unknown_endpoints():
    with pytest.raises(AssertionError, match="projects/archived"):
        FakeApi().send("GET", "projects/archived", {})
