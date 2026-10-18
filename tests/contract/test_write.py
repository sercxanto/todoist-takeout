"""Write contract test against a dedicated test account (design D14).

It creates temporary objects, deletes, restores and archives them, and checks how the tool stores
what the sync reports. Each step's raw incremental response is recorded as a finding, because
these API behaviours are undocumented. The test fails at the end if storage did not match the
spec, after the findings are written.
"""

import json

import pytest

from todoist_takeout import storage
from todoist_takeout.sync import DELETED_SUFFIX, JSON_SUFFIX, SYNC_DIR, run_backup

PREFIX = "todoist-takeout contract test"
# Candidate commands for undeleting a task. None is documented; the test records what happens.
RESTORE_COMMANDS = ["item_undelete", "item_restore"]


@pytest.fixture
def write_client(contract_support, client_for):
    write_token = contract_support.env_token(contract_support.WRITE_TOKEN)
    if not write_token:
        pytest.skip(f"{contract_support.WRITE_TOKEN} is not set")
    if not contract_support.writes_allowed():
        pytest.skip(f"{contract_support.ALLOW_WRITES} is not 1")
    read_token = contract_support.env_token(contract_support.READ_TOKEN)
    try:
        contract_support.check_write_account(write_token, read_token, client_for)
    except contract_support.AccountGuardError as exc:
        pytest.fail(f"refusing to write: {exc}")
    return write_token, client_for(write_token)


class Observer:
    """Follows the account with its own sync token, to see the raw response of each step."""

    def __init__(self, client):
        self.client = client
        self.token = json.loads(client.sync("*"))["sync_token"]

    def changes(self) -> dict:
        response = json.loads(self.client.sync(self.token))
        self.token = response["sync_token"]
        return response


def find(response: dict, resource: str, object_id):
    for obj in response.get(resource) or []:
        if str(obj.get("id")) == str(object_id):
            return obj
    return None


def stored(target, resource: str, object_id) -> tuple[dict | None, dict | None]:
    """The stored `<key>.json` and `<key>.deleted.json` of an object, parsed, or None."""
    directory = target / SYNC_DIR / resource
    stem = storage.encode_key(str(object_id))
    files = []
    for suffix in (JSON_SUFFIX, DELETED_SUFFIX):
        data = storage.read_bytes(directory / f"{stem}{suffix}")
        files.append(None if data is None else json.loads(data))
    return files[0], files[1]


def check_deleted(target, resource, object_id, problems):
    live, tombstone = stored(target, resource, object_id)
    if tombstone is None:
        if not (live and live.get("is_deleted") is True):
            problems.append(f"{resource}/{object_id}: neither a deletion payload nor a tombstone is stored")
    elif tombstone.get("is_deleted") is not True or live is None or live.get("is_deleted") is True:
        problems.append(f"{resource}/{object_id}: the tombstone does not follow D8 ({live=}, {tombstone=})")


def presence(obj) -> dict:
    if obj is None:
        return {"present": False}
    return {"present": True, "is_archived": obj.get("is_archived"), "is_deleted": obj.get("is_deleted")}


def test_deletion_restore_and_archive(write_client, contract_support, recorder, tmp_path, write_findings):
    token, client = write_client
    commands = contract_support.Commands(client)
    target = tmp_path / "backup"
    findings: dict = {}
    problems: list[str] = []
    projects: list = []

    def backup():
        run_backup(target, client)

    def payload(response, resource, object_id):
        return recorder.scrub(find(response, resource, object_id))

    try:
        observer = Observer(client)
        backup()

        # Create a project with a task and a comment.
        project = commands.add("project_add", {"name": f"{PREFIX}: deletion"})
        assert project, f"cannot create a project: {[r.as_finding() for r in commands.rejected]}"
        projects.append(project)
        task = commands.add("item_add", {"content": f"{PREFIX}: task", "project_id": project})
        assert task, f"cannot create a task: {[r.as_finding() for r in commands.rejected]}"
        note = commands.add("note_add", {"content": f"{PREFIX}: comment", "item_id": task})
        findings["comment_created"] = note is not None
        observer.changes()
        backup()

        # Delete the comment and the task.
        if note:
            commands.run("note_delete", {"id": note})
        commands.run("item_delete", {"id": task})
        changes = observer.changes()
        findings["deletion"] = {"task": payload(changes, "items", task)}
        if note:
            findings["deletion"]["comment"] = payload(changes, "notes", note)
        backup()
        if findings["deletion"]["task"] is None:
            problems.append("the incremental sync did not report the deleted task")
        check_deleted(target, "items", task, problems)
        if note:
            check_deleted(target, "notes", note, problems)

        # Try to restore the task.
        findings["restore"] = {"commands": {}}
        restored = False
        for command in RESTORE_COMMANDS:
            ok = commands.run(command, {"id": task})
            findings["restore"]["commands"][command] = "ok" if ok else commands.rejected[-1].error
            if ok:
                restored = True
                break
        if restored:
            changes = observer.changes()
            findings["restore"]["task"] = payload(changes, "items", task)
            backup()
            live, tombstone = stored(target, "items", task)
            if tombstone is not None or live is None or live.get("is_deleted") is True:
                problems.append(f"the restored task is not stored as live without a tombstone ({live=}, {tombstone=})")
        else:
            findings["restore"]["result"] = "no restore path found"

        # Delete the project.
        if commands.run("project_delete", {"id": project}):
            projects.remove(project)
        changes = observer.changes()
        findings["deletion"]["project"] = payload(changes, "projects", project)
        backup()
        check_deleted(target, "projects", project, problems)

        # Archive a second project that contains a task.
        archived = commands.add("project_add", {"name": f"{PREFIX}: archive"})
        assert archived, f"cannot create a project: {[r.as_finding() for r in commands.rejected]}"
        projects.append(archived)
        archived_task = commands.add("item_add", {"content": f"{PREFIX}: archived task", "project_id": archived})
        observer.changes()
        backup()
        commands.run("project_archive", {"id": archived})
        changes = observer.changes()
        findings["archive"] = {
            "project": presence(find(changes, "projects", archived)),
            "task": presence(find(changes, "items", archived_task)) if archived_task else None,
            "project_payload": payload(changes, "projects", archived),
            "task_payload": payload(changes, "items", archived_task) if archived_task else None,
        }
        backup()
    finally:
        cleanup = []
        for project_id in projects:
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                cleanup.append(project_id)
        findings["rejected_commands"] = [rejected.as_finding() for rejected in commands.rejected]
        findings["cleanup_failed"] = cleanup
        write_findings("write", findings, token)

    assert not cleanup, f"delete these projects of the test account by hand: {cleanup}"
    assert not problems, "\n".join(problems)
