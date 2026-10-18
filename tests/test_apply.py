import json
import logging
import os

import pytest

from todoist_takeout import storage
from todoist_takeout.errors import StorageError
from todoist_takeout.resources import KNOWN, Classifier
from todoist_takeout.sync import apply_response

FULL_SYNC_TREE = {
    "sync/items/6X7rM8997g3RQmvh.json",
    "sync/items/6X7rfFVPjhvv84XG.json",
    "sync/items/6X7rfEVP8hvv25ZQ.json",
    "sync/projects/6Jf8VQXxpwv56VQ7.json",
    "sync/projects/6X7fphhgwcXVGccJ.json",
    "sync/projects/6Jf8VQXxpwv5XvQ2.json",
    "sync/sections/6X7FxXvX84jHphx2.json",
    "sync/labels/2156154810.json",
    "sync/labels/2156154811.json",
    "sync/notes/6X7rfFVQXwRmcvFp.json",
    "sync/project_notes/6X7v4F2r7hH8cGWW.json",
    "sync/filters/4638879.json",
    "sync/reminders/6X7Vfq5rqPMM5j5q.json",
    "sync/collaborators/2671362.json",
    "sync/collaborator_states/6X7fphhgwcXVGccJ~2671362.json",
    "sync/view_options/project~6X7fphhgwcXVGccJ.json",
    "sync/project_view_options_defaults/6Jf8VQXxpwv5XvQ2.json",
    "sync/workspaces/28173.json",
    "sync/workspace_users/28173~2671355.json",
    "sync/user.json",
    "sync/user_settings.json",
    "sync/user_plan_limits.json",
    "sync/settings_notifications.json",
    "sync/stats.json",
    "sync/day_orders.json",
    "sync/locations.json",
    "sync/role_actions.json",
    "sync/live_notifications_last_read_id.json",
}
OLD = 1_000_000_000


def tree(target):
    return {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}


def snapshot(target):
    return {path: (target / path).read_bytes() for path in tree(target)}


def age_all_files(target):
    for path in tree(target):
        os.utime(target / path, ns=(OLD, OLD))


def mtimes(target):
    return {path: (target / path).stat().st_mtime_ns for path in tree(target)}


def apply(target, response):
    return apply_response(target, response, Classifier())


# --- 4.3 live objects and single resources ------------------------------------


def test_full_sync_produces_expected_tree(tmp_path, sync_fixture):
    stats = apply(tmp_path, sync_fixture("full_sync"))
    assert tree(tmp_path) == FULL_SYNC_TREE
    assert (stats.written, stats.unchanged, stats.deleted, stats.missing) == (len(FULL_SYNC_TREE), 0, 0, 0)


def test_stored_objects_parse_to_their_source_objects(tmp_path, sync_fixture):
    response = sync_fixture("full_sync")
    apply(tmp_path, response)
    for path in FULL_SYNC_TREE:
        parts = path.removesuffix(".json").split("/")
        stored = json.loads((tmp_path / path).read_bytes())
        if len(parts) == 2:
            assert stored == response[parts[1]]
            continue
        resource, stem = parts[1], parts[2]
        value = response[resource]
        if isinstance(value, dict):
            source = value[stem]
        else:
            [source] = [obj for obj in value if storage.encode_key(_key(resource, obj)) == stem]
        assert stored == source
        assert list(stored) == list(source)  # field order kept
        assert (tmp_path / path).read_bytes() == storage.encode_json(source)


def _key(resource, obj):
    return [str(obj[field]) for field in KNOWN[resource].fields]


def test_non_ascii_content_is_stored_literally(tmp_path, sync_fixture):
    apply(tmp_path, sync_fixture("full_sync"))
    assert "Größe 🍎".encode() in (tmp_path / "sync/items/6X7rM8997g3RQmvh.json").read_bytes()


def test_reapplying_full_sync_rewrites_nothing(tmp_path, sync_fixture):
    apply(tmp_path, sync_fixture("full_sync"))
    age_all_files(tmp_path)
    before = snapshot(tmp_path)
    stats = apply(tmp_path, sync_fixture("full_sync"))
    assert snapshot(tmp_path) == before
    assert set(mtimes(tmp_path).values()) == {OLD}
    assert (stats.written, stats.unchanged) == (0, len(FULL_SYNC_TREE))


def test_incremental_edit_changes_only_that_object(tmp_path, sync_fixture):
    apply(tmp_path, sync_fixture("full_sync"))
    age_all_files(tmp_path)
    edit = sync_fixture("incremental_edit")
    stats = apply(tmp_path, edit)
    changed = {path for path, mtime in mtimes(tmp_path).items() if mtime != OLD}
    assert changed == {"sync/items/6X7rM8997g3RQmvh.json"}
    assert tree(tmp_path) == FULL_SYNC_TREE
    assert json.loads((tmp_path / "sync/items/6X7rM8997g3RQmvh.json").read_bytes()) == edit["items"][0]
    assert (stats.written, stats.unchanged) == (1, 0)


