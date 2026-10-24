import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_api import (
    ACTIVITIES,
    ARCHIVED_PROJECTS,
    BY_COMPLETION_DATE,
    COMMENTS,
    TASKS_COMPLETED,
    FakeApi,
    archived_fixture,
    attachment_fixture,
    completed_fixture,
)

from todoist_takeout import archived, completed, storage

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

    # Enum-like values of enum fields, null and dates in their format stay.
    assert item["color"] == "berry_red" and scrubbed["user_item_orders"][0]["scope"] == "day"
    assert scrubbed["view_options"][0]["view_type"] == "project"
    assert scrubbed["view_options"][0]["sorted_by"] == "due_date"
    assert item["deadline"] is None
    assert item["due"]["date"] == "2000-01-01T00:00:00"
    assert item["added_at"] == "2000-01-01T00:00:00.000000Z"
    # A date used as an ID (the scope of a day order) is pseudonymized like an ID, so keys stay unique.
    scope_id = scrubbed["user_item_orders"][0]["scope_id"]
    assert scope_id != "2026-10-18" and same_classes("2026-10-18", scope_id) and scope_id[4] == "-"

    # Nothing of the account survives anywhere, and the shape is unchanged.
    text = json.dumps(scrubbed, ensure_ascii=False)
    for secret in [
        "Jane",
        "Doe",
        "jane.doe",
        TOKEN,
        "Müller",
        "💊",
        "Familie",
        "health",
        "Berlin",
        "cloudfront",
        "monday",
        "legacy",
        "54345",
        "Inbox",
        *IDS,
    ]:
        assert secret not in text, secret
    assert recorder.shape_of(scrubbed) == recorder.shape_of(PERSONAL)


def test_scrubber_sets_booleans_to_false_except_response_and_plan_flags(recorder):
    scrubbed = recorder.Scrubber().scrub(PERSONAL)
    assert scrubbed["user"]["is_premium"] is False and scrubbed["items"][0]["due"]["is_recurring"] is False
    assert scrubbed["full_sync"] is True
    assert scrubbed["user_plan_limits"]["current"]["comments"] is True

    # The contract findings keep them.
    kept = recorder.Scrubber(keep_booleans=True).scrub(PERSONAL)
    assert kept["user"]["is_premium"] is True and kept["items"][0]["due"]["is_recurring"] is True


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
    for original, pseudonym in zip(originals, pseudonyms, strict=True):
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
        recorder.record("read", now=datetime(2026, 10, 18, 10, tzinfo=UTC))
        return tmp_path / "recorded" / "read"

    return run


def test_record_takes_one_page_of_each_completed_tasks_endpoint(recording):
    # September is empty; August has a task with comments (note_count 2).
    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    directory = recording(fake)

    assert sorted(path.stem for path in directory.iterdir()) == sorted(
        ["full", "incremental", "by_completion_date", "tasks_completed", "activities", "comments", "projects_archived"]
    )
    limit = {"limit": "50"}
    assert fake.requested(BY_COMPLETION_DATE) == [
        {"since": "2026-09-01T00:00:00.000000Z", "until": "2026-09-30T23:59:59.999999Z", **limit},
        {"since": "2026-08-01T00:00:00.000000Z", "until": "2026-08-31T23:59:59.999999Z", **limit},
    ]
    assert fake.requested(TASKS_COMPLETED) == [
        {
            "since": "2026-08-01T00:00:00.000000Z",
            "until": "2026-08-31T23:59:59.999999Z",
            "annotate_items": "true",
            "offset": "0",
            **limit,
        },
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
        "kind": "map",
        "types": ["object"],
        "fields": {},
    }
    events = recorder.endpoint_shape_of(completed_fixture("activities_1"))["keys"]
    assert events["results"]["fields"]["id"] == ["integer"] and events["next_cursor"]["kind"] == "value"

    changed = completed_fixture("tasks_completed")
    changed["items"][0]["brand_new"] = 1
    expected = recorder.endpoint_shape_of(completed_fixture("tasks_completed"))
    added, _removed = recorder.compare_shapes(expected, recorder.endpoint_shape_of(changed))
    assert added == ["field items.brand_new (integer)"]


def test_committed_shapes_describe_the_get_endpoints(recorder):
    shapes = json.loads(recorder.SHAPES.read_bytes())
    assert sorted(shapes["endpoints"]) == sorted(recorder.ENDPOINTS)
    assert all("full_sync" not in entry for e in shapes["endpoints"].values() for entry in e["keys"].values())


# --- Archived-projects endpoints ----------------------------------------------------------------

PARENT_ID, CHILD_ID, OWNER_ID, COLLABORATOR_ID = "6Xr2Pq7Wm4Kd9Hc3", "6Xr2Pq8Tn5Lf2Jb6", "2671355", "48213907"


