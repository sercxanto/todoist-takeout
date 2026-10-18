import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todoist_takeout import completed, storage, sync
from todoist_takeout.api import Client, cursor_pages
from todoist_takeout.cli import main
from todoist_takeout.errors import ConfigError, StorageError
from todoist_takeout.sync import run_backup

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


# --- group 4: completed-task months and their comments ----------------------------------------

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
OLD = 1_000_000_000

# The sync fixtures: the account was created on 2026-08-14.
FIRST_MONTHS = ["2026-08", "2026-09", "2026-10"]
TASK_WITH_COMMENTS = "6X8Gq4CfVh9xPm2R"


def sync_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def unchanged(token):
    return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()


def account(*, plan=None, user=None):
    """The sync responses of the fixtures, optionally with changed plan flags or user fields."""
    full = json.loads(sync_bytes("full_sync"))
    full["user_plan_limits"]["current"].update(plan or {})
    full["user"].update(user or {})
    full["user"] = {k: v for k, v in full["user"].items() if v is not None}
    return {"*": json.dumps(full).encode(), "FULL-1": sync_bytes("incremental_edit"), "INC-1": unchanged("INC-1")}


def window(month):
    since, until = completed.month_window(month)
    return {"since": since, "until": until}


def september(api, pages=None):
    """Let the API return the fixture tasks of 2026-09, in two pages, and the comments of one of them."""
    pages = pages or [completed_fixture("by_completion_date_1"), completed_fixture("by_completion_date_2")]
    api.cursor_pages(BY_COMPLETION_DATE, window("2026-09"), pages)
    api.answer(COMMENTS, {"task_id": TASK_WITH_COMMENTS}, completed_fixture("comments"))
    return api


def _no_sleep(delay):
    raise AssertionError("the run should not wait")


def run(target, api=None, now=NOW):
    api = api if api is not None else FakeApi(account())
    run_backup(target, Client(api, sleep=_no_sleep), now=lambda: now)
    return api


def months_requested(api):
    return [p["since"][:7] for p in api.requested(BY_COMPLETION_DATE) if "cursor" not in p]


def progress(target):
    return json.loads((target / "state.json").read_bytes()).get("completed")


def stored(target, path):
    return json.loads((target / "completed" / path).read_bytes())


def completed_tree(target):
    root = target / "completed"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def task(id, completed_at, note_count=0, **fields):
    return {"id": id, "content": f"task {id}", "completed_at": completed_at, "note_count": note_count, **fields}


@pytest.fixture(autouse=True)
def _disarm_fault_hook():
    yield
    storage.set_fault_after(None)


# --- 4.1 start of this part -------------------------------------------------------------------


def test_t0_and_first_month_are_written_before_any_request_of_this_part(tmp_path):
    seen = []

    class Watching(FakeApi):
        def send(self, method, path, params):
            if method == "GET":
                seen.append(progress(tmp_path))
            return super().send(method, path, params)

    run(tmp_path, Watching(account()))
    assert seen[0]["t0_utc"] == "2026-10-18T12:00:00.000000Z"
    assert seen[0]["first_month"] == "2026-08"
    assert seen[0]["tasks_closed_through"] is None


def test_second_run_keeps_t0_and_first_month(tmp_path):
    run(tmp_path)
    run(tmp_path, now=NOW + timedelta(days=40))
    assert progress(tmp_path)["t0_utc"] == "2026-10-18T12:00:00.000000Z"
    assert progress(tmp_path)["first_month"] == "2026-08"


@pytest.mark.parametrize("joined_at", [None, "yesterday"])
def test_unknown_creation_date_starts_in_2007(tmp_path, caplog, joined_at):
    with caplog.at_level(logging.INFO, logger="todoist_takeout.completed"):
        api = run(tmp_path, FakeApi(account(user={"joined_at": joined_at})))
    assert progress(tmp_path)["first_month"] == "2007-01"
    assert "the account's creation date is unknown; completed tasks are backed up from 2007-01 on" in caplog.messages
    requested = months_requested(api)
    assert requested[0] == "2007-01" and requested[-1] == "2026-10" and len(requested) == 19 * 12 + 10


