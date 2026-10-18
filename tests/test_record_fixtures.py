import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from todoist_takeout import completed, storage

from fake_api import ACTIVITIES, BY_COMPLETION_DATE, COMMENTS, TASKS_COMPLETED, FakeApi, completed_fixture

TOKEN = "0123456789abcdef0123456789abcdef01234567"

PERSONAL = {
    "sync_token": "aLGJg_2qwBE_kE3j9_Gn6uoKQtvQeyjm_UEz_l2cx9SxAuT8M8O4OfzYyoEh0YJwPtVE7ZEZ3uL9e0vZ",
    "full_sync": True,
    "user": {
        "id": "2671355",
        "email": "jane.doe@example.com",
        "full_name": "Jane Doe",
        "token": TOKEN,
        "avatar_big": "https://dcff1xvirvpfp.cloudfront.net/abc_big.jpg",
        "inbox_project_id": "6Jf8VQXxpwv56VQ7",
        "tz_info": {"timezone": "Europe/Berlin", "gmt_string": "+02:00", "hours": 2},
        "lang": "de",
        "premium_status": "legacy_personal_plan",
        "karma": 54345.0,
        "is_premium": True,
    },
    "projects": [{"id": "6Jf8VQXxpwv56VQ7", "name": "Inbox", "creator_uid": 2671355, "child_order": 3}],
    "items": [
        {
            "id": "6X7rM8997g3RQmvh",
            "project_id": "6Jf8VQXxpwv56VQ7",
            "content": "Call Dr. Müller about the results",
            "description": "Ask for 💊",
            "labels": ["health", "Familie"],
            "child_ids": ["6X7rfFVPjhvv84XG"],
            "priority": 4,
            "due": {"date": "2026-10-20T09:30:00", "string": "every monday", "lang": "en", "is_recurring": True},
            "added_at": "2026-10-18T10:00:00.123456Z",
            "color": "berry_red",
            "view_style": "Not An Enum Value",
            "deadline": None,
        }
    ],
    "day_orders": {"6X7rM8997g3RQmvh": 7},
    "view_options": [{"view_type": "project", "object_id": "6Jf8VQXxpwv56VQ7", "sorted_by": "due_date"}],
    "user_item_orders": [{"scope": "day", "scope_id": "2026-10-18", "item_id": "6X7rM8997g3RQmvh"}],
    "user_plan_limits": {"current": {"max_projects": 300, "comments": True, "upload_limit_mb": 100}},
}
IDS = ["2671355", "6Jf8VQXxpwv56VQ7", "6X7rM8997g3RQmvh", "6X7rfFVPjhvv84XG"]


def same_classes(original, pseudonym):
    def classes(text):
        return [(c.isdigit(), c.isupper(), c.islower()) for c in text]

    return len(original) == len(pseudonym) and classes(original) == classes(pseudonym)