def shared_full() -> dict:
    """`/full` of the parent fixture, shared with a collaborator."""
    full = archived_fixture("full_parent")
    full["collaborators"] = [
        {
            "id": OWNER_ID,
            "email": "jane.doe@example.com",
            "full_name": "Jane Doe",
            "timezone": "Europe/Berlin",
            "image_id": None,
        },
        {
            "id": COLLABORATOR_ID,
            "email": "max@example.com",
            "full_name": "Max Mustermann",
            "timezone": "Europe/Vienna",
            "image_id": "a1b2c3d4e5f6",
        },
    ]
    full["collaborator_states"] = [
        {"project_id": PARENT_ID, "user_id": user_id, "state": "active", "is_deleted": False, "role": "CREATOR"}
        for user_id in (OWNER_ID, COLLABORATOR_ID)
    ]
    return full


def test_ids_inside_subprojects_and_collaborators_get_consistent_pseudonyms(recorder):
    listed, full = archived_fixture("list_1"), shared_full()
    bodies = recorder.scrubbed_bytes([json.dumps(listed).encode(), json.dumps(full).encode()], TOKEN)
    listed_out, full_out = (json.loads(data) for data in bodies)
    project, [subproject] = full_out["project"], full_out["subprojects"]
    collaborators, states = full_out["collaborators"], full_out["collaborator_states"]

    # The listed project, the project of /full and the parent of its sub-project are the same.
    assert listed_out["results"][0]["id"] == project["id"] == subproject["parent_id"] != PARENT_ID
    assert subproject["id"] != CHILD_ID and same_classes(CHILD_ID, subproject["id"])
    assert subproject["id"] != project["id"]
    # Collaborators and their states point at the same users, and the owner is the creator.
    assert [state["user_id"] for state in states] == [collaborator["id"] for collaborator in collaborators]
    assert {state["project_id"] for state in states} == {project["id"]}
    assert collaborators[0]["id"] == project["creator_uid"] == subproject["creator_uid"] != OWNER_ID
    assert collaborators[1]["id"] != COLLABORATOR_ID and same_classes(COLLABORATOR_ID, collaborators[1]["id"])
    assert all(collaborator["email"] == recorder.EMAIL_PLACEHOLDER for collaborator in collaborators)

    text = b"".join(bodies).decode("utf-8")
    for original in [
        PARENT_ID,
        CHILD_ID,
        OWNER_ID,
        COLLABORATOR_ID,
        "a1b2c3d4e5f6",
        "Jane",
        "Max",
        "Vienna",
        "Renovation",
        "Kitchen",
        "YXJjaGl2ZWQ6Mg",
    ]:
        assert original not in text, original
    assert recorder.endpoint_shape_of(full_out) == recorder.endpoint_shape_of(full)


def test_record_takes_the_archived_list_and_one_full(recording):
    full_path = archived.full_path(PARENT_ID)
    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    fake.answer(ARCHIVED_PROJECTS, {}, archived_fixture("list_1"))
    fake.answer(full_path, {}, shared_full())
    directory = recording(fake)

    # One page of the list, although it has a next cursor. Its first project has tasks and sections,
    # so its /full is the only one requested.
    assert fake.requested(ARCHIVED_PROJECTS) == [{"limit": "50"}]
    assert fake.requested(full_path) == [{}]
    assert [path for path, _params in fake.get_requests if path.startswith("projects/")] == [
        ARCHIVED_PROJECTS,
        full_path,
    ]
    listed = json.loads((directory / "projects_archived.json").read_bytes())
    full = json.loads((directory / "project_full.json").read_bytes())
    assert listed["results"][0]["id"] == full["project"]["id"] != PARENT_ID
    # Scrubbed with the scrubber of the whole recording: the user matches the sync's.
    assert full["collaborators"][0]["id"] == listed["results"][0]["creator_uid"]


def archived_account(contents: list[set[str]]) -> tuple[FakeApi, list[str]]:
    """An account whose archived projects have /full responses with the named lists non-empty."""
    template = archived_fixture("full_parent")
    template["collaborators"] = shared_full()["collaborators"]
    project_ids = [f"6Xr2Pq7Wm4Kd{n:04d}" for n in range(len(contents))]
    projects = [{**archived_fixture("list_1")["results"][0], "id": project_id} for project_id in project_ids]
    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    fake.answer(ARCHIVED_PROJECTS, {}, {"results": projects, "next_cursor": None})
    for project, filled in zip(projects, contents, strict=True):
        full = {key: (value if key in filled or not isinstance(value, list) else []) for key, value in template.items()}
        fake.answer(archived.full_path(project["id"]), {}, {**full, "project": project})
    return fake, project_ids