def test_unknown_resources_are_stored_unchanged_with_a_warning(tmp_path, sync_fixture, caplog):
    response = sync_fixture("unknown_key")
    with caplog.at_level(logging.WARNING):
        apply(tmp_path, response)
    assert tree(tmp_path) == {
        "sync/brand_new_things/N1.json",
        "sync/brand_new_things/N2.json",
        "sync/brand_new_setting.json",
    }
    assert json.loads((tmp_path / "sync/brand_new_setting.json").read_bytes()) == response["brand_new_setting"]
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 2
    assert any("'brand_new_things'" in m for m in warned)
    assert any("'brand_new_setting'" in m for m in warned)


def test_colliding_key_raises_without_overwriting(tmp_path, sync_fixture):
    # On a case-insensitive file system, the file of key `6x7rm8997g3rqmvh` is also the file of
    # `6X7rM8997g3RQmvh`. Simulate that with a file whose content has the other key.
    path = tmp_path / "sync/items/6X7rM8997g3RQmvh.json"
    other = storage.encode_json({"id": "6x7rm8997g3rqmvh", "content": "another task"})
    storage.write_bytes_atomic(path, other)
    with pytest.raises(StorageError) as info:
        apply(tmp_path, sync_fixture("full_sync"))
    assert "'6x7rm8997g3rqmvh'" in str(info.value)
    assert "'6X7rM8997g3RQmvh'" in str(info.value)
    assert path.read_bytes() == other


def test_colliding_composite_key_raises(tmp_path, sync_fixture):
    path = tmp_path / "sync/view_options/project~6X7fphhgwcXVGccJ.json"
    other = storage.encode_json({"view_type": "PROJECT", "object_id": "6X7fphhgwcXVGccJ"})
    storage.write_bytes_atomic(path, other)
    with pytest.raises(StorageError):
        apply(tmp_path, sync_fixture("full_sync"))
    assert path.read_bytes() == other


# --- 4.4 tombstones -----------------------------------------------------------


def test_full_deletion_payload_replaces_the_object(tmp_path, sync_fixture):
    apply(tmp_path, sync_fixture("full_sync"))
    deletion = sync_fixture("delete_full")
    stats = apply(tmp_path, deletion)
    path = tmp_path / "sync/items/6X7rfFVPjhvv84XG.json"
    assert path.read_bytes() == storage.encode_json(deletion["items"][0])
    assert tree(tmp_path) == FULL_SYNC_TREE  # no other file
    assert stats.deleted == 1


def test_stub_deletion_payload_becomes_a_tombstone(tmp_path, sync_fixture):
    full = sync_fixture("full_sync")
    apply(tmp_path, full)
    stats = apply(tmp_path, sync_fixture("delete_stub"))
    project = tmp_path / "sync/projects/6X7fphhgwcXVGccJ.json"
    tombstone = tmp_path / "sync/projects/6X7fphhgwcXVGccJ.deleted.json"
    assert project.read_bytes() == storage.encode_json(full["projects"][1])
    assert json.loads(tombstone.read_bytes()) == {"id": "6X7fphhgwcXVGccJ", "is_deleted": True}
    assert tree(tmp_path) == FULL_SYNC_TREE | {"sync/projects/6X7fphhgwcXVGccJ.deleted.json"}
    assert stats.deleted == 1


def test_deleted_before_ever_stored(tmp_path, sync_fixture):
    deletion = sync_fixture("delete_stub")
    apply(tmp_path, deletion)
    assert tree(tmp_path) == {"sync/projects/6X7fphhgwcXVGccJ.json"}
    assert (tmp_path / "sync/projects/6X7fphhgwcXVGccJ.json").read_bytes() == storage.encode_json(
        deletion["projects"][0]
    )


@pytest.mark.parametrize("name", ["delete_full", "delete_stub"])
def test_reapplying_a_deletion_changes_nothing(tmp_path, sync_fixture, name):
    apply(tmp_path, sync_fixture("full_sync"))
    apply(tmp_path, sync_fixture(name))
    age_all_files(tmp_path)
    before = snapshot(tmp_path)
    stats = apply(tmp_path, sync_fixture(name))
    assert snapshot(tmp_path) == before
    assert set(mtimes(tmp_path).values()) == {OLD}
    assert (stats.written, stats.deleted) == (0, 0)


# --- 4.5 missing objects in a full sync ----------------------------------------


def test_server_full_sync_keeps_missing_objects_and_warns(tmp_path, sync_fixture, caplog):
    apply(tmp_path, sync_fixture("full_sync"))
    age_all_files(tmp_path)
    missing = ["sync/items/6X7rfEVP8hvv25ZQ.json", "sync/labels/2156154811.json", "sync/filters/4638879.json"]
    before = {path: (tmp_path / path).read_bytes() for path in missing}

    with caplog.at_level(logging.WARNING):
        stats = apply(tmp_path, sync_fixture("server_full_sync"))

    assert stats.missing == 3
    assert {path: (tmp_path / path).read_bytes() for path in missing} == before
    assert all((tmp_path / path).stat().st_mtime_ns == OLD for path in missing)
    assert tree(tmp_path) == FULL_SYNC_TREE
    [record] = caplog.records
    assert record.levelname == "WARNING"
    assert "3 stored objects" in record.getMessage()