def test_scrubber_replaces_everything_of_the_account(recorder):
    scrubbed = recorder.Scrubber().scrub(PERSONAL)
    user, project, item = scrubbed["user"], scrubbed["projects"][0], scrubbed["items"][0]

    assert user["email"] == recorder.EMAIL_PLACEHOLDER
    assert user["full_name"] == user["token"] == recorder.PLACEHOLDER
    assert user["avatar_big"] == recorder.URL_PLACEHOLDER
    assert user["tz_info"] == {"timezone": recorder.PLACEHOLDER, "gmt_string": recorder.PLACEHOLDER, "hours": 0}
    assert user["lang"] == user["premium_status"] == item["due"]["lang"] == recorder.PLACEHOLDER
    assert item["content"] == item["description"] == item["due"]["string"] == recorder.PLACEHOLDER
    assert item["labels"] == [recorder.PLACEHOLDER, recorder.PLACEHOLDER]
    assert item["view_style"] == recorder.PLACEHOLDER  # an enum field, but not an enum-like value
    assert scrubbed["sync_token"] == recorder.PLACEHOLDER

    # Numbers become zero of the same type, except plan values.
    assert (user["karma"], project["child_order"], item["priority"]) == (0.0, 0, 0)
    assert isinstance(user["karma"], float)
    assert scrubbed["user_plan_limits"] == PERSONAL["user_plan_limits"]

    # Enum-like values of enum fields, booleans, null and dates in their format stay.
    assert item["color"] == "berry_red" and scrubbed["user_item_orders"][0]["scope"] == "day"
    assert scrubbed["view_options"][0]["view_type"] == "project"
    assert scrubbed["view_options"][0]["sorted_by"] == "due_date"
    assert user["is_premium"] is True and item["due"]["is_recurring"] is True and item["deadline"] is None
    assert item["due"]["date"] == "2000-01-01T00:00:00"
    assert item["added_at"] == "2000-01-01T00:00:00.000000Z"
    # A date used as an ID (the scope of a day order) is pseudonymized like an ID, so keys stay unique.
    scope_id = scrubbed["user_item_orders"][0]["scope_id"]
    assert scope_id != "2026-10-18" and same_classes("2026-10-18", scope_id) and scope_id[4] == "-"

    # Nothing of the account survives anywhere, and the shape is unchanged.
    text = json.dumps(scrubbed, ensure_ascii=False)
    for secret in ["Jane", "Doe", "jane.doe", TOKEN, "Müller", "💊", "Familie", "health", "Berlin",
                   "cloudfront", "monday", "legacy", "54345", "Inbox", *IDS]:
        assert secret not in text, secret
    assert recorder.shape_of(scrubbed) == recorder.shape_of(PERSONAL)


def test_ids_get_consistent_pseudonyms_with_the_same_classes(recorder):
    scrubber = recorder.Scrubber()
    scrubbed = scrubber.scrub(PERSONAL)
    user, project, item = scrubbed["user"], scrubbed["projects"][0], scrubbed["items"][0]

    # References between objects still match.
    assert item["project_id"] == project["id"] == user["inbox_project_id"]
    assert item["project_id"] == scrubbed["view_options"][0]["object_id"]
    assert list(scrubbed["day_orders"]) == [item["id"]] == [scrubbed["user_item_orders"][0]["item_id"]]
    assert scrubbed["day_orders"][item["id"]] == 0
    # A numeric ID becomes a number with the same pseudonym as the same ID as a string.
    assert project["creator_uid"] == int(user["id"])

    for original in IDS:
        assert same_classes(original, scrubber.pseudonym(original))
        assert scrubber.pseudonym(original) != original
    assert scrubber.originals == {*IDS, "2026-10-18"}

    # The same scrubber gives the same pseudonyms again; a new one gives other ones.
    assert scrubber.scrub(PERSONAL) == scrubbed
    assert recorder.Scrubber().scrub(PERSONAL)["items"][0]["id"] != item["id"]


def test_scrubbed_bytes_are_consistent_across_the_responses_of_a_recording(recorder):
    full = json.dumps(PERSONAL).encode()
    incremental = json.dumps({"items": [{"id": "6X7rM8997g3RQmvh", "content": "edited"}]}).encode()
    full_out, incremental_out = (json.loads(data) for data in recorder.scrubbed_bytes([full, incremental], TOKEN))
    assert incremental_out["items"][0]["id"] == full_out["items"][0]["id"]


def test_all_letter_ids_as_object_keys_are_pseudonymized(recorder):
    # About 7 % of Todoist IDs have no digit. One that is only a day-order key, and one that is also
    # a task ID but lowercase-free of digits, must both be replaced.
    response = {
        "items": [{"id": "abcDEFghiJKLmnoP", "content": "x"}],
        "day_orders": {"abcDEFghiJKLmnoP": 1, "qrsTUVwxyZABcdeF": 2, "onlylowercaseid": 3},
        "user_settings": {"reminder_push": True, "global.teams": True, "❤️": ["1"]},
    }
    [data] = recorder.scrubbed_bytes([json.dumps(response).encode()])
    scrubbed = json.loads(data)
    for original in ["abcDEFghiJKLmnoP", "qrsTUVwxyZABcdeF", "onlylowercaseid"]:
        assert original not in data.decode()
    assert scrubbed["items"][0]["id"] in scrubbed["day_orders"]
    # Field names stay. Todoist's are snake_case; a camelCase key would count as an ID and be
    # replaced, which errs on the side of removing data.
    assert list(scrubbed["user_settings"]) == ["reminder_push", "global.teams", "❤️"]


