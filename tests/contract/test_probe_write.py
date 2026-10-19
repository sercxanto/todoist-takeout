"""Write probes W1-W8 of add-completed-tasks-backup and add-archived-projects-backup.

A spike, not a regression test. It runs only against the dedicated write account, behind the same
guard as the write contract test, and creates temporary projects that it deletes at the end.

W1-W7 record in `findings/probe-write.json` how completing, uncompleting and archiving show up in
the completed-tasks endpoints, the archived-projects endpoints and the sync. API payloads are
scrubbed; time comparisons are made before scrubbing and recorded as booleans or seconds.

W8 records in `findings/probe-updated-at.json`, as booleans only, whether a task's `updated_at`
changes on completion, on a new comment and when its project is archived and unarchived, and how
archived sub-projects appear in the list of archived projects and in `subprojects` of `/full`.
Run it alone with `-k updated_at`.

Each test fails only when a probe crashes, the cleanup fails or, for W8, a recorded value is not a
boolean.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from todoist_takeout.resources import Classifier
from todoist_takeout.sync import apply_response, run_backup

PREFIX = "todoist-takeout probe"
# Clock skew between this machine and the API, allowed for in the windows of recent completions.
SKEW = timedelta(minutes=10)
# W6: completion dates in the past; 3 days is inside a free plan's 7-day activity limit, 30 is not.
BACKDATED_DAYS = (3, 30)
# W6: candidate names of the completion date argument of item_complete.
COMPLETION_DATE_ARGS = ("completed_at", "date_completed")
# W8: seconds between a change and the step before it, so that a changed `updated_at` cannot
# coincide with the old one.
PAUSE = 1.5


def test_completed_and_archived_behaviour(write_client, contract_support, probes, recorder, tmp_path, write_findings):
    token, client = write_client
    api = probes.Api.for_token(token)
    commands = contract_support.Commands(client)
    scrubber = recorder.Scrubber()
    sections = probes.Sections()
    started = datetime.now(timezone.utc)
    findings: dict = {}
    projects: list = []

    def add(command, args, what):
        return probes.require(commands.add(command, args), f"{what} ({_last_rejection(commands)})")

    def new_project(name):
        project = add("project_add", {"name": f"{PREFIX}: {name}"}, "cannot create a project")
        projects.append(project)
        return project

    def recent(task_id, *, done=lambda found: bool(found["by_date"]), timeout=60.0):
        """Poll both completed-tasks endpoints for a task completed during this test."""

        def fetch():
            since, until = probes.utc(started - SKEW), probes.utc(datetime.now(timezone.utc))
            return {
                "by_date": probes.entries_for(probes.by_completion_date(api, since, until).items, task_id),
                "all": probes.entries_for(probes.completed_all(api, since=since, until=until).items, task_id),
            }

        found, seconds, finished = probes.poll(fetch, done, timeout=timeout)
        return found, {"seconds": seconds, "condition_met": finished}

    activity = {"filter": None}

    def completion_events(task_id, expected):
        """Poll the activity log for completion events of a task, once a working filter is known."""
        if activity["filter"] is None:
            activity["filter"], activity["report"] = probes.activity_filter(api, date_from=probes.day(started))
        if activity["filter"] is None:
            return None, {"result": "no activity filter found"}
        events, seconds, finished = probes.poll(
            lambda: [e for e in probes.activities(api, activity["filter"], date_from=probes.day(started)).items
                     if str(e.get("object_id")) == str(task_id)],
            lambda found: len(found) >= expected, timeout=30.0)
        return events, {"seconds": seconds, "condition_met": finished}

    def comment_counts(**params):
        answer = probes.attempt(lambda: len(probes.comments(api, **params).items))
        return answer["value"] if answer["ok"] else answer["error"]

    def annotated_notes(task_id, **params):
        """The `notes` that tasks/completed returns with annotate_notes for a task's entries."""
        answer = probes.attempt(lambda: [
            len(i.get("notes") or []) for i in probes.entries_for(
                probes.completed_all(api, annotate_notes="true", **params).items, task_id)])
        return answer["value"] if answer["ok"] else answer["error"]

    def entries(found):
        return {
            name: {
                "count": len(items),
                "differing_fields": probes.differing_fields(items),
                "id_fields": probes.id_field_stats(items),
                "payloads": scrubber.scrub(items),
            }
            for name, items in found.items()
        }

    try:
        observer = contract_support.Observer(client)
        project = new_project("completed")

        # W1: a recurring task completed twice.
        with sections.run(findings, "W1_recurring") as entry:
            task = add("item_add", {"content": f"{PREFIX}: recurring", "project_id": project,
                                    "due": {"string": "every day"}}, "cannot create a recurring task")
            observer.changes()
            entry["closes"] = []
            for _ in range(2):
                ok = commands.run("item_close", {"id": task})
                sync_payload = probes.find(observer.changes().get("items"), task)
                entry["closes"].append({
                    "accepted": bool(ok),
                    "sync": probes.describe(sync_payload, "checked", "is_deleted", "completed_at"),
                })
            found, timing = recent(task, done=lambda found: len(found["by_date"]) >= 2 and len(found["all"]) >= 2)
            entry.update(timing=timing, entries=entries(found))
            entry["completed_at_differs"] = {
                name: len({i.get("completed_at") for i in items}) > 1 for name, items in found.items()
            }
            events, timing = completion_events(task, 2)
            entry["activity_log"] = timing if events is None else {
                "timing": timing,
                "completion_events": len(events),
                "event_date_differs": len({e.get("event_date") for e in events}) > 1,
                "fields": probes.field_types(events),
                "extra_data_fields": probes.field_types(e.get("extra_data") for e in events),
                "payloads": scrubber.scrub(events),
            }

        # W2 and W3: complete, uncomplete and complete a task again; compare with the sync.
        with sections.run(findings, "W2_W3_complete_uncomplete") as entry:
            overlap = entry.setdefault("W3_sync_overlap", {})
            task = add("item_add", {"content": f"{PREFIX}: plain", "project_id": project}, "cannot create a task")
            observer.changes()
            entry["complete_accepted"] = bool(commands.run("item_complete", {"id": task}))
            sync_payload = probes.find(observer.changes().get("items"), task)
            first, timing = recent(task)
            entry["after_complete"] = {"timing": timing, "entries": entries(first)}

            full = json.loads(client.post("sync", {"sync_token": "*", "resource_types": '["items"]'}))
            overlap.update(
                incremental_sync=probes.describe(sync_payload, "checked", "is_deleted", "completed_at"),
                in_full_sync=probes.describe(probes.find(full.get("items"), task), "checked"),
                sync_vs_by_date=probes.field_diff(sync_payload, first["by_date"][0] if first["by_date"] else None),
                sync_vs_get_all=probes.field_diff(sync_payload, first["all"][0] if first["all"] else None),
                by_date_vs_get_all=probes.field_diff(first["by_date"][0] if first["by_date"] else None,
                                                     first["all"][0] if first["all"] else None),
                sync_payload=scrubber.scrub(sync_payload),
            )

            entry["uncomplete_accepted"] = bool(commands.run("item_uncomplete", {"id": task}))
            observer.changes()
            gone, timing = recent(task, done=lambda found: not found["by_date"] and not found["all"], timeout=30.0)
            entry["after_uncomplete"] = {"timing": timing, "counts": {k: len(v) for k, v in gone.items()}}

            entry["recomplete_accepted"] = bool(commands.run("item_complete", {"id": task}))
            observer.changes()
            again, timing = recent(task, timeout=30.0)
            entry["after_recomplete"] = {"timing": timing, "entries": entries(again)}
            first_times = {i.get("completed_at") for items in first.values() for i in items}
            entry["recomplete_has_new_completed_at"] = any(
                i.get("completed_at") not in first_times for items in again.values() for i in items
            )
            events, timing = completion_events(task, 2)
            entry["activity_log"] = timing if events is None else {
                "timing": timing,
                "types": [probes.event_type(e) for e in events],
                "completion_events": len(events),
            }

        # W7: comments of a completed task in an active project.
        with sections.run(findings, "W7_comments") as entry:
            task = add("item_add", {"content": f"{PREFIX}: commented", "project_id": project}, "cannot create a task")
            note = commands.add("note_add", {"content": f"{PREFIX}: comment", "item_id": task})
            entry["comment_created"] = note is not None
            entry["complete_accepted"] = bool(commands.run("item_complete", {"id": task}))
            observer.changes()
            found, timing = recent(task)
            since, until = probes.utc(started - SKEW), probes.utc(datetime.now(timezone.utc))
            entry.update(
                timing=timing,
                by_date_note_count=[i.get("note_count") for i in found["by_date"]],
                get_all_annotated_notes=annotated_notes(task, since=since, until=until),
                comments_endpoint_task=comment_counts(task_id=task),
            )
            listed = probes.attempt(lambda: probes.comments(api, task_id=task).items)
            entry["comments_endpoint_payload"] = scrubber.scrub(listed["value"]) if listed["ok"] else listed["error"]

        # W6: completion dates in the past.
        with sections.run(findings, "W6_backdated") as entry:
            for days in BACKDATED_DAYS:
                task = add("item_add", {"content": f"{PREFIX}: backdated {days}", "project_id": project},
                           "cannot create a task")
                when = datetime.now(timezone.utc) - timedelta(days=days)
                result = {"accepted_argument": None}
                for argument in COMPLETION_DATE_ARGS:
                    if commands.run("item_complete", {"id": task, argument: probes.utc(when)}):
                        result["accepted_argument"] = argument
                        break
                observer.changes()
                if result["accepted_argument"]:
                    since, until = probes.utc(when - timedelta(days=1)), probes.utc(when + timedelta(days=1))
                    around = probes.attempt(lambda: {
                        "by_date": len(probes.entries_for(probes.by_completion_date(api, since, until).items, task)),
                        "all": len(probes.entries_for(probes.completed_all(api, since=since, until=until).items, task)),
                    })
                    now_found, timing = recent(task, done=lambda found: bool(found["by_date"] or found["all"]),
                                               timeout=15.0)
                    result.update(
                        around_backdated_time=around["value"] if around["ok"] else around["error"],
                        in_recent_window={k: len(v) for k, v in now_found.items()},
                    )
                entry[f"{days}_days"] = result

        # W4: archiving a project with content.
        with sections.run(findings, "W4_archive_project") as entry:
            archived = new_project("archive")
            section = add("section_add", {"name": f"{PREFIX}: section", "project_id": archived}, "cannot create a section")
            open_task = add("item_add", {"content": f"{PREFIX}: open", "project_id": archived, "section_id": section},
                            "cannot create a task")
            done_task = add("item_add", {"content": f"{PREFIX}: done", "project_id": archived}, "cannot create a task")
            task_note = commands.add("note_add", {"content": f"{PREFIX}: task comment", "item_id": open_task})
            project_note = commands.add("note_add", {"content": f"{PREFIX}: project comment", "project_id": archived})
            done_note = commands.add("note_add", {"content": f"{PREFIX}: done task comment", "item_id": done_task})
            entry["comments_created"] = {
                "task": task_note is not None, "project": project_note is not None, "done_task": done_note is not None,
            }
            commands.run("item_complete", {"id": done_task})
            observer.changes()
            target = tmp_path / "backup"
            run_backup(target, client)

            entry["archive_accepted"] = bool(commands.run("project_archive", {"id": archived}))
            changes = observer.changes()
            objects = {
                "project": ("projects", archived), "section": ("sections", section), "open_task": ("items", open_task),
                "done_task": ("items", done_task), "task_comment": ("notes", task_note),
                "project_comment": ("project_notes", project_note),
            }

            def presence(response):
                return {
                    name: probes.describe(probes.find(response.get(resource), object_id), "is_archived", "is_deleted", "checked")
                    for name, (resource, object_id) in objects.items() if object_id
                }

            entry["incremental_sync"] = presence(changes)

            listed = probes.attempt(lambda: probes.walk_cursor(api, probes.ARCHIVED_PROJECTS, {}, items_key="results", limit=50).items)
            entry["archived_list"] = (
                {"contains_project": probes.find(listed["value"], archived) is not None,
                 "payload": scrubber.scrub(probes.find(listed["value"], archived))}
                if listed["ok"] else listed
            )
            full_project = probes.attempt(lambda: api.get(f"projects/{archived}/full"))
            if full_project["ok"]:
                value = full_project["value"]
                entry["full_project"] = {
                    "top_level_keys": sorted(value),
                    "project": probes.describe(value.get("project"), "is_archived"),
                    "open_task": probes.describe(probes.find(value.get("tasks"), open_task), "checked"),
                    "done_task": probes.describe(probes.find(value.get("tasks"), done_task), "checked"),
                    "section": probes.describe(probes.find(value.get("sections"), section), "is_archived"),
                    "task_comment": probes.describe(probes.find(value.get("notes"), task_note)),
                    "project_comment": probes.describe(probes.find(value.get("notes"), project_note)),
                    "payload": scrubber.scrub(value),
                }
            else:
                entry["full_project"] = full_project
            found, timing = recent(done_task, done=lambda found: bool(found["by_date"] or found["all"]), timeout=15.0)
            in_project = probes.attempt(lambda: len(probes.entries_for(
                probes.completed_all(api, project_id=archived, limit=50, max_pages=1).items, done_task)))
            entry["completed_done_task"] = {
                "by_date": len(found["by_date"]), "all": len(found["all"]),
                "all_with_project_id": in_project["value"] if in_project["ok"] else in_project["error"],
            }
            entry["comments_after_archive"] = {
                "comments_endpoint_open_task": comment_counts(task_id=open_task),
                "comments_endpoint_done_task": comment_counts(task_id=done_task),
                "comments_endpoint_project": comment_counts(project_id=archived),
                "get_all_annotated_notes_done_task": annotated_notes(done_task, project_id=archived),
            }

            # A server-initiated full sync, as the tool would apply it: are the archived project's
            # objects missing, and counted by the warning of design D8?
            fresh = json.loads(client.sync("*"))
            entry["full_sync"] = presence(fresh)
            entry["missing_after_applying_full_sync"] = apply_response(target, fresh, Classifier()).missing

        # W5: archiving a section of an active project.
        with sections.run(findings, "W5_archive_section") as entry:
            active = new_project("section")
            section = add("section_add", {"name": f"{PREFIX}: section", "project_id": active}, "cannot create a section")
            task = add("item_add", {"content": f"{PREFIX}: in section", "project_id": active, "section_id": section},
                       "cannot create a task")
            observer.changes()
            entry["archive_accepted"] = bool(commands.run("section_archive", {"id": section}))
            changes = observer.changes()
            fields = ("is_archived", "archived_at", "is_deleted", "checked", "completed_at")
            entry["incremental_sync"] = {
                "section": probes.describe(probes.find(changes.get("sections"), section), *fields),
                "task": probes.describe(probes.find(changes.get("items"), task), *fields),
            }
            fresh = json.loads(client.sync("*"))
            entry["full_sync"] = {
                "section": probes.describe(probes.find(fresh.get("sections"), section), *fields),
                "task": probes.describe(probes.find(fresh.get("items"), task), *fields),
            }
            full_project = probes.attempt(lambda: api.get(f"projects/{active}/full"))
            entry["full_project"] = (
                {"section": probes.describe(probes.find(full_project["value"].get("sections"), section), *fields),
                 "task": probes.describe(probes.find(full_project["value"].get("tasks"), task), *fields)}
                if full_project["ok"] else full_project
            )
            found, timing = recent(task, done=lambda found: bool(found["by_date"] or found["all"]), timeout=15.0)
            entry["task_in_completed"] = {k: len(v) for k, v in found.items()}
    finally:
        cleanup = []
        for project_id in projects:
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                cleanup.append(project_id)
        findings["activity_filters"] = activity.get("report")
        findings["rejected_commands"] = [rejected.as_finding() for rejected in commands.rejected]
        findings["cleanup_failed"] = cleanup
        write_findings("probe-write", findings, token)

    assert not cleanup, f"delete these projects of the test account by hand: {cleanup}"
    assert not sections.crashed, f"probes that crashed (see findings/probe-write.json): {sections.crashed}"


