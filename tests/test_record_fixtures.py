import json

import pytest

from todoist_takeout import storage

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
        "is_premium": True,
    },
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
        }
    ],
    "view_options": [{"view_type": "project", "object_id": "6Jf8VQXxpwv56VQ7", "sorted_by": "due_date"}],
}


def test_scrubber_replaces_personal_strings_and_keeps_ids_and_enums(recorder):
    scrubbed = recorder.scrub(PERSONAL)
    user, item = scrubbed["user"], scrubbed["items"][0]

    assert user["email"] == recorder.EMAIL_PLACEHOLDER
    assert user["full_name"] == recorder.PLACEHOLDER
    assert user["token"] == recorder.PLACEHOLDER
    assert user["avatar_big"] == recorder.URL_PLACEHOLDER
    assert user["tz_info"] == {"timezone": recorder.PLACEHOLDER, "gmt_string": recorder.PLACEHOLDER, "hours": 2}
    assert item["content"] == item["description"] == item["due"]["string"] == recorder.PLACEHOLDER
    assert item["labels"] == [recorder.PLACEHOLDER, recorder.PLACEHOLDER]
    assert item["view_style"] == recorder.PLACEHOLDER  # an enum field, but not an enum-like value
    assert scrubbed["sync_token"] == recorder.PLACEHOLDER

    # IDs, keys, enum-like values, numbers and booleans stay.
    assert user["id"] == "2671355" and user["inbox_project_id"] == "6Jf8VQXxpwv56VQ7"
    assert item["id"] == "6X7rM8997g3RQmvh" and item["child_ids"] == ["6X7rfFVPjhvv84XG"]
    assert user["lang"] == "de" and item["color"] == "berry_red" and item["due"]["lang"] == "en"
    assert scrubbed["view_options"][0] == {"view_type": "project", "object_id": "6Jf8VQXxpwv56VQ7", "sorted_by": "due_date"}
    assert item["priority"] == 4 and item["due"]["is_recurring"] is True and user["is_premium"] is True

    # Dates keep their format.
    assert item["due"]["date"] == "2000-01-01T00:00:00"
    assert item["added_at"] == "2000-01-01T00:00:00.000000Z"

    # Nothing personal survives anywhere, and the shape is unchanged.
    text = json.dumps(scrubbed, ensure_ascii=False)
    for secret in ["Jane", "Doe", "jane.doe", TOKEN, "Müller", "💊", "Familie", "health", "Berlin", "cloudfront", "monday"]:
        assert secret not in text
    assert recorder.shape_of(scrubbed) == recorder.shape_of(PERSONAL)


def test_scrubbed_bytes_refuses_output_that_contains_the_token(recorder):
    body = json.dumps({"projects": [{"id": TOKEN}]}).encode()  # an ID-like field holding the token
    with pytest.raises(SystemExit, match="still contains the API token"):
        recorder.scrubbed_bytes(body, TOKEN)
    assert TOKEN.encode() not in recorder.scrubbed_bytes(json.dumps(PERSONAL).encode(), TOKEN)


def test_recording_needs_the_account_variable(recorder, monkeypatch):
    monkeypatch.delenv("TODOIST_CONTRACT_WRITE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TODOIST_CONTRACT_WRITE_TOKEN"):
        recorder.token_for("write")


def test_committed_shapes_match_the_fixtures(recorder):
    expected = storage.encode_json(recorder.generate_shapes())
    assert recorder.SHAPES.read_bytes() == expected, (
        "tests/fixtures/shapes.json is out of date; run `python scripts/record_fixtures.py shapes`"
    )


def test_shapes_describe_kinds_fields_and_types(recorder):
    shape = recorder.shape_of(PERSONAL)["keys"]
    assert shape["sync_token"] == {"kind": "meta", "types": ["string"]}
    assert shape["items"]["kind"] == "collection(id)"
    assert shape["items"]["fields"]["due"] == ["object"]
    assert shape["view_options"]["kind"] == "collection(view_type,object_id)"
    assert shape["user"]["kind"] == "single" and shape["user"]["fields"]["is_premium"] == ["boolean"]
    assert recorder.shape_of({"brand_new": [{"id": "1"}]})["keys"]["brand_new"]["kind"] == "unclassified"


def test_compare_shapes_fails_on_additions_and_reports_removals(recorder):
    expected = recorder.shape_of(PERSONAL)
    changed = json.loads(json.dumps(PERSONAL))
    changed["items"][0]["deadline"] = None
    changed["items"][0]["priority"] = "high"
    del changed["items"][0]["labels"]
    del changed["view_options"]
    changed["brand_new"] = []
    added, removed = recorder.compare_shapes(expected, recorder.shape_of(changed))
    assert added == [
        "key 'brand_new' (unclassified)",
        "field items.deadline (null)",
        "type string of items.priority",
    ]
    assert removed == ["key 'view_options'", "field items.labels"]
    assert recorder.compare_shapes(expected, expected) == ([], [])
