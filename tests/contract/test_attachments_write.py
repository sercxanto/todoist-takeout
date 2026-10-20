"""Write contract test of the attachment download against the dedicated test account (add-attachment-
downloads, task 7.3).

It uploads a small text file through `POST /uploads`, adds it as the comment of a new task, and backs
up the account: the file must be stored under `attachments/<comment id>/` byte for byte, with one file
request. Then it completes the task, waits until the completion is listed, archives the project and
backs up again, which stores the comment once more in `completed/` and `archived/` as far as the plan
allows: that run must make no file request. Before the first backup it runs the download check of
the read-only test on the new file. Everything it created is deleted again, the upload included. The
findings hold booleans, counts and statuses only.
"""

import json
from datetime import datetime, timezone

from todoist_takeout import archived, attachments, completed, storage, sync
from todoist_takeout.api import Client, UrllibTransport
from todoist_takeout.sync import run_backup

PREFIX = "todoist-takeout contract test"
FILE_NAME = "contract test: Notiz.txt"  # the colon needs cleaning (design D8)
CONTENT = b"todoist-takeout contract test attachment\n" * 64


def read_json(path):
    data = storage.read_bytes(path)
    return None if data is None else json.loads(data)


def holds(comments, comment_id) -> bool:
    return any(isinstance(c, dict) and str(c.get("id")) == comment_id for c in comments or [])


def test_attachment_is_downloaded_once(write_client, contract_support, recorder, tmp_path, write_findings):
    token, client = write_client
    commands = contract_support.Commands(client)
    target = tmp_path / "backup"
    started = datetime.now(timezone.utc)
    findings: dict = {}
    problems: list[str] = []
    projects: list[str] = []
    uploads: list[str] = []
    created: set[str] = set()

    def add(command, args, what):
        new = commands.add(command, args)
        assert new, f"cannot create {what}: {[r.as_finding() for r in commands.rejected]}"
        created.add(str(new))
        return str(new)

    def backup() -> list[str]:
        """Run the tool on the target; return the URLs of its file requests."""
        transport = contract_support.CountingTransport(UrllibTransport(token))
        run_backup(target, Client(transport))
        return transport.file_requests

    try:
        project = add("project_add", {"name": f"{PREFIX}: attachments"}, "a project")
        projects.append(project)
        task = add("item_add", {"content": f"{PREFIX}: task", "project_id": project}, "a task")
        uploaded = contract_support.upload_file(token, project, FILE_NAME, CONTENT, "text/plain")
        uploads.append(uploaded["file_url"])
        note = add(
            "note_add",
            {"item_id": task, "content": f"{PREFIX}: file", "file_attachment": uploaded},
            "a comment with a file",
        )

        listed = json.loads(client.get(completed.COMMENTS, {"task_id": task}))["results"]
        [attachment] = [a for a in map(attachments.uploaded_file, listed) if a is not None]
        findings["check_download"] = contract_support.check_download(client, attachment, tmp_path / "check")

        requests = backup()
        directory = target / attachments.ATTACHMENTS_DIR / storage.encode_key(note)
        stored = sorted(directory.iterdir()) if directory.is_dir() else []
        findings["first_run"] = {"file_requests": len(requests), "files": len(stored)}
        if requests != [attachment.url]:
            problems.append(f"the first run made {len(requests)} file requests, expected one")
        if [p.name for p in stored] != [storage.attachment_file_name(attachment.file_name)]:
            problems.append("attachments/<comment id>/ does not hold exactly the file under its cleaned name")
        elif stored[0].read_bytes() != CONTENT:
            problems.append("the stored file differs from the uploaded bytes")

        in_sync = read_json(target / sync.SYNC_DIR / "notes" / f"{storage.encode_key(note)}{sync.JSON_SUFFIX}")
        if in_sync is None:
            problems.append("the comment is not stored in sync/notes/")
        else:
            expected = json.loads(recorder.SHAPES.read_bytes())["file_attachments"]
            added, removed = recorder.compare_shapes(
                expected, recorder.attachment_shape_of(recorder.uploaded_attachments(in_sync))
            )
            findings["shape"] = {"added": added, "removed": removed}
            problems += [f"file_attachment has what shapes.json lacks: {line}" for line in added]

        findings["complete_accepted"] = bool(commands.run("item_complete", {"id": task}))
        findings["completion_listed"] = contract_support.completion_listed(client, project, task, started)
        findings["archive_accepted"] = bool(commands.run("project_archive", {"id": project}))
        requests = backup()
        task_file = f"{storage.encode_key(task)}{completed.JSON_SUFFIX}"
        copies = {
            "completed": target / completed.COMPLETED_DIR / completed.COMMENTS_DIR / task_file,
            "archived": archived.snapshot_dir(target, project) / archived.COMMENTS_DIR / task_file,
        }
        findings["second_run"] = {
            "file_requests": len(requests),
            "copies": {name: holds((read_json(path) or {}).get("results"), note) for name, path in copies.items()},
            "files": len(list(directory.iterdir())) if directory.is_dir() else 0,
        }
        findings["plan"] = {key: sync.current_plan_limits(target).get(key) for key in ("completed_tasks", "comments")}
        if requests:
            problems.append(f"the second run made {len(requests)} file requests, expected none")
        if findings["second_run"]["files"] != 1:
            problems.append("the second run changed the comment's directory under attachments/")
    finally:
        cleanup: dict = {"projects_not_deleted": 0, "uploads_deleted": []}
        for project_id in reversed(projects):
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                cleanup["projects_not_deleted"] += 1
        for url in uploads:
            cleanup["uploads_deleted"].append(contract_support.delete_upload(token, url))
        full = json.loads(client.sync("*"))
        listed_archived = json.loads(client.get(archived.ARCHIVED_PROJECTS, {"limit": "200"})).get("results") or []
        cleanup["left_in_sync"] = sum(
            1
            for resource in ("projects", "items", "notes", "project_notes")
            for obj in full.get(resource) or []
            if isinstance(obj, dict) and not obj.get("is_deleted") and str(obj.get("id")) in created
        )
        cleanup["left_archived"] = sum(
            1 for p in listed_archived if isinstance(p, dict) and str(p.get("id")) in created
        )
        findings["cleanup"] = cleanup
        findings["rejected_commands"] = len(commands.rejected)
        write_findings("attachments-write", findings, token)

    assert not cleanup["projects_not_deleted"] and not cleanup["left_in_sync"] and not cleanup["left_archived"], (
        f"delete the projects named {PREFIX!r} of the test account by hand: {cleanup}"
    )
    # 404: already gone, as after deleting its comment in the probe W9.
    assert all(200 <= status < 300 or status == 404 for status in cleanup["uploads_deleted"]), (
        f"uploads not deleted: {cleanup}"
    )
    assert not problems, "\n".join(problems)