def test_plan_without_completed_tasks_warns_once_and_makes_no_request(tmp_path, monkeypatch, capsys):
    api = FakeApi(account(plan={"completed_tasks": False}))
    monkeypatch.setenv("TODOIST_API_TOKEN", "0123456789abcdef0123456789abcdef01234567")
    status = main(["backup", "--quiet", str(tmp_path)], client_factory=lambda token, wait: Client(api, sleep=_no_sleep))
    assert status == 0
    assert capsys.readouterr().err.splitlines() == [
        "WARNING the Todoist plan 'free' does not include completed_tasks; completed tasks, completion events and "
        "their comments cannot be backed up on this plan"
    ]
    assert api.get_requests == []
    assert progress(tmp_path) is None
    assert not (tmp_path / "completed").exists()
    assert (tmp_path / "sync" / "items" / "6X7rM8997g3RQmvh.json").is_file()


# --- 4.2 merging pages ------------------------------------------------------------------------


def test_pages_are_concatenated_and_sorted_by_time_then_id():
    pages = [
        {"items": [task("c", "2026-09-03T00:00:00.000000Z"), task("b", "2026-09-01T00:00:00.000000Z")], "next_cursor": "2"},
        {"items": [task("a", "2026-09-03T00:00:00.000000Z")], "next_cursor": "3"},
        {"items": [task("d", "2026-09-02T00:00:00.000000Z")]},
    ]
    merged = completed.merge_pages(pages, "items", "completed_at")
    assert list(merged) == ["items"]
    assert [t["id"] for t in merged["items"]] == ["b", "d", "a", "c"]
    assert merged["items"][0] is pages[0]["items"][1]  # entries unchanged


def test_maps_are_merged_and_other_keys_take_the_last_value():
    pages = [
        {"items": [], "projects": {"P1": {"name": "old"}, "P2": {"name": "two"}}, "sections": {}, "total": 1, "next_cursor": "x"},
        {"items": [], "projects": {"P1": {"name": "new"}, "P3": {"name": "three"}}, "sections": {"S": {}}, "total": 2},
    ]
    merged = completed.merge_pages(pages, "items", "completed_at")
    assert merged == {
        "items": [],
        "projects": {"P1": {"name": "new"}, "P2": {"name": "two"}, "P3": {"name": "three"}},
        "sections": {"S": {}},
        "total": 2,
    }
    assert list(merged) == ["items", "projects", "sections", "total"]


def test_entry_without_time_sorts_first_and_integer_ids_compare_as_integers():
    entries = [
        {"id": 10, "event_date": "2026-10-01T00:00:00.000000Z"},
        {"id": 9, "event_date": "2026-10-01T00:00:00.000000Z"},
        {"id": 2**100, "event_date": "2026-10-01T00:00:00.000000Z"},
        {"id": 11},
    ]
    merged = completed.merge_pages([{"results": entries}], "results", "event_date")
    assert [e["id"] for e in merged["results"]] == [11, 9, 10, 2**100]


def test_page_order_does_not_change_the_bytes():
    first, last = completed_fixture("by_completion_date_1"), completed_fixture("by_completion_date_2")
    one = completed.merge_pages([first, last], "items", "completed_at")
    other = completed.merge_pages([last, first], "items", "completed_at")
    assert storage.encode_json(one) == storage.encode_json(other)
    assert [t["completed_at"] for t in one["items"]] == sorted(t["completed_at"] for t in one["items"])


def test_merge_by_id_lets_a_new_entry_replace_a_stored_one():
    stored = {"results": [{"id": "1", "content": "old", "posted_at": "2026-09-01T00:00:00.000000Z"},
                          {"id": "2", "content": "kept", "posted_at": "2026-09-02T00:00:00.000000Z"}]}
    new = {"results": [{"id": "1", "content": "edited", "posted_at": "2026-09-01T00:00:00.000000Z"}], "next_cursor": None}
    merged = completed.merge_pages([stored, new], "results", "posted_at", by_id=True)
    assert merged == {"results": [{"id": "1", "content": "edited", "posted_at": "2026-09-01T00:00:00.000000Z"},
                                  {"id": "2", "content": "kept", "posted_at": "2026-09-02T00:00:00.000000Z"}]}


