"""Offline checks of the API probe helpers in tests/contract/probes.py."""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from todoist_takeout.errors import ApiError


class FakeTransport:
    """Answers each request with the next of the given (status, body) pairs; records the requests."""

    def __init__(self, *answers, headers=None):
        self.answers = list(answers)
        self.requests = []
        self.headers = headers or {}

    def send(self, method, path, form):
        self.requests.append((method, path, dict(form)))
        status, body = self.answers.pop(0)
        return status, self.headers, json.dumps(body).encode()

    def query(self, index):
        parts = urlsplit(self.requests[index][1])
        return parts.path, {key: values[0] for key, values in parse_qs(parts.query).items()}


def api(probes, transport):
    return probes.Api(transport, sleep=lambda delay: None, jitter=lambda: 0.0)


def test_requests_are_sent_as_get_with_a_query_string(probes):
    transport = FakeTransport((200, {"items": []}))
    assert api(probes, transport).get("tasks/completed", {"since": "2026-10-01T00:00:00Z", "cursor": None}) == {"items": []}
    method, _, form = transport.requests[0]
    assert method == "GET" and form == {}
    assert transport.query(0) == ("tasks/completed", {"since": "2026-10-01T00:00:00Z"})


def test_rate_limits_are_retried_like_the_tools_requests(probes):
    transport = FakeTransport((429, {"error_extra": {"retry_after": 1}}), (200, {"ok": True}))
    assert api(probes, transport).get("projects/archived") == {"ok": True}
    assert len(transport.requests) == 2


def test_deprecation_headers_are_kept(probes):
    transport = FakeTransport((200, {}), headers={"Sunset": "x", "Content-Type": "application/json"})
    client = api(probes, transport)
    client.get("tasks/completed")
    assert client.notice_headers() == {"Sunset": "x"}


def test_walk_cursor_follows_next_cursor(probes):
    transport = FakeTransport(
        (200, {"results": [{"id": "1"}], "next_cursor": "c1"}),
        (200, {"results": [{"id": "2"}], "next_cursor": None}),
    )
    walk = probes.walk_cursor(api(probes, transport), "projects/archived", {}, items_key="results", limit=1)
    assert [p["id"] for p in walk.items] == ["1", "2"]
    assert walk.pages == 2 and walk.keys == ["next_cursor", "results"]
    assert "cursor" not in transport.query(0)[1] and transport.query(1)[1]["cursor"] == "c1"


def test_walk_offset_stops_at_a_short_page_and_merges_maps(probes):
    transport = FakeTransport(
        (200, {"items": [{"id": "1"}, {"id": "2"}], "projects": {"P1": {"id": "P1"}}, "sections": {}}),
        (200, {"items": [{"id": "3"}], "projects": {"P2": {"id": "P2"}}, "sections": {}}),
    )
    walk = probes.completed_all(api(probes, transport), limit=2, since="s")
    assert [i["id"] for i in walk.items] == ["1", "2", "3"]
    assert sorted(walk.maps["projects"]) == ["P1", "P2"]
    assert [transport.query(i)[1]["offset"] for i in range(2)] == ["0", "2"]


def test_month_helpers_cross_year_boundaries(probes):
    start = probes.month_start(datetime(2026, 1, 15, 12, tzinfo=timezone.utc))
    assert probes.add_months(start, -1) == datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert probes.add_months(start, 12) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert probes.month_window(probes.add_months(start, 1)) == ("2026-02-01T00:00:00Z", "2026-02-28T23:59:59Z")
    assert probes.months_between(datetime(2025, 11, 30, tzinfo=timezone.utc), start) == 2


def test_parse_time_accepts_fractions_and_z(probes):
    assert probes.parse_time("2026-10-05T08:12:33.123456Z") == datetime(2026, 10, 5, 8, 12, 33, 123456, tzinfo=timezone.utc)
    assert probes.parse_time("not a date") is None and probes.parse_time(None) is None


def test_field_analysis(probes):
    first = {"id": "T1", "completed_at": "a", "content": "x"}
    second = {"id": "T1", "completed_at": "b", "content": "x", "note_count": 0}
    assert probes.differing_fields([first, second]) == ["completed_at", "note_count"]
    assert probes.field_diff(first, second) == {
        "only_in_first": [], "only_in_second": ["note_count"], "different_values": ["completed_at"],
    }
    assert probes.id_field_stats([first, second, {"id": "T2", "task_id": "T1"}]) == {
        "id": {"present": 3, "distinct": 2}, "task_id": {"present": 1, "distinct": 1},
    }
    assert probes.overlap([{"task_id": "T1"}], [{"id": "T1"}, {"id": "T9"}]) == {"task_id~id": 1}
    assert probes.entries_for([first, {"id": "C1", "task_id": "T1"}, {"id": "T2"}], "T1") == [first, {"id": "C1", "task_id": "T1"}]


def test_describe_never_returns_data(probes):
    assert probes.describe({"checked": True, "completed_at": "2026-10-05", "content": "x"}, "checked", "completed_at") == {
        "present": True, "checked": True, "completed_at": "<string>",
    }
    assert probes.describe(None, "checked") == {"present": False}