def test_scrubbed_bytes_refuses_output_that_contains_the_token_or_an_id(recorder):
    leaked_token = json.dumps({"projects": [{"name": "x", "url_token": TOKEN}]}).encode()
    assert TOKEN.encode() not in recorder.scrubbed_bytes([leaked_token], TOKEN)[0]
    # A token in an ID field would be pseudonymized; text holding an ID would be a placeholder. Force
    # a leak by making a field name equal to an ID: field names are kept unless they look like IDs.
    leaked_id = json.dumps({"projects": [{"id": "6Jf8VQXxpwv56VQ7", "6Jf8VQXxpwv56VQ7_x": 1}]}).encode()
    with pytest.raises(SystemExit, match="original IDs"):
        recorder.scrubbed_bytes([leaked_id], TOKEN)


def test_recording_needs_the_account_variable(recorder, monkeypatch):
    monkeypatch.delenv("TODOIST_CONTRACT_WRITE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TODOIST_CONTRACT_WRITE_TOKEN"):
        recorder.token_for("write")


def test_committed_shapes_match_the_fixtures(recorder):
    expected = storage.encode_json(recorder.generate_shapes())
    assert recorder.SHAPES.read_bytes() == expected, (
        "tests/fixtures/shapes.json is out of date; run `python scripts/record_fixtures.py shapes`"
    )


def test_rescrub_pseudonymizes_existing_recordings(recorder, tmp_path, monkeypatch):
    directory = tmp_path / "recorded" / "read"
    directory.mkdir(parents=True)
    (directory / "full.json").write_text(json.dumps(PERSONAL))
    (directory / "incremental.json").write_text(json.dumps({"items": [{"id": "6X7rM8997g3RQmvh"}]}))
    monkeypatch.setattr(recorder, "RECORDED", tmp_path / "recorded")
    recorder.rescrub()
    full = json.loads((directory / "full.json").read_bytes())
    incremental = json.loads((directory / "incremental.json").read_bytes())
    assert incremental["items"][0]["id"] == full["items"][0]["id"] != "6X7rM8997g3RQmvh"
    text = (directory / "full.json").read_text()
    assert not any(original in text for original in IDS)


def test_shapes_describe_kinds_fields_and_types(recorder):
    shape = recorder.shape_of(PERSONAL)["keys"]
    assert shape["sync_token"] == {"kind": "meta", "types": ["string"], "full_sync": True}
    assert shape["items"]["kind"] == "collection(id)"
    assert shape["items"]["fields"]["due"] == ["object"]
    assert shape["view_options"]["kind"] == "collection(view_type,object_id)"
    assert shape["user"]["kind"] == "single" and shape["user"]["fields"]["is_premium"] == ["boolean"]
    assert shape["day_orders"]["fields"] == {"<id>": ["integer"]}
    assert recorder.shape_of({"brand_new": [{"id": "1"}]})["keys"]["brand_new"]["kind"] == "unclassified"


def test_compare_shapes_fails_on_additions_and_reports_removals(recorder):
    expected = recorder.shape_of(PERSONAL)
    changed = json.loads(json.dumps(PERSONAL))
    changed["items"][0]["duration"] = None
    changed["items"][0]["priority"] = "high"
    del changed["items"][0]["labels"]
    del changed["view_options"]
    changed["brand_new"] = []
    added, removed = recorder.compare_shapes(expected, recorder.shape_of(changed))
    assert added == [
        "key 'brand_new' (unclassified)",
        "field items.duration (null)",
        "type string of items.priority",
    ]
    assert removed == ["key 'view_options'", "field items.labels"]
    assert recorder.compare_shapes(expected, expected) == ([], [])