def test_empty_fetch_gives_an_empty_list():
    assert completed.merge_pages([{"items": []}], "items", "completed_at") == {"items": []}
    assert completed.merge_pages([], "items", "completed_at") == {"items": []}


# --- 4.3 months -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month, since, until",
    [
        ("2026-09", "2026-09-01T00:00:00.000000Z", "2026-09-30T23:59:59.999999Z"),
        ("2024-02", "2024-02-01T00:00:00.000000Z", "2024-02-29T23:59:59.999999Z"),
        ("2026-12", "2026-12-01T00:00:00.000000Z", "2026-12-31T23:59:59.999999Z"),
    ],
)
def test_month_window_spans_the_whole_month(month, since, until):
    assert completed.month_window(month) == (since, until)


def test_first_run_fetches_every_month_through_the_current_one(tmp_path):
    api = run(tmp_path, september(FakeApi(account())))
    assert months_requested(api) == FIRST_MONTHS
    assert api.requested(BY_COMPLETION_DATE)[0] == {**window("2026-08"), "limit": "200"}
    assert set(completed_tree(tmp_path)) == {"2026-08.json", "2026-09.json", "2026-10.json", f"comments/{TASK_WITH_COMMENTS}.json"}
    assert completed_tree(tmp_path)["2026-08.json"] == b'{\n  "items": []\n}\n'
    assert progress(tmp_path)["tasks_closed_through"] == "2026-09"


def test_month_boundary_and_several_pages(tmp_path):
    api = run(tmp_path, september(FakeApi(account())))
    assert window("2026-09")["until"] == "2026-09-30T23:59:59.999999Z"
    assert window("2026-10")["since"] == "2026-10-01T00:00:00.000000Z"
    september_tasks = stored(tmp_path, "2026-09.json")
    assert list(september_tasks) == ["items"]  # no next_cursor
    pages = completed_fixture("by_completion_date_1")["items"] + completed_fixture("by_completion_date_2")["items"]
    assert sorted(september_tasks["items"], key=lambda t: t["id"]) == sorted(pages, key=lambda t: t["id"])
    assert september_tasks["items"][-1]["completed_at"] == "2026-09-30T23:59:59.999999Z"
    assert stored(tmp_path, "2026-10.json") == {"items": []}
    assert len(api.requested(BY_COMPLETION_DATE)) == 4  # 2026-09 in two pages


def test_month_with_450_tasks_in_three_pages(tmp_path):
    tasks = [task(f"T{i:03d}", f"2026-09-{1 + i % 30:02d}T08:00:00.000000Z") for i in range(450)]
    pages = [{"items": tasks[i : i + 200], "next_cursor": f"c{i}"} for i in range(0, 400, 200)] + [{"items": tasks[400:]}]
    run(tmp_path, september(FakeApi(account()), pages))
    assert len(stored(tmp_path, "2026-09.json")["items"]) == 450


