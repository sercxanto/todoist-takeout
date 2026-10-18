"""Read-only probes R1-R7 of add-completed-tasks-backup and add-archived-projects-backup.

A spike, not a regression test: it records how the completed-tasks and archived-projects endpoints
behave for each configured account, in `findings/probe-read-<account>.json`. API payloads in the
findings are scrubbed; everything else is counts, field names and booleans derived from them. The
test fails only when a probe crashes, not because of what the API answers.
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

MAX_MONTHS_BACK = 24  # R1 looks this far back for a month with completed tasks
WINDOW_DAYS = (31, 89, 92, 93, 100, 185)  # R1: which windows by_completion_date accepts
SMALL_PAGE = 5  # R1: pagination check
MAX_ARCHIVED_DETAILS = 3  # R3, R5: archived projects looked at in detail
HISTORY_WINDOW_DAYS = 89  # R6
MAX_HISTORY_WINDOWS = 100


@pytest.mark.parametrize("account", ["read", "write"])
def test_completed_and_archived_endpoints(account, contract_support, probes, recorder, client_for, write_findings):
    variable = contract_support.ACCOUNT_VARIABLES[account]
    token = contract_support.env_token(variable)
    if not token:
        pytest.skip(f"{variable} is not set")

    api = probes.Api.for_token(token)
    sync = json.loads(client_for(token).sync("*"))
    scrubber = recorder.Scrubber()
    sections = probes.Sections()
    now = datetime.now(timezone.utc)
    limits = (sync.get("user_plan_limits") or {}).get("current") or {}
    findings: dict = {
        "plan": {name: limits.get(name) for name in ("plan_name", "completed_tasks", "activity_log_limit")},
    }
    active = {str(p.get("id")): p for p in sync.get("projects") or [] if isinstance(p, dict)}
    joined = probes.parse_time((sync.get("user") or {}).get("joined_at"))
    user_id = str((sync.get("user") or {}).get("id"))

    # R4: the list of archived projects.
    archived: list = []
    with sections.run(findings, "R4_archived_projects") as entry:
        walk = probes.walk_cursor(api, probes.ARCHIVED_PROJECTS, {}, items_key="results", limit=50)
        archived = walk.items
        entry.update(
            count=len(archived),
            pages=walk.pages,
            top_level_keys=walk.keys,
            fields=probes.field_types(archived),
            is_archived=dict(Counter(str(p.get("is_archived")) for p in archived)),
            with_parent=sum(1 for p in archived if p.get("parent_id")),
            in_workspace=sum(1 for p in archived if p.get("workspace_id")),
            also_in_sync=sum(1 for p in archived if str(p.get("id")) in active),
            example=scrubber.scrub(archived[0]) if archived else None,
            deprecation_headers=api.notice_headers(),
        )
        count = probes.attempt(lambda: api.get(probes.ARCHIVED_PROJECTS_COUNT))
        if count["ok"]:
            value = count["value"]
            entry["count_endpoint"] = {
                "keys": sorted(value) if isinstance(value, dict) else probes.json_type(value),
                "equals_listed": len(archived) in (value.values() if isinstance(value, dict) else [value]),
            }
        else:
            entry["count_endpoint"] = count
    archived_ids = {str(p.get("id")) for p in archived}
    detailed = [str(p.get("id")) for p in archived[:MAX_ARCHIVED_DETAILS]]

    # R5: the content of archived projects.
    with sections.run(findings, "R5_full_archived_project") as entry:
        entry["projects"] = []
        for project_id in detailed:
            answer = probes.attempt(lambda: api.get(f"projects/{project_id}/full"))
            if not answer["ok"]:
                entry["projects"].append(answer)
                continue
            full = answer["value"]
            tasks = full.get("tasks") or []
            found_sections = full.get("sections") or []
            entry["projects"].append({
                "top_level_keys": sorted(full),
                "project": probes.describe(full.get("project"), "is_archived"),
                "tasks": len(tasks),
                "tasks_checked": sum(1 for t in tasks if t.get("checked") is True or t.get("completed_at")),
                "sections": len(found_sections),
                "sections_archived": sum(1 for s in found_sections if s.get("archived_at") or s.get("is_archived")),
                "notes": len(full.get("notes") or []),
                "has_comments_count": "comments_count" in full,
                "task_fields": probes.field_types(tasks),
                "section_fields": probes.field_types(found_sections),
                "note_fields": probes.field_types(full.get("notes") or []),
            })

    # R1: by_completion_date for the most recent month with completed tasks.
    month = None
    r1_items: list = []
    with sections.run(findings, "R1_by_completion_date") as entry:
        current = probes.month_start(now)
        for back in range(MAX_MONTHS_BACK):
            start = probes.add_months(current, -back)
            since, until = probes.month_window(start)
            walk = probes.by_completion_date(api, since, until)
            if walk.items:
                month, r1_items = start, walk.items
                break
        if month is None:
            entry["result"] = f"no completed tasks in the last {MAX_MONTHS_BACK} months"
        else:
            since, until = probes.month_window(month)
            times = [probes.parse_time(i.get("completed_at")) for i in r1_items]
            entry.update(
                months_back=probes.months_between(month, current),
                count=len(r1_items),
                pages=walk.pages,
                top_level_keys=walk.keys,
                fields=probes.field_types(r1_items),
                id_fields=probes.id_field_stats(r1_items),
                duplicate_id_and_completed_at=len(r1_items) - len({(str(i.get("id")), i.get("completed_at")) for i in r1_items}),
                completed_at_missing=sum(1 for t in times if t is None),
                completed_at_outside_window=sum(
                    1 for t in times if t and not (probes.parse_time(since) <= t <= probes.parse_time(until) + timedelta(seconds=1))
                ),
                example=scrubber.scrub(r1_items[0]),
                deprecation_headers=api.notice_headers(),
            )
            if len(r1_items) > SMALL_PAGE:
                small = probes.by_completion_date(api, since, until, limit=SMALL_PAGE)
                entry["small_pages"] = {
                    "pages": small.pages,
                    "same_entries": sorted(json.dumps(i, sort_keys=True) for i in small.items)
                    == sorted(json.dumps(i, sort_keys=True) for i in r1_items),
                }
            entry["boundaries"] = _boundaries(api, probes, r1_items)
        entry["windows"] = {}
        for days in WINDOW_DAYS:
            answer = probes.attempt(lambda: api.get(probes.COMPLETED_BY_DATE, {
                "since": probes.utc(now - timedelta(days=days)), "until": probes.utc(now), "limit": 1}))
            entry["windows"][f"{days}_days"] = "accepted" if answer["ok"] else answer["error"]

    # R2: tasks/completed for the same month, with and without annotations.
    r2_items: list = []
    with sections.run(findings, "R2_completed_get_all") as entry:
        if month is None:
            entry["result"] = "skipped: no month with completed tasks"
        else:
            since, until = probes.month_window(month)
            walk = probes.completed_all(api, since=since, until=until, annotate_notes="true", annotate_items="true")
            r2_items = walk.items
            with_notes = [i for i in r2_items if i.get("notes")]
            project_map = walk.maps.get("projects", {})
            entry.update(
                count=len(r2_items),
                same_count_as_r1=len(r2_items) == len(r1_items),
                pages=walk.pages,
                top_level_keys=walk.keys,
                fields=probes.field_types(r2_items),
                id_fields=probes.id_field_stats(r2_items),
                overlap_with_r1=probes.overlap(r2_items, r1_items),
                items_with_notes_key=sum(1 for i in r2_items if "notes" in i),
                items_with_notes=len(with_notes),
                note_fields=probes.field_types(n for i in with_notes for n in i["notes"]),
                example_with_notes=scrubber.scrub(with_notes[0]) if with_notes else None,
                example=scrubber.scrub(r2_items[0]) if r2_items else None,
                projects_map={
                    "count": len(project_map),
                    "archived_flag": sum(1 for p in project_map.values() if isinstance(p, dict) and p.get("is_archived")),
                    "in_archived_list": len(set(project_map) & archived_ids),
                    "in_sync": len(set(project_map) & set(active)),
                    "fields": probes.field_types(project_map.values()),
                },
                sections_map={"count": len(walk.maps.get("sections", {}))},
                deprecation_headers=api.notice_headers(),
            )
            plain = probes.attempt(lambda: api.get(probes.COMPLETED_ALL, {"since": since, "until": until, "limit": 1}))
            entry["without_annotations"] = (
                {"item_fields": probes.field_types(plain["value"].get("items") or [])} if plain["ok"] else plain
            )

    # R3: completions in shared, workspace and archived projects.
    with sections.run(findings, "R3_project_kinds") as entry:
        def kinds(items):
            counter = Counter()
            for item in items:
                project = active.get(str(item.get("project_id")))
                if str(item.get("project_id")) in archived_ids:
                    counter["archived"] += 1
                elif project is None:
                    counter["unknown"] += 1
                elif project.get("workspace_id"):
                    counter["workspace"] += 1
                elif project.get("is_shared") or project.get("shared"):
                    counter["shared"] += 1
                else:
                    counter["personal"] += 1
            return dict(counter)

        entry["r1_by_project_kind"] = kinds(r1_items)
        entry["r2_by_project_kind"] = kinds(r2_items)
        r2_task_ids = {str(i.get("task_id")) for i in r2_items}
        by_others = [i for i in r1_items if i.get("completed_by_uid") not in (None, user_id)]
        entry["r1_completed_by_others"] = {
            "count": len(by_others),
            "by_project_kind": kinds(by_others),
            "also_in_r2": sum(1 for i in by_others if str(i.get("id")) in r2_task_ids),
        }
        entry["r1_missing_in_r2"] = kinds(i for i in r1_items if str(i.get("id")) not in r2_task_ids) if r2_items else None
        entry["archived_projects"] = []
        for project_id in detailed:
            answer = probes.attempt(lambda: probes.completed_all(api, project_id=project_id, limit=20, max_pages=1))
            if not answer["ok"]:
                entry["archived_projects"].append({"get_all": answer})
                continue
            items = answer["value"].items
            result = {"get_all_with_project_id": len(items)}
            newest = max((probes.parse_time(i.get("completed_at")) for i in items if i.get("completed_at")), default=None)
            if newest:
                start = probes.month_start(newest)
                since, until = probes.month_window(start)
                in_month = [
                    i for i in items
                    if (moment := probes.parse_time(i.get("completed_at"))) and probes.month_start(moment) == start
                ]
                seen_ids = set().union(*(probes.task_ids(i) for i in probes.by_completion_date(api, since, until).items))
                filtered = probes.attempt(lambda: probes.by_completion_date(api, since, until, project_id=project_id).items)
                result.update(
                    in_newest_month=len(in_month),
                    found_in_by_completion_date=sum(1 for i in in_month if probes.task_ids(i) & seen_ids),
                    by_completion_date_with_project_id=len(filtered["value"]) if filtered["ok"] else filtered["error"],
                )
            entry["archived_projects"].append(result)
        workspaces = [w for w in sync.get("workspaces") or [] if isinstance(w, dict) and w.get("id")]
        if month is not None and workspaces:
            since, until = probes.month_window(month)
            answer = probes.attempt(lambda: probes.by_completion_date(api, since, until, workspace_id=workspaces[0]["id"]).items)
            entry["with_workspace_id"] = (
                {"count": len(answer["value"]), "by_project_kind": kinds(answer["value"])} if answer["ok"] else answer
            )

    # R6: how far back completions go.
    earliest_window = None
    with sections.run(findings, "R6_history_depth") as entry:
        if joined is None:
            entry["result"] = "the user has no joined_at"
        else:
            nonempty, empty, until, reached = [], 0, now, False
            for _ in range(MAX_HISTORY_WINDOWS):
                since = until - timedelta(days=HISTORY_WINDOW_DAYS)
                page = api.get(probes.COMPLETED_BY_DATE, {"since": probes.utc(since), "until": probes.utc(until), "limit": 1})
                if page.get("items"):
                    nonempty.append(probes.months_between(joined, since))
                    earliest_window = (since, until)
                else:
                    empty += 1
                if since < joined:
                    reached = True
                    break
                until = since
            before = api.get(probes.COMPLETED_BY_DATE, {
                "since": probes.utc(joined - timedelta(days=HISTORY_WINDOW_DAYS)), "until": probes.utc(joined), "limit": 1})
            entry.update(
                windows=len(nonempty) + empty,
                empty_windows=empty,
                earliest_nonempty_months_after_joined=min(nonempty) if nonempty else None,
                reached_joined_at=reached,
                completions_before_joined_at=bool(before.get("items")),
            )

    # R7: task completions in the activity log, compared with R1 and R2.
    with sections.run(findings, "R7_activity_log") as entry:
        chosen, entry["filters"] = probes.activity_filter(api)
        entry["chosen_filter"] = chosen
        if chosen is None:
            entry["result"] = "no filter returned completion events only"
        else:
            if month is not None:
                last_day = probes.add_months(month, 1) - timedelta(days=1)
                walk = probes.activities(api, chosen, date_from=probes.day(month), date_to=probes.day(last_day))
                events = walk.items
                per_object = Counter(str(e.get("object_id")) for e in events)
                r2_task_ids = {str(i.get("task_id")) for i in r2_items}
                r1_ids = {str(i.get("id")) for i in r1_items}
                entry["month"] = {
                    "events": len(events),
                    "pages": walk.pages,
                    "top_level_keys": walk.keys,
                    "types": dict(Counter(probes.event_type(e) for e in events)),
                    "fields": probes.field_types(events),
                    "extra_data_fields": probes.field_types(e.get("extra_data") for e in events),
                    "objects": len(per_object),
                    "objects_with_several_events": sum(1 for n in per_object.values() if n > 1),
                    "same_count_as_r2": len(events) == len(r2_items),
                    "r2_tasks_covered": len(r2_task_ids & set(per_object)),
                    "r2_tasks": len(r2_task_ids),
                    "r1_tasks_covered": len(r1_ids & set(per_object)),
                    "r1_tasks": len(r1_ids),
                    "in_archived_projects": sum(1 for e in events if str(e.get("parent_project_id")) in archived_ids),
                    "initiators": len({e.get("initiator_id") for e in events}),
                    "example": scrubber.scrub(events[0]) if events else None,
                    "deprecation_headers": api.notice_headers(),
                }
                annotated = probes.attempt(lambda: probes.activities(
                    api, chosen, date_from=probes.day(month), date_to=probes.day(last_day),
                    annotate_notes="true", annotate_parents="true", max_pages=1).items)
                entry["annotated"] = (
                    {"fields": probes.field_types(annotated["value"]),
                     "extra_data_fields": probes.field_types(e.get("extra_data") for e in annotated["value"])}
                    if annotated["ok"] else annotated
                )
            if earliest_window is not None:
                since, until = earliest_window
                early = probes.attempt(lambda: probes.activities(
                    api, chosen, date_from=probes.day(since), date_to=probes.day(until), max_pages=1).items)
                entry["earliest_completion_window"] = (
                    {"events": len(early["value"])} if early["ok"] else early
                )
            if detailed:
                in_archived = probes.attempt(lambda: probes.activities(
                    api, chosen, parent_project_id=detailed[0], max_pages=1).items)
                entry["archived_project_filter"] = (
                    {"events": len(in_archived["value"])} if in_archived["ok"] else in_archived
                )

    write_findings(f"probe-read-{account}", findings, token)
    assert not sections.crashed, f"probes that crashed (see findings/probe-read-{account}.json): {sections.crashed}"


def _boundaries(api, probes, items) -> dict:
    """Whether `since` and `until` include an item completed exactly at the boundary."""
    item = next((i for i in items if probes.parse_time(i.get("completed_at"))), None)
    if item is None:
        return {"result": "no item with completed_at"}
    raw = item["completed_at"]
    moment = probes.parse_time(raw)
    day = timedelta(days=1)
    cases = {
        "since_equals_completed_at": (raw, probes.utc(moment + day)),
        "until_equals_completed_at": (probes.utc(moment - day), raw),
        "until_truncated_to_seconds": (probes.utc(moment - day), probes.utc(moment)),
    }
    result = {"completed_at_has_fraction": "." in raw}
    for name, (since, until) in cases.items():
        answer = probes.attempt(lambda: probes.by_completion_date(api, since, until).items)
        result[name] = (
            {"contains_item": any(probes.task_ids(item) & probes.task_ids(i) and i.get("completed_at") == raw for i in answer["value"])}
            if answer["ok"] else answer["error"]
        )
    return result