def _last_rejection(commands) -> str:
    return json.dumps(commands.rejected[-1].as_finding()) if commands.rejected else "no rejection recorded"


def test_updated_at_and_subprojects(write_client, contract_support, probes, write_findings):
    """W8: when `updated_at` of a task changes, and where archived sub-projects are listed."""
    token, client = write_client
    api = probes.Api.for_token(token)
    commands = contract_support.Commands(client)
    sections = probes.Sections()
    started = datetime.now(timezone.utc)
    findings: dict = {}
    projects: list = []

    def add(command, args, what):
        return probes.require(commands.add(command, args), f"{what} ({_last_rejection(commands)})")

    def new_project(name, parent=None):
        args = {"name": f"{PREFIX}: {name}"}
        if parent is not None:
            args["parent_id"] = parent
        project = add("project_add", args, "cannot create a project")
        projects.append(project)
        return project

    def synced(resource, object_id):
        """The payload of an active object, from a full sync of its resource only."""
        response = json.loads(client.post("sync", {"sync_token": "*", "resource_types": json.dumps([resource])}))
        return probes.find(response.get(resource), object_id)

    def completion(task_id, **filters):
        """The task's entry in by_completion_date, polled until it appears."""

        def fetch():
            since, until = probes.utc(started - SKEW), probes.utc(datetime.now(timezone.utc))
            return probes.entries_for(probes.by_completion_date(api, since, until, **filters).items, task_id)

        found, _, _ = probes.poll(fetch, bool, timeout=60.0)
        return found[0] if found else None

    def listed_archived():
        return probes.walk_cursor(api, probes.ARCHIVED_PROJECTS, {}, items_key="results", limit=50).items

    try:
        parent = new_project("updated_at parent")
        child = new_project("updated_at child", parent)
        grandchild = new_project("updated_at grandchild", child)
        open_task = add("item_add", {"content": f"{PREFIX}: open", "project_id": parent}, "cannot create a task")
        done_task = add("item_add", {"content": f"{PREFIX}: done", "project_id": parent}, "cannot create a task")
        commented = add("item_add", {"content": f"{PREFIX}: commented", "project_id": parent}, "cannot create a task")
        observer = contract_support.Observer(client)
        state: dict = {}

        with sections.run(findings, "W8_completion") as entry:
            before = probes.require(synced("items", done_task), "task not in the sync")
            time.sleep(PAUSE)
            entry["complete_accepted"] = bool(commands.run("item_complete", {"id": done_task}))
            in_sync = probes.find(observer.changes().get("items"), done_task)
            state["done_by_date"] = completion(done_task)
            entry["incremental_sync"] = probes.time_change(before, in_sync)
            entry["by_completion_date"] = probes.time_change(before, state["done_by_date"])

        with sections.run(findings, "W8_comment") as entry:
            before = probes.require(synced("items", commented), "task not in the sync")
            time.sleep(PAUSE)
            note = commands.add("note_add", {"content": f"{PREFIX}: comment", "item_id": commented})
            entry["comment_created"] = note is not None
            entry["sync"] = probes.time_change(before, synced("items", commented))

        with sections.run(findings, "W8_archive") as entry:
            task_before = probes.require(synced("items", open_task), "task not in the sync")
            project_before = probes.require(synced("projects", parent), "project not in the sync")
            time.sleep(PAUSE)
            entry["archive_accepted"] = bool(commands.run("project_archive", {"id": parent}))
            observer.changes()
            full = api.get(f"projects/{parent}/full")
            state["task_archived"] = probes.find(full.get("tasks"), open_task)
            listed = listed_archived()
            state["project_archived"] = probes.find(listed, parent)
            entry["open_task_in_full"] = probes.time_change(task_before, state["task_archived"])
            entry["completed_task_in_by_completion_date"] = probes.time_change(
                state.get("done_by_date"), completion(done_task, project_id=parent))
            entry["project_in_archived_list"] = probes.time_change(project_before, state["project_archived"])
            entry["project_in_full"] = probes.time_change(project_before, full.get("project"))

            subprojects = full.get("subprojects") or []
            entry["archived_list"] = {
                name: probes.describe(probes.find(listed, project_id), "is_archived")
                for name, project_id in (("parent", parent), ("child", child), ("grandchild", grandchild))
            }
            entry["parent_full_subprojects"] = {
                "child": probes.describe(probes.find(subprojects, child), "is_archived"),
                "grandchild": probes.describe(probes.find(subprojects, grandchild), "is_archived"),
            }
            child_full = probes.attempt(lambda: api.get(f"projects/{child}/full"))
            entry["child_full_subprojects"] = (
                {"grandchild": probes.describe(probes.find(child_full["value"].get("subprojects"), grandchild),
                                               "is_archived")}
                if child_full["ok"] else child_full
            )

        with sections.run(findings, "W8_unarchive") as entry:
            time.sleep(PAUSE)
            entry["unarchive_accepted"] = bool(commands.run("project_unarchive", {"id": parent}))
            observer.changes()
            task_after = synced("items", open_task)
            entry["open_task_vs_archived"] = probes.time_change(state.get("task_archived"), task_after)
            entry["project_vs_archived"] = probes.time_change(state.get("project_archived"), synced("projects", parent))
            entry["children_active_again"] = {
                "child": probes.describe(synced("projects", child), "is_archived"),
                "grandchild": probes.describe(synced("projects", grandchild), "is_archived"),
            }
    finally:
        cleanup = []
        for project_id in reversed(projects):
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                cleanup.append(project_id)
        findings["rejected_commands"] = [rejected.as_finding() for rejected in commands.rejected]
        findings["cleanup_failed"] = bool(cleanup)
        findings, removed = probes.booleans_only(findings)
        write_findings("probe-updated-at", findings, token)

    assert not cleanup, f"delete these projects of the test account by hand: {cleanup}"
    assert not sections.crashed, f"probes that crashed (see findings/probe-updated-at.json): {sections.crashed}"
    assert not removed, f"values that are not booleans were left out of the findings: {removed}"