def test_sections_record_api_errors_as_answers_and_other_errors_as_crashes(probes):
    sections, findings = probes.Sections(), {}
    with sections.run(findings, "answered"):
        raise ApiError("x", http_status=403, error="Forbidden", error_tag="PREMIUM_ONLY", error_code=1)
    with sections.run(findings, "crashed"):
        raise KeyError("items")
    assert findings["answered"]["api_error"]["error_tag"] == "PREMIUM_ONLY"
    assert findings["crashed"]["crashed"] == "KeyError: 'items'"
    assert sections.crashed == ["crashed"]


def test_poll_stops_when_done_or_at_the_timeout(probes):
    clock = iter([0.0, 1.0, 2.0, 0.0, 5.0, 10.0])
    values = iter([0, 1, 2])
    sleeps = []
    value, seconds, done = probes.poll(lambda: next(values), lambda v: v == 1, timeout=60, sleep=sleeps.append,
                                       clock=lambda: next(clock))
    assert (value, done, len(sleeps)) == (1, True, 1)
    value, seconds, done = probes.poll(lambda: 0, lambda v: False, timeout=10, sleep=sleeps.append, clock=lambda: next(clock))
    assert (done, seconds) == (False, 10.0)


def test_attempt_and_require(probes):
    def fail():
        raise ApiError("x", http_status=400, error="bad", error_tag="INVALID_ARGUMENT_VALUE", error_code=20)

    assert probes.attempt(lambda: 1) == {"ok": True, "value": 1}
    assert probes.attempt(fail)["error"]["error_code"] == 20
    with pytest.raises(probes.ProbeError, match="cannot continue: no task"):
        probes.require(None, "no task")


def test_activity_filter_picks_the_first_candidate_that_returns_only_completions(probes):
    bad_request = {"error": "bad", "error_tag": "INVALID_ARGUMENT_VALUE", "error_code": 20, "http_code": 400}
    transport = FakeTransport(
        (400, bad_request),
        (200, {"results": [{"object_type": "item", "event_type": "completed"},
                           {"object_type": "item", "event_type": "added"}]}),
        (200, {"results": [{"object_type": "item", "event_type": "completed"}]}),
    )
    chosen, report = probes.activity_filter(api(probes, transport), date_from="2026-10-01")
    assert chosen == probes.ACTIVITY_FILTERS[2]
    names = list(report)
    assert report[names[0]]["error_code"] == 20
    assert report[names[1]] == {"events": 2, "types": ["item:added", "item:completed"]}
    assert report[names[3]] == "not tried" and report[names[4]] == "not tried"
    assert transport.query(2)[1]["date_from"] == "2026-10-01"


def test_activity_filter_chooses_nothing_without_completions(probes):
    transport = FakeTransport(*[(200, {"results": []})] * len(probes.ACTIVITY_FILTERS))
    chosen, report = probes.activity_filter(api(probes, transport))
    assert chosen is None and all(entry == {"events": 0, "types": []} for entry in report.values())


def test_day_is_the_utc_date(probes):
    assert probes.day(datetime(2026, 10, 18, 23, 30, tzinfo=timezone.utc)) == "2026-10-18"


def test_time_change_compares_without_returning_values(probes):
    before = {"id": "T1", "updated_at": "2026-10-19T08:00:00.000000Z"}
    later = {"id": "T1", "updated_at": "2026-10-19T08:00:01.500000Z"}
    assert probes.time_change(before, later) == {"compared": True, "changed": True, "later": True}
    assert probes.time_change(later, before) == {"compared": True, "changed": True, "later": False}
    assert probes.time_change(before, dict(before)) == {"compared": True, "changed": False, "later": False}
    assert probes.time_change(before, None) == {"compared": False}
    assert probes.time_change({"id": "T1"}, later) == {"compared": False}


def test_booleans_only_removes_ids_timestamps_and_content(probes):
    findings = {
        "W8_archive": {
            "archive_accepted": True,
            "open_task_in_full": {"compared": True, "changed": False, "later": False},
            "archived_list": {"child": {"present": True, "is_archived": "<string>"}, "grandchild": {"present": False}},
            "project_id": "6X7fphhgwcXVGccJ",
            "updated_at": "2026-10-19T08:00:00.000000Z",
            "seen": [True, None, "content"],
            "count": 3,
        },
        "rejected_commands": [{"command": "project_archive", "error": {"error": "Invalid argument"}}],
        "W8_comment": {"crashed": "ProbeError: cannot continue: task not in the sync"},
    }
    copy, removed = probes.booleans_only(findings)
    assert removed == [
        "W8_archive.archived_list.child.is_archived", "W8_archive.project_id", "W8_archive.updated_at",
        "W8_archive.seen[2]", "W8_archive.count",
    ]
    assert copy["W8_archive"]["open_task_in_full"] == findings["W8_archive"]["open_task_in_full"]
    assert copy["W8_archive"]["seen"] == [True, None, "<removed>"]
    assert copy["rejected_commands"] == findings["rejected_commands"]
    assert copy["W8_comment"] == findings["W8_comment"]
    leaves = json.dumps({k: v for k, v in copy.items() if k not in probes.TEXT_KEYS})
    assert "6X7f" not in leaves and "2026" not in leaves and "content" not in leaves