def test_month_within_the_grace_period_stays_open(tmp_path):
    api = run(tmp_path, now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    assert months_requested(api) == FIRST_MONTHS
    assert progress(tmp_path)["tasks_closed_through"] == "2026-08"
    api = run(tmp_path, now=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
    assert months_requested(api) == ["2026-09", "2026-10"]


def test_month_closes_after_the_grace_period_and_is_not_fetched_again(tmp_path):
    run(tmp_path, now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    api = run(tmp_path, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert months_requested(api) == ["2026-09", "2026-10"]
    assert progress(tmp_path)["tasks_closed_through"] == "2026-09"
    api = run(tmp_path, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert months_requested(api) == ["2026-10"]


def test_historical_months_close_as_they_are_stored(tmp_path):
    seen = {}

    class Watching(FakeApi):
        def send(self, method, path, params):
            if path == BY_COMPLETION_DATE:
                seen[params["since"][:7]] = progress(tmp_path)["tasks_closed_through"]
            return super().send(method, path, params)

    run(tmp_path, Watching(account(user={"joined_at": "2026-05-02T00:00:00.000000Z"})))
    assert seen == {"2026-05": None, "2026-06": "2026-05", "2026-07": "2026-06", "2026-08": "2026-07",
                    "2026-09": "2026-08", "2026-10": "2026-09"}


def test_second_run_requests_only_open_months(tmp_path):
    run(tmp_path)
    api = run(tmp_path, now=NOW + timedelta(hours=1))
    assert months_requested(api) == ["2026-10"]
    api = run(tmp_path, now=datetime(2026, 12, 7, tzinfo=timezone.utc))
    assert months_requested(api) == ["2026-10", "2026-11", "2026-12"]
    assert progress(tmp_path)["tasks_closed_through"] == "2026-10"


def test_interrupted_first_run_continues_with_the_first_month_not_closed(tmp_path):
    joined = {"joined_at": "2026-05-02T00:00:00.000000Z"}

    class Crashing(FakeApi):
        def send(self, method, path, params):
            if path == BY_COMPLETION_DATE and params["since"].startswith("2026-08"):
                storage.set_fault_after(0)
            return super().send(method, path, params)

    with pytest.raises(storage.InjectedFault):
        run(tmp_path, Crashing(account(user=joined)))
    assert progress(tmp_path)["tasks_closed_through"] == "2026-07"
    api = run(tmp_path, FakeApi(account(user=joined)))
    assert months_requested(api) == ["2026-08", "2026-09", "2026-10"]


def test_open_month_follows_completions_and_uncompletions(tmp_path):
    october = window("2026-10")
    first = FakeApi(account()).answer(BY_COMPLETION_DATE, october, {"items": [task("X", "2026-10-02T09:00:00.000000Z"), task("Y", "2026-10-03T09:00:00.000000Z")]})
    run(tmp_path, september(first))
    # Y was uncompleted, Z completed since; September is closed and not fetched again, though one of
    # its tasks was uncompleted and completed again in October.
    reopened = {**completed_fixture("by_completion_date_2")["items"][0], "completed_at": "2026-10-19T10:00:00.000000Z"}
    later = FakeApi(account()).answer(BY_COMPLETION_DATE, october, {"items": [task("Z", "2026-10-18T13:00:00.000000Z"), task("X", "2026-10-02T09:00:00.000000Z"), reopened]})
    september_before = completed_tree(tmp_path)["2026-09.json"]
    run(tmp_path, later, now=datetime(2026, 10, 19, 11, tzinfo=timezone.utc))
    assert [t["id"] for t in stored(tmp_path, "2026-10.json")["items"]] == ["X", "Z", reopened["id"]]
    assert completed_tree(tmp_path)["2026-09.json"] == september_before
    assert reopened["id"] in [t["id"] for t in stored(tmp_path, "2026-09.json")["items"]]


def test_unchanged_open_month_is_not_rewritten(tmp_path):
    run(tmp_path, september(FakeApi(account())), now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    for path in (tmp_path / "completed").rglob("*.json"):
        os.utime(path, ns=(OLD, OLD))
    before = completed_tree(tmp_path)
    run(tmp_path, september(FakeApi(account())), now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert completed_tree(tmp_path) == before
    assert all(p.stat().st_mtime_ns == OLD for p in (tmp_path / "completed").rglob("*.json"))


def test_plan_restriction_of_completed_tasks_warns_and_the_run_succeeds(tmp_path, caplog):
    api = FakeApi(account()).answer(BY_COMPLETION_DATE, {}, completed_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "the Todoist plan does not allow fetching completed tasks; they cannot be backed up on this plan"
    ]
    assert months_requested(api) == ["2026-08"]
    assert progress(tmp_path)["tasks_closed_through"] is None
    assert json.loads((tmp_path / "state.json").read_bytes())["last_successful_run_utc"] is not None


def test_invalid_month_in_the_state_is_a_config_error(tmp_path):
    run(tmp_path)
    state = json.loads((tmp_path / "state.json").read_bytes())
    state["completed"]["tasks_closed_through"] = "2026-9"
    (tmp_path / "state.json").write_bytes(storage.encode_json(state))
    with pytest.raises(ConfigError, match="tasks_closed_through '2026-9' is not a month"):
        run(tmp_path)


# --- 4.4 comments of completed tasks ---------------------------------------------------------


def comments_file(target):
    return stored(target, f"comments/{TASK_WITH_COMMENTS}.json")


def test_comments_of_a_task_are_stored_with_their_attachment(tmp_path):
    api = run(tmp_path, september(FakeApi(account())))
    assert api.requested(COMMENTS) == [{"task_id": TASK_WITH_COMMENTS, "limit": "100"}]
    fixture = completed_fixture("comments")["results"]
    assert comments_file(tmp_path) == {"results": [fixture[1], fixture[0]]}  # ordered by posted_at
    assert comments_file(tmp_path)["results"][0]["file_attachment"] == fixture[1]["file_attachment"]


def test_comments_are_written_before_the_month_file(tmp_path, monkeypatch):
    written = []
    real = storage.write_bytes_atomic

    def write(path, data, **kwargs):
        written.append(Path(path).relative_to(tmp_path).as_posix())
        return real(path, data, **kwargs)

    monkeypatch.setattr(storage, "write_bytes_atomic", write)
    run(tmp_path, september(FakeApi(account())))
    assert written.index(f"completed/comments/{TASK_WITH_COMMENTS}.json") < written.index("completed/2026-09.json")


def test_task_without_comments_gets_no_comment_request(tmp_path):
    api = run(tmp_path, september(FakeApi(account())))
    assert all(p["task_id"] == TASK_WITH_COMMENTS for p in api.requested(COMMENTS))


def test_plan_without_comments_gets_no_comment_request(tmp_path):
    api = run(tmp_path, september(FakeApi(account(plan={"comments": False}))))
    assert api.requested(COMMENTS) == []
    assert "comments" not in {p.parent.name for p in (tmp_path / "completed").rglob("*.json")}
    assert len(stored(tmp_path, "2026-09.json")["items"]) == 3


def october_api(note_count, comments):
    api = FakeApi(account())
    api.answer(BY_COMPLETION_DATE, window("2026-10"), {"items": [task(TASK_WITH_COMMENTS, "2026-10-02T09:00:00.000000Z", note_count)]})
    api.answer(COMMENTS, {"task_id": TASK_WITH_COMMENTS}, {"results": comments, "next_cursor": None})
    return api


def comment(id, posted_at, content="text"):
    return {"id": id, "item_id": TASK_WITH_COMMENTS, "content": content, "posted_at": posted_at}


C1 = comment("C1", "2026-10-02T08:00:00.000000Z")
C2 = comment("C2", "2026-10-02T08:30:00.000000Z")
C3 = comment("C3", "2026-10-18T13:00:00.000000Z")


def test_comment_added_in_an_open_month_is_stored_next_to_the_others(tmp_path):
    run(tmp_path, october_api(2, [C1, C2]))
    api = run(tmp_path, october_api(3, [C1, C2, C3]), now=NOW + timedelta(days=1))
    assert len(api.requested(COMMENTS)) == 1
    assert comments_file(tmp_path)["results"] == [C1, C2, C3]


def test_comment_deleted_in_an_open_month_is_kept(tmp_path):
    run(tmp_path, october_api(2, [C1, C2]))
    run(tmp_path, october_api(1, [C2]), now=NOW + timedelta(days=1))
    assert comments_file(tmp_path)["results"] == [C1, C2]


def test_edited_comment_replaces_the_stored_one(tmp_path):
    run(tmp_path, october_api(2, [C1, C2]))
    edited = {**C2, "content": "edited"}
    run(tmp_path, october_api(3, [C1, edited, C3]), now=NOW + timedelta(days=1))
    assert comments_file(tmp_path)["results"] == [C1, edited, C3]


def test_unchanged_note_count_gets_no_comment_request(tmp_path):
    run(tmp_path, october_api(2, [C1, C2]))
    api = run(tmp_path, october_api(2, [C1, C2, C3]), now=NOW + timedelta(days=1))
    assert api.requested(COMMENTS) == []
    assert comments_file(tmp_path)["results"] == [C1, C2]


def test_comments_of_closed_months_are_not_fetched_again(tmp_path):
    run(tmp_path, september(FakeApi(account())))
    later = september(FakeApi(account()), [{"items": [task(TASK_WITH_COMMENTS, "2026-09-12T18:40:12.345678Z", 5)]}])
    api = run(tmp_path, later, now=NOW + timedelta(days=1))
    assert api.requested(COMMENTS) == []


def test_comments_not_found_are_skipped(tmp_path, caplog):
    api = october_api(2, [])
    api.answer(COMMENTS, {"task_id": TASK_WITH_COMMENTS}, completed_fixture("error_404"), status=404)
    with caplog.at_level(logging.DEBUG, logger="todoist_takeout.completed"):
        run(tmp_path, api)
    [record] = [r for r in caplog.records if "HTTP 404" in r.getMessage()]
    assert record.levelno == logging.DEBUG
    assert not (tmp_path / "completed" / "comments").exists()
    assert stored(tmp_path, "2026-10.json")["items"][0]["id"] == TASK_WITH_COMMENTS


def test_comments_restricted_by_the_plan_warn_once(tmp_path, caplog):
    api = FakeApi(account())
    tasks = [task(f"T{i}", f"2026-10-0{i}T09:00:00.000000Z", 1) for i in range(1, 4)]
    api.answer(BY_COMPLETION_DATE, window("2026-10"), {"items": tasks})
    api.answer(BY_COMPLETION_DATE, window("2026-09"), {"items": tasks[:1]})
    api.answer(COMMENTS, {}, completed_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "the Todoist plan does not allow fetching comments; the comments of completed tasks cannot be backed up "
        "on this plan"
    ]
    assert len(api.requested(COMMENTS)) == 1
    assert len(stored(tmp_path, "2026-10.json")["items"]) == 3


def test_comments_of_another_task_in_the_file_fail_the_run(tmp_path):
    # What a case-insensitive file system does to two task IDs that differ only in letter case.
    run(tmp_path)
    other = TASK_WITH_COMMENTS.lower()
    path = tmp_path / "completed" / "comments" / f"{TASK_WITH_COMMENTS}.json"
    path.parent.mkdir(parents=True)
    content = storage.encode_json({"results": [{**C1, "item_id": other}]})
    path.write_bytes(content)
    with pytest.raises(StorageError, match=f"comments of the task '{other}'"):
        run(tmp_path, october_api(2, [C1, C2]), now=NOW + timedelta(days=1))
    assert path.read_bytes() == content
    assert stored(tmp_path, "2026-10.json") == {"items": []}


def test_crash_after_the_comments_and_before_the_month_file(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    run(reference, october_api(2, [C1, C2]))

    target = tmp_path / "target"
    real = storage.write_bytes_atomic

    def crash_on_month(path, data, **kwargs):
        if Path(path).name == "2026-10.json":
            raise storage.InjectedFault("killed")
        return real(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write_bytes_atomic", crash_on_month)
        with pytest.raises(storage.InjectedFault):
            run(target, october_api(2, [C1, C2]))
    assert (target / "completed" / "comments" / f"{TASK_WITH_COMMENTS}.json").is_file()
    assert not (target / "completed" / "2026-10.json").exists()

    api = run(target, october_api(2, [C1, C2]))
    assert len(api.requested(COMMENTS)) == 1
    assert completed_tree(target) == completed_tree(reference)


# --- 5.1 completion events from the activity log ------------------------------------------------

RECURRING_TASK = "6X8Gr7Mn2pQwVb4T"
LARGE_EVENT_ID = 152735830295847392018475629384756102938
NOVEMBER = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def activity_api(pages=None, **kwargs):
    """Let the activity log return the fixture events: two completions of a recurring task after T0,
    one completion before T0, and one in November."""
    pages = pages or [completed_fixture("activities_1"), completed_fixture("activities_2")]
    return FakeApi(account(**kwargs)).cursor_pages(ACTIVITIES, {}, pages)


def events(target, month):
    return stored(target, f"activity/{month}.json")["results"]


def test_activity_log_is_requested_for_completions_since_the_day_before_t0(tmp_path):
    api = run(tmp_path)
    assert api.requested(ACTIVITIES) == [
        {"object_event_types": '["item:completed"]', "date_from": "2026-10-17", "limit": "100"}
    ]
    assert progress(tmp_path)["activity_fetched_utc"] == "2026-10-18T12:00:00.000000Z"
    assert not (tmp_path / "completed" / "activity").exists()  # only months with events get a file


def test_recurring_task_completed_twice_between_two_runs_gives_two_events(tmp_path):
    run(tmp_path)
    api = run(tmp_path, activity_api(), now=NOVEMBER)
    assert api.requested(ACTIVITIES)[0]["date_from"] == "2026-10-17"
    assert len(api.requested(ACTIVITIES)) == 2  # the cursor walk
    recurring = [e for e in events(tmp_path, "2026-10") if e["object_id"] == RECURRING_TASK]
    assert [e["event_date"] for e in recurring] == ["2026-10-19T06:30:00.000000Z", "2026-10-20T06:45:30.000000Z"]
    assert events(tmp_path, "2026-10") == completed_fixture("activities_1")["results"]  # unchanged
    assert [e["event_date"] for e in events(tmp_path, "2026-11")] == ["2026-11-01T00:00:01.000000Z"]
    assert list(stored(tmp_path, "activity/2026-10.json")) == ["results"]  # no next_cursor
    assert progress(tmp_path)["activity_fetched_utc"] == "2026-11-02T12:00:00.000000Z"


def test_event_before_t0_is_not_stored(tmp_path):
    run(tmp_path)  # T0 is 2026-10-18T12:00:00Z
    run(tmp_path, activity_api(), now=NOVEMBER)
    dates = [e["event_date"] for month in ("2026-10", "2026-11") for e in events(tmp_path, month)]
    assert "2026-10-18T09:59:59.000000Z" not in dates
    assert len(dates) == 3


def test_event_at_t0_is_stored(tmp_path):
    at_t0 = {**completed_fixture("activities_2")["results"][0], "event_date": "2026-10-18T12:00:00.000000Z"}
    run(tmp_path, activity_api([{"results": [at_t0], "next_cursor": None}]))
    assert events(tmp_path, "2026-10") == [at_t0]


def test_large_event_id_is_stored_exactly(tmp_path):
    run(tmp_path)
    run(tmp_path, activity_api(), now=NOVEMBER)
    data = (tmp_path / "completed" / "activity" / "2026-10.json").read_bytes()
    assert f'"id": {LARGE_EVENT_ID},'.encode() in data
    assert events(tmp_path, "2026-10")[0]["id"] == LARGE_EVENT_ID


def test_overlapping_fetches_store_each_event_once_with_the_latest_content(tmp_path):
    run(tmp_path)
    run(tmp_path, activity_api(), now=NOVEMBER)
    first, second = completed_fixture("activities_1")["results"]
    edited = {**second, "extra_data": {**second["extra_data"], "content": "Take out the bins (edited)"}}
    api = run(tmp_path, activity_api([{"results": [edited, first], "next_cursor": None}]), now=NOVEMBER + timedelta(hours=1))
    assert api.requested(ACTIVITIES)[0]["date_from"] == "2026-11-01"
    assert events(tmp_path, "2026-10") == [first, edited]


def test_killed_before_the_activity_checkpoint(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    run(reference)
    run(reference, activity_api(), now=NOVEMBER)

    target = tmp_path / "target"
    run(target)
    real = storage.update_completed_state

    def killed(target_, state, **changes):
        if "activity_fetched_utc" in changes:
            raise storage.InjectedFault("killed")
        return real(target_, state, **changes)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "update_completed_state", killed)
        with pytest.raises(storage.InjectedFault):
            run(target, activity_api(), now=NOVEMBER)
    assert (target / "completed" / "activity" / "2026-10.json").is_file()
    assert progress(target)["activity_fetched_utc"] == "2026-10-18T12:00:00.000000Z"

    api = run(target, activity_api(), now=NOVEMBER)
    assert api.requested(ACTIVITIES)[0]["date_from"] == "2026-10-17"  # fetched again
    assert len(events(target, "2026-10")) == 2
    assert completed_tree(target) == completed_tree(reference)


def test_event_without_a_valid_date_goes_to_the_month_of_the_fetch(tmp_path, caplog):
    undated = {**completed_fixture("activities_1")["results"][0], "event_date": None}
    with caplog.at_level(logging.WARNING):
        run(tmp_path, activity_api([{"results": [undated], "next_cursor": None}]))
    assert events(tmp_path, "2026-10") == [undated]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "an event of the activity log has no valid event_date (None); it is stored in the month of the fetch, 2026-10"
    ]


def test_invalid_activity_checkpoint_is_a_config_error(tmp_path):
    run(tmp_path)
    state = json.loads((tmp_path / "state.json").read_bytes())
    state["completed"]["activity_fetched_utc"] = "last week"
    (tmp_path / "state.json").write_bytes(storage.encode_json(state))
    with pytest.raises(ConfigError, match="activity_fetched_utc 'last week' is not a UTC timestamp"):
        run(tmp_path)


# --- 5.2 gaps in the activity log and plan restrictions ---------------------------------------


def activity_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "activity log" in r.getMessage()]


def test_free_plan_long_pause_warns_about_the_gap_and_exits_with_status_0(tmp_path, monkeypatch, capsys):
    run(tmp_path, FakeApi(account(plan={"activity_log_limit": 7})))
    api = activity_api(plan={"activity_log_limit": 7})
    monkeypatch.setenv("TODOIST_API_TOKEN", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setattr(sync, "_utc_now", lambda: NOW + timedelta(days=10))
    status = main(["backup", "--quiet", str(tmp_path)], client_factory=lambda token, wait: Client(api, sleep=_no_sleep))
    assert status == 0
    assert capsys.readouterr().err.splitlines() == [
        "WARNING the Todoist plan keeps the activity log for only 7 days, and it was not fetched for 10 days; "
        "completion events from 2026-10-18 12:00 UTC to 2026-10-21 12:00 UTC may be missing from the backup"
    ]
    assert len(events(tmp_path, "2026-10")) == 2  # the events the log still returns are stored
    assert progress(tmp_path)["activity_fetched_utc"] == "2026-10-28T12:00:00.000000Z"


@pytest.mark.parametrize("plan", [{"activity_log_limit": 7}, {"activity_log_limit": -1}, {}])
def test_within_the_limit_or_without_one_there_is_no_warning(tmp_path, caplog, plan):
    run(tmp_path, FakeApi(account(plan=plan)))
    days = 2 if plan.get("activity_log_limit") == 7 else 400
    with caplog.at_level(logging.WARNING):
        run(tmp_path, FakeApi(account(plan=plan)), now=NOW + timedelta(days=days))
    assert activity_warnings(caplog) == []


def test_activity_log_restricted_by_the_plan(tmp_path, caplog):
    api = september(FakeApi(account(plan={"activity_log_limit": 7})))
    api.answer(ACTIVITIES, {}, completed_fixture("error_plan_restricted"), status=403)
    with caplog.at_level(logging.WARNING):
        run(tmp_path, api)
    assert activity_warnings(caplog) == [
        "the Todoist plan does not allow fetching the activity log; completion events cannot be backed up on this plan"
    ]
    assert progress(tmp_path)["activity_fetched_utc"] is None  # the checkpoint stays
    assert len(stored(tmp_path, "2026-09.json")["items"]) == 3  # the completed tasks are stored
    assert json.loads((tmp_path / "state.json").read_bytes())["last_successful_run_utc"] is not None

    # Once the log is available again, the warning names the gap since T0.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run(tmp_path, activity_api(plan={"activity_log_limit": 7}), now=NOW + timedelta(days=10))
    assert len(activity_warnings(caplog)) == 1
    assert "from 2026-10-18 12:00 UTC to 2026-10-21 12:00 UTC" in activity_warnings(caplog)[0]
