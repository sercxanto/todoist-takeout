"""Write contract test of the archived-projects backup against the dedicated test account (design D14).

It creates a project with a sub-project, a section, an open and a completed task and comments, backs
them up, archives the parent and backs up again. Then it forces a full sync, as the server does when
it no longer accepts a sync token, and checks both snapshots, the comment files, and that no stored
object of the archived projects counts as missing. The findings hold booleans and counts only.
"""

import json
import logging
from datetime import datetime, timezone

from todoist_takeout import archived, completed, storage, sync
from todoist_takeout.sync import run_backup

PREFIX = "todoist-takeout contract test"
MISSING_WARNING = "were not in the full sync; they may have been deleted"
LEFT_OUT_INFO = "were not in the full sync, as expected"


def read_json(path):
    data = storage.read_bytes(path)
    return None if data is None else json.loads(data)


def ids(objects) -> set[str]:
    return {str(obj.get("id")) for obj in objects or [] if isinstance(obj, dict)}


def test_archived_projects_backup(write_client, contract_support, tmp_path, caplog, write_findings):
    token, client = write_client
    commands = contract_support.Commands(client)
    target = tmp_path / "backup"
    started = datetime.now(timezone.utc)
    findings: dict = {}
    problems: list[str] = []
    projects: list = []

    def add(command, args, what):
        new = commands.add(command, args)
        assert new, f"cannot create {what}: {[r.as_finding() for r in commands.rejected]}"
        return str(new)

    try:
        parent = add("project_add", {"name": f"{PREFIX}: archived parent"}, "a project")
        projects.append(parent)
        child = add("project_add", {"name": f"{PREFIX}: archived child", "parent_id": parent}, "a sub-project")
        projects.append(child)
        section = add("section_add", {"name": f"{PREFIX}: section", "project_id": parent}, "a section")
        open_task = add("item_add", {"content": f"{PREFIX}: open", "project_id": parent, "section_id": section},
                        "a task")
        done_task = add("item_add", {"content": f"{PREFIX}: done", "project_id": parent}, "a task")
        child_task = add("item_add", {"content": f"{PREFIX}: child task", "project_id": child}, "a task")
        notes = {
            "open": add("note_add", {"content": f"{PREFIX}: comment", "item_id": open_task}, "a comment"),
            "done": add("note_add", {"content": f"{PREFIX}: comment", "item_id": done_task}, "a comment"),
            "project": add("note_add", {"content": f"{PREFIX}: comment", "project_id": parent}, "a comment"),
        }
        findings["complete_accepted"] = bool(commands.run("item_complete", {"id": done_task}))
        findings["completion_listed"] = contract_support.completion_listed(client, parent, done_task, started)
        run_backup(target, client)  # sync/ holds the objects before the archiving

        findings["archive_accepted"] = bool(commands.run("project_archive", {"id": parent}))
        run_backup(target, client)  # the incremental sync reports the archiving; the snapshots follow

        # Force a full sync on the next run.
        state = storage.load_or_create_state(target)
        state.pop("sync_token", None)
        storage.write_state(target, state)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="todoist_takeout"):
            run_backup(target, client)
        messages = [(record.levelno, record.getMessage()) for record in caplog.records]
        findings["missing_warning"] = any(MISSING_WARNING in text for level, text in messages
                                          if level >= logging.WARNING)
        findings["left_out_logged"] = any(LEFT_OUT_INFO in text for _level, text in messages)
        if findings["missing_warning"]:
            problems.append("the full sync after archiving logged the missing-object warning")
        if not findings["left_out_logged"]:
            problems.append("the full sync after archiving did not log the left-out objects at INFO")

        limits = sync.current_plan_limits(target)
        findings["plan"] = {"completed_tasks": limits.get("completed_tasks"), "comments": limits.get("comments")}
        parent_dir, child_dir = archived.snapshot_dir(target, parent), archived.snapshot_dir(target, child)
        for name, directory, project_id, tasks in (("parent", parent_dir, parent, {open_task}),
                                                    ("child", child_dir, child, {child_task})):
            project = read_json(directory / archived.PROJECT_FILE)
            full = read_json(directory / archived.FULL_FILE)
            findings[name] = {"project_json": project is not None, "full_json": full is not None}
            if project is None or full is None:
                problems.append(f"the {name} snapshot is incomplete ({findings[name]})")
                continue
            findings[name]["project_is_archived"] = project.get("is_archived")
            findings[name]["open_tasks"] = len(full.get("tasks") or [])
            if str(project.get("id")) != project_id or str((full.get("project") or {}).get("id")) != project_id:
                problems.append(f"the {name} snapshot holds another project")
            if ids(full.get("tasks")) != tasks:
                problems.append(f"the {name} snapshot's full.json does not hold exactly its open task")
        full = read_json(parent_dir / archived.FULL_FILE) or {}
        if section not in ids(full.get("sections")):
            problems.append("the parent snapshot's full.json does not hold the section")

        stored_completed = read_json(parent_dir / archived.COMPLETED_FILE)
        findings["completed_json"] = None if stored_completed is None else len(stored_completed.get("items") or [])
        if limits.get("completed_tasks") is False:
            if stored_completed is not None:
                problems.append("completed.json was written although the plan has no completed tasks")
        elif stored_completed is None or done_task not in ids(stored_completed.get("items")):
            problems.append("completed.json does not hold the completed task")

        comment_files = {
            "open": parent_dir / archived.COMMENTS_DIR / f"{storage.encode_key(open_task)}{completed.JSON_SUFFIX}",
            "done": parent_dir / archived.COMMENTS_DIR / f"{storage.encode_key(done_task)}{completed.JSON_SUFFIX}",
            "project": parent_dir / archived.PROJECT_COMMENTS_FILE,
        }
        findings["comment_files"] = {}
        for name, path in comment_files.items():
            stored = read_json(path)
            findings["comment_files"][name] = None if stored is None else len(stored.get("results") or [])
            if limits.get("comments") is False:
                if stored is not None:
                    problems.append(f"{path.name} was written although the plan has no comments")
            elif stored is None or notes[name] not in ids(stored.get("results")):
                problems.append(f"the {name} comment is not stored in {path.relative_to(target).as_posix()}")
    finally:
        cleanup = []
        for project_id in reversed(projects):
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                cleanup.append(project_id)
        findings["rejected_commands"] = len(commands.rejected)
        findings["cleanup_failed"] = len(cleanup)
        write_findings("archived-write", findings, token)

    assert not cleanup, f"delete these projects of the test account by hand: {cleanup}"
    assert not problems, "\n".join(problems)