def full_requests(fake: FakeApi) -> list[str]:
    return [path for path, _params in fake.get_requests if path.endswith("/full")]


def test_record_stops_at_the_first_full_with_tasks_and_sections(recording):
    fake, project_ids = archived_account([set(), {"tasks"}, {"tasks", "sections"}, {"tasks", "sections"}])
    directory = recording(fake)
    assert full_requests(fake) == [archived.full_path(project_id) for project_id in project_ids[:3]]
    full = json.loads((directory / "project_full.json").read_bytes())
    listed = json.loads((directory / "projects_archived.json").read_bytes())
    assert full["project"]["id"] == listed["results"][2]["id"]
    assert full["tasks"] and full["sections"]


def test_record_takes_the_full_with_the_most_content_among_the_candidates(recording, recorder, monkeypatch):
    monkeypatch.setattr(recorder, "FULL_CANDIDATES", 10)
    contents = [
        set(),
        {"tasks"},
        set(),
        {"tasks", "collaborators"},
        {"sections", "collaborators"},
        *[set()] * 5,
        {"tasks", "sections"},
    ]
    fake, project_ids = archived_account(contents)
    directory = recording(fake)
    # The eleventh project would have tasks and sections, but only ten are tried.
    assert recorder.FULL_CANDIDATES == 10
    assert full_requests(fake) == [archived.full_path(p) for p in project_ids[: recorder.FULL_CANDIDATES]]
    full = json.loads((directory / "project_full.json").read_bytes())
    listed = json.loads((directory / "projects_archived.json").read_bytes())
    # The fourth and the fifth have two non-empty lists each; the earlier one wins.
    assert full["project"]["id"] == listed["results"][3]["id"]


def test_record_without_archived_projects_drops_an_old_full_file(recording):
    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    fake.answer(ARCHIVED_PROJECTS, {}, archived_fixture("list_1"))
    fake.answer(archived.full_path(PARENT_ID), {}, archived_fixture("full_parent"))
    directory = recording(fake)
    assert (directory / "project_full.json").exists()

    fake = fake_account({"2026-08": completed_fixture("by_completion_date_1")})
    recording(fake)
    assert (directory / "projects_archived.json").exists()
    assert not (directory / "project_full.json").exists()
    assert [path for path, _params in fake.get_requests if path.startswith("projects/")] == [ARCHIVED_PROJECTS]


def test_committed_shapes_describe_the_archived_endpoints(recorder):
    endpoints = json.loads(recorder.SHAPES.read_bytes())["endpoints"]
    listed, full = endpoints["projects_archived"]["keys"], endpoints["project_full"]["keys"]
    assert listed["results"]["kind"] == "list" and listed["results"]["fields"]["is_archived"] == ["boolean"]
    assert sorted(full) == sorted(
        [
            "project",
            "tasks",
            "sections",
            "subprojects",
            "collaborators",
            "collaborator_states",
            "folder",
            "comments_count",
        ]
    )
    assert full["project"]["kind"] == "map" and full["subprojects"]["kind"] == "list"


def test_archived_contract_comparison_fails_on_an_added_field(recorder):
    # What the contract test of the archived endpoints compares: the committed shapes against the
    # recordings they were made from, or the hand-written fixtures without a recording.
    expected = json.loads(recorder.SHAPES.read_bytes())["endpoints"]
    sources = recorder.endpoint_shape_sources()
    bodies = {name: sources[name][0].read_bytes() for name in recorder.ARCHIVED_ENDPOINTS}
    assert recorder.compare_endpoint_bodies(expected, bodies) == {
        "projects_archived": ([], []),
        "project_full": ([], []),
    }

    listed, full = (json.loads(bodies[name]) for name in ("projects_archived", "project_full"))
    listed["results"][0]["brand_new"] = True
    full["project"]["brand_new"] = "x"
    full["brand_new_key"] = []
    bodies = {"projects_archived": json.dumps(listed).encode(), "project_full": json.dumps(full).encode()}
    result = recorder.compare_endpoint_bodies(expected, bodies)
    assert result["projects_archived"][0] == ["field results.brand_new (boolean)"]
    assert result["project_full"][0] == ["key 'brand_new_key' (list)", "field project.brand_new (string)"]


# --- Uploaded files (add-attachment-downloads) --------------------------------------------------