def test_compare_shapes_does_not_report_what_an_account_or_a_full_sync_cannot_show(recorder):
    recorded_incremental = {"full_sync": False, "id_mapping": {}, "items": [{"id": "1", "content": "x"}]}
    expected = recorder.merge_shapes([recorder.shape_of(PERSONAL), recorder.shape_of(recorded_incremental)])
    empty_account = {**PERSONAL, "items": [], "day_orders": {}}
    added, removed = recorder.compare_shapes(expected, recorder.shape_of(empty_account))
    assert added == [] and removed == []

    without_field = json.loads(json.dumps(PERSONAL))
    del without_field["items"][0]["content"]
    del without_field["user"]
    assert recorder.compare_shapes(expected, recorder.shape_of(without_field))[1] == [
        "key 'user'",
        "field items.content",
    ]


# --- Completed-tasks endpoints ------------------------------------------------------------------


def test_large_event_ids_become_numeric_pseudonyms_of_the_same_length(recorder):
    first, second = completed_fixture("activities_1"), completed_fixture("activities_2")
    second["results"].append(dict(first["results"][0]))  # the same event in a later response
    originals = [event["id"] for page in (first, second) for event in page["results"]]
    assert all(original > 2**64 for original in originals)

    pages = [json.loads(data) for data in recorder.scrubbed_bytes([json.dumps(p).encode() for p in (first, second)])]
    pseudonyms = [event["id"] for page in pages for event in page["results"]]
    for original, pseudonym in zip(originals, pseudonyms):
        assert isinstance(pseudonym, int) and pseudonym != 0 and pseudonym != original
        assert len(str(pseudonym)) == len(str(original))
    # Consistent within the recording: the repeated event keeps its pseudonym, the others differ.
    assert pseudonyms[-1] == pseudonyms[0]
    assert len(set(pseudonyms)) == len(set(originals))
    text = "".join(json.dumps(page) for page in pages)
    assert not any(str(original) in text for original in originals)


def fake_account(by_month: dict[str, dict], *, comments: bool = True) -> FakeApi:
    full = {"sync_token": "token-1", "full_sync": True, "items": []}
    fake = FakeApi({"*": json.dumps(full).encode(), "token-1": b'{"sync_token": "token-2", "full_sync": false}'})
    for month, page in by_month.items():
        since, until = completed.month_window(month)
        fake.answer(BY_COMPLETION_DATE, {"since": since, "until": until}, page)
    fake.answer(TASKS_COMPLETED, {}, completed_fixture("tasks_completed"))
    fake.answer(ACTIVITIES, {}, completed_fixture("activities_1"))
    if comments:
        fake.answer(COMMENTS, {}, completed_fixture("comments"))
    return fake


@pytest.fixture
def recording(recorder, tmp_path, monkeypatch):
    """Run `record --account read` against a fake API into a temporary recordings directory."""
    monkeypatch.setenv("TODOIST_CONTRACT_TOKEN", TOKEN)
    monkeypatch.setattr(recorder, "RECORDED", tmp_path / "recorded")

    def run(fake: FakeApi) -> Path:
        monkeypatch.setattr(recorder, "UrllibTransport", lambda token: fake)
        recorder.record("read", now=datetime(2026, 10, 18, 10, tzinfo=timezone.utc))
        return tmp_path / "recorded" / "read"

    return run