def test_missing_count_ignores_objects_already_deleted(tmp_path, sync_fixture):
    apply(tmp_path, sync_fixture("full_sync"))
    apply(tmp_path, sync_fixture("delete_stub"))  # tombstone for project 6X7fphhgwcXVGccJ
    apply(tmp_path, sync_fixture("delete_full"))  # item 6X7rfFVPjhvv84XG stored with is_deleted
    response = sync_fixture("server_full_sync")
    response["projects"] = [p for p in response["projects"] if p["id"] != "6X7fphhgwcXVGccJ"]
    response["items"] = [i for i in response["items"] if i["id"] != "6X7rfFVPjhvv84XG"]
    assert apply(tmp_path, response).missing == 3


def test_incremental_sync_does_not_count_missing_objects(tmp_path, sync_fixture, caplog):
    apply(tmp_path, sync_fixture("full_sync"))
    with caplog.at_level(logging.WARNING):
        stats = apply(tmp_path, sync_fixture("incremental_edit"))
    assert stats.missing == 0
    assert caplog.records == []


# --- 4.7 restored objects -----------------------------------------------------

PROJECT = "sync/projects/6X7fphhgwcXVGccJ.json"
PROJECT_TOMBSTONE = "sync/projects/6X7fphhgwcXVGccJ.deleted.json"


def restore_response(full):
    project = {**full["projects"][1], "name": "Restored", "is_deleted": False}
    return {"sync_token": "INC-9", "full_sync": False, "projects": [project]}


def test_live_payload_restores_a_tombstoned_object(tmp_path, sync_fixture):
    full = sync_fixture("full_sync")
    apply(tmp_path, full)
    apply(tmp_path, sync_fixture("delete_stub"))
    restore = restore_response(full)
    stats = apply(tmp_path, restore)
    assert (tmp_path / PROJECT).read_bytes() == storage.encode_json(restore["projects"][0])
    assert not (tmp_path / PROJECT_TOMBSTONE).exists()
    assert tree(tmp_path) == FULL_SYNC_TREE
    assert (stats.written, stats.unchanged, stats.deleted) == (1, 0, 0)


def test_tombstone_next_to_an_up_to_date_object_is_removed(tmp_path, sync_fixture):
    # A run that wrote the restored payload and was killed before it removed the tombstone.
    full = sync_fixture("full_sync")
    restore = restore_response(full)
    apply(tmp_path, full)
    apply(tmp_path, sync_fixture("delete_stub"))
    storage.write_bytes_atomic(tmp_path / PROJECT, storage.encode_json(restore["projects"][0]))
    stats = apply(tmp_path, restore)
    assert (tmp_path / PROJECT).read_bytes() == storage.encode_json(restore["projects"][0])
    assert not (tmp_path / PROJECT_TOMBSTONE).exists()
    assert (stats.written, stats.unchanged) == (1, 0)


def test_interrupted_restore_completes_on_reapplication(tmp_path, sync_fixture):
    full = sync_fixture("full_sync")
    restore = restore_response(full)
    apply(tmp_path, full)
    apply(tmp_path, sync_fixture("delete_stub"))
    storage.set_fault_after(1)  # the payload is written, the removal of the tombstone fails
    try:
        with pytest.raises(storage.InjectedFault):
            apply(tmp_path, restore)
    finally:
        storage.set_fault_after(None)
    assert (tmp_path / PROJECT_TOMBSTONE).exists()
    apply(tmp_path, restore)
    assert not (tmp_path / PROJECT_TOMBSTONE).exists()
    assert (tmp_path / PROJECT).read_bytes() == storage.encode_json(restore["projects"][0])


def test_reapplying_a_restore_changes_nothing(tmp_path, sync_fixture):
    full = sync_fixture("full_sync")
    restore = restore_response(full)
    apply(tmp_path, full)
    apply(tmp_path, sync_fixture("delete_stub"))
    apply(tmp_path, restore)
    age_all_files(tmp_path)
    before = snapshot(tmp_path)
    stats = apply(tmp_path, restore)
    assert snapshot(tmp_path) == before
    assert set(mtimes(tmp_path).values()) == {OLD}
    assert (stats.written, stats.unchanged) == (0, 1)


def test_tombstone_of_another_key_is_not_removed(tmp_path, sync_fixture):
    # On a case-insensitive file system, the tombstone of `6x7fphhgwcxvgccj` would be found under
    # the name of `6X7fphhgwcXVGccJ`.
    full = sync_fixture("full_sync")
    apply(tmp_path, full)
    other = storage.encode_json({"id": "6x7fphhgwcxvgccj", "is_deleted": True})
    storage.write_bytes_atomic(tmp_path / PROJECT_TOMBSTONE, other)
    with pytest.raises(StorageError):
        apply(tmp_path, restore_response(full))
    assert (tmp_path / PROJECT_TOMBSTONE).read_bytes() == other