UPLOAD = {
    "id": "6Xd9Kp3Rt5Vw7Yb2",
    "item_id": "6X7rM8997g3RQmvh",
    "content": "Rechnung von Dr. Müller",
    "file_attachment": {
        "file_name": "Rechnung März 2026.png",
        "file_size": 48213,
        "file_type": "image/png",
        "file_url": "https://files.todoist.com/Xq3v9TzW/by_id/6Xd9Kp3Rt5Vw7Yb2/Rechnung%20M%C3%A4rz%202026.png",
        "image": "https://files.todoist.com/Xq3v9TzW/tn/Rechnung.png?Expires=1767225600&Signature=c2lnbmF0dXJl",
        "image_height": 480,
        "image_width": 640,
        "resource_type": "image",
        "tn_l": ["https://d1abcdefgh.cloudfront.net/Xq3v9TzW/tn_l.png?Key-Pair-Id=K2JCJMDEHXQW5F", 528, 396],
        "upload_state": "completed",
    },
}
UPLOAD_SECRETS = [
    "files.todoist.com",
    "cloudfront",
    "Xq3v9TzW",
    "by_id",
    "Rechnung",
    "März",
    "M%C3%A4rz",
    "?",
    "Expires",
    "Signature",
    "Key-Pair-Id",
    "image/png",
    "Müller",
    "6Xd9Kp3Rt5Vw7Yb2",
    "6X7rM8997g3RQmvh",
]


def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def test_scrubbed_upload_keeps_no_host_path_query_or_file_name(recorder):
    scrubbed = recorder.Scrubber().scrub({"notes": [UPLOAD]})
    [comment] = scrubbed["notes"]
    attachment = comment["file_attachment"]
    assert attachment["file_name"] == attachment["file_type"] == recorder.PLACEHOLDER
    assert attachment["file_url"] == attachment["image"] == recorder.URL_PLACEHOLDER
    assert attachment["tn_l"] == [recorder.URL_PLACEHOLDER, 0, 0]
    assert (attachment["file_size"], attachment["image_width"], attachment["image_height"]) == (0, 0, 0)
    # What the download step and the shapes need stays: the kind of upload and its state.
    assert (attachment["resource_type"], attachment["upload_state"]) == ("image", "completed")
    assert comment["id"] != UPLOAD["id"] and same_classes(UPLOAD["id"], comment["id"])
    text = json.dumps(scrubbed, ensure_ascii=False)
    for secret in UPLOAD_SECRETS:
        assert secret not in text, secret
    assert recorder.uploaded_attachments(scrubbed) == [attachment]
    assert recorder.attachment_shape_of([attachment]) == recorder.attachment_shape_of([UPLOAD["file_attachment"]])


def test_no_recording_holds_a_file_host_path_query_or_file_name(recorder):
    recordings = sorted(recorder.RECORDED.glob("*/*.json"))
    assert recordings
    for path in recordings:
        for text in strings(json.loads(path.read_bytes())):
            if text.startswith(("http://", "https://")):
                assert text == recorder.URL_PLACEHOLDER, path
            assert "?" not in text and "todoist.com" not in text and "cloudfront" not in text, path
        for attachment in recorder.uploaded_attachments(json.loads(path.read_bytes())):
            assert attachment.get("file_name") in (recorder.PLACEHOLDER, None), path


def test_uploaded_attachments_follow_the_download_rule(recorder):
    link, pending, foreign = attachment_fixture("comments")["results"]
    uploads = attachment_fixture("uploads")["results"]
    response = {"notes": [link, pending, foreign, {"id": "N1", "file_attachment": None}], "results": uploads}
    # Links and pending uploads are not uploaded files; a file on a foreign host is one, which the step skips.
    assert recorder.uploaded_attachments(response) == [
        foreign["file_attachment"],
        *(c["file_attachment"] for c in uploads),
    ]


def test_committed_shapes_describe_uploaded_files(recorder):
    shapes = json.loads(recorder.SHAPES.read_bytes())["file_attachments"]
    assert shapes["sources"] == [
        p.relative_to(recorder.FIXTURES).as_posix() for p in recorder.attachment_shape_sources()
    ]
    entry = shapes["keys"]["file_attachment"]
    assert entry["kind"] == "file|image"
    assert {"file_name", "file_size", "file_url", "resource_type", "upload_state", "tn_l"} <= entry["fields"].keys()

    uploads = [c["file_attachment"] for c in attachment_fixture("uploads")["results"]]
    assert recorder.compare_shapes(shapes, recorder.attachment_shape_of(uploads)) == ([], [])
    # A text file has no thumbnails: reported as removed, which the contract tests only report.
    text = uploads[0]
    assert recorder.compare_shapes(shapes, recorder.attachment_shape_of([text])) == (
        [],
        ["field file_attachment.tn_l", "field file_attachment.tn_m", "field file_attachment.tn_s"],
    )
    changed = {**text, "resource_type": "video", "checksum": "abc"}
    added, _removed = recorder.compare_shapes(shapes, recorder.attachment_shape_of([changed]))
    assert added == [
        "kind 'video' of 'file_attachment' (expected 'file|image')",
        "field file_attachment.checksum (string)",
    ]