def test_record_takes_one_page_of_each_completed_tasks_endpoint(recording):
    # September is empty; August has a task with comments (note_count 2).
    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    directory = recording(fake)

    assert sorted(path.stem for path in directory.iterdir()) == sorted(
        ["full", "incremental", "by_completion_date", "tasks_completed", "activities", "comments"]
    )
    limit = {"limit": "50"}
    assert fake.requested(BY_COMPLETION_DATE) == [
        {"since": "2026-09-01T00:00:00.000000Z", "until": "2026-09-30T23:59:59.999999Z", **limit},
        {"since": "2026-08-01T00:00:00.000000Z", "until": "2026-08-31T23:59:59.999999Z", **limit},
    ]
    assert fake.requested(TASKS_COMPLETED) == [
        {"since": "2026-08-01T00:00:00.000000Z", "until": "2026-08-31T23:59:59.999999Z",
         "annotate_items": "true", "offset": "0", **limit},
    ]
    assert fake.requested(ACTIVITIES) == [{"object_event_types": '["item:completed"]', **limit}]
    assert fake.requested(COMMENTS) == [{"task_id": "6X8Gq4CfVh9xPm2R", **limit}]
    assert fake.requests == ["*", "token-1"]  # two syncs and no commands

    # Scrubbed with one scrubber: the comments' task matches the recorded task, and no ID is left.
    tasks = json.loads((directory / "by_completion_date.json").read_bytes())["items"]
    comments = json.loads((directory / "comments.json").read_bytes())["results"]
    assert {comment["item_id"] for comment in comments} == {tasks[0]["id"]} != {"6X8Gq4CfVh9xPm2R"}
    text = "".join(path.read_text(encoding="utf-8") for path in directory.iterdir())
    for original in ["6X8Gq4CfVh9xPm2R", "6X7fphhgwcXVGccJ", "152735830295847392018475629384756102938", "preise"]:
        assert original not in text, original


def test_record_without_a_task_with_comments_drops_an_old_comments_file(recording, recorder):
    directory = recording(fake_account({"2026-08": completed_fixture("by_completion_date_1")}))
    assert (directory / "comments.json").exists()

    without = completed_fixture("by_completion_date_2")  # no task with comments
    fake = fake_account({"2026-09": without}, comments=False)
    recording(fake)
    assert not (directory / "comments.json").exists()
    assert len(fake.requested(BY_COMPLETION_DATE)) == recorder.SEARCH_MONTHS
    assert fake.requested(COMMENTS) == []
    # The month recorded is the latest with completed tasks, not the last one searched.
    assert fake.requested(TASKS_COMPLETED)[0]["since"] == "2026-09-01T00:00:00.000000Z"


def test_rescrub_keeps_the_completed_tasks_recordings_consistent(recording, recorder):
    directory = recording(fake_account({"2026-08": completed_fixture("by_completion_date_1")}))
    recorder.rescrub()
    tasks = json.loads((directory / "by_completion_date.json").read_bytes())["items"]
    comments = json.loads((directory / "comments.json").read_bytes())["results"]
    assert {comment["item_id"] for comment in comments} == {tasks[0]["id"]}


def test_endpoint_shapes_follow_the_page_merge(recorder):
    shape = recorder.endpoint_shape_of(completed_fixture("tasks_completed"))["keys"]
    assert shape["items"]["kind"] == "list" and shape["items"]["fields"]["item_object"] == ["object"]
    # The project map is described by the fields of its projects, not by their IDs.
    assert shape["projects"]["kind"] == "map" and shape["projects"]["fields"]["name"] == ["string"]
    assert recorder.endpoint_shape_of({"projects": {}})["keys"]["projects"] == {
        "kind": "map", "types": ["object"], "fields": {},
    }
    events = recorder.endpoint_shape_of(completed_fixture("activities_1"))["keys"]
    assert events["results"]["fields"]["id"] == ["integer"] and events["next_cursor"]["kind"] == "value"

    changed = completed_fixture("tasks_completed")
    changed["items"][0]["brand_new"] = 1
    expected = recorder.endpoint_shape_of(completed_fixture("tasks_completed"))
    added, _removed = recorder.compare_shapes(expected, recorder.endpoint_shape_of(changed))
    assert added == ["field items.brand_new (integer)"]


def test_committed_shapes_describe_the_completed_tasks_endpoints(recorder):
    shapes = json.loads(recorder.SHAPES.read_bytes())
    assert sorted(shapes["endpoints"]) == sorted(recorder.ENDPOINTS)
    assert all("full_sync" not in entry for e in shapes["endpoints"].values() for entry in e["keys"].values())
