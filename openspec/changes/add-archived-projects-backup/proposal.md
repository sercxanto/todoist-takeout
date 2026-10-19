# Proposal

> Status: the API questions are answered by contract probes (2026-10-18, see API findings). The design questions are settled in `design.md` (2026-10-19, see Open Questions); the behaviour of `updated_at` is checked by a last probe (tasks 1.1 to 1.3). Split off from `add-completed-tasks-backup` (2026-10-18), because archived projects are snapshots per project rather than a time series.

## Why

The sync endpoint returns neither archived projects nor the sections, tasks and comments inside them, so today they are missing from the backup entirely, although they are often exactly the history a user wants to keep. A project that is archived after it was backed up stays in `sync/`, but nothing inside it is ever fetched again, and the next full sync reports all of its objects as missing. Completed tasks have the same problem today: the incremental sync returns a completed task once, it stays in `sync/items/`, and every later server-initiated full sync counts it as missing, so the warning fires for ordinary completions rather than only for possible deletions.

## What Changes

- The tool lists archived projects through `GET /api/v1/projects/archived` and backs up, for each one:
  - the project and its open tasks, sections and sub-projects, from `GET /api/v1/projects/{id}/full`;
  - its completed tasks, from `GET /api/v1/tasks/completed/by_completion_date` with `project_id`, in windows of up to 3 months over the project's lifetime;
  - the comments of its tasks (for tasks with `note_count > 0`) and of the project itself, from `GET /api/v1/comments`, with attachment metadata as in the payload and no attachment files.
- An archived project is fetched once; changes made later to its content are deliberately not captured, as for completed tasks.
- A project that is archived after it was backed up keeps its stored data; its tasks do not silently disappear from the backup.
- Objects of archived projects and completed tasks (stored with `checked: true`) no longer count as missing when a server-initiated full sync omits them.
- Archived sections cannot be listed through the API. The tool keeps what the sync reported when a section was archived. Sections archived before the first backup are lost apart from the `section_id` of their tasks; the format description says so.
- Where part of this data is not available, the tool warns and skips that part instead of failing.

## Capabilities

### New Capabilities

- `archived-projects-backup`: listing archived projects and backing up their content: open and completed tasks, sections, sub-projects and comments.

### Modified Capabilities

- `backup-storage`: adds the storage location for archived projects and their content.
- `active-data-sync`: objects of archived projects and completed tasks do not count as missing after a full sync.

## Impact

- Builds on the CLI, API access, storage and checkpoint handling of `add-active-data-sync` (archived), and on `add-completed-tasks-backup` for GET requests and pagination, fetching completed tasks and comments, and the comment files of completed tasks.
- Requests per archived project: one for `/full`, one per 3-month window of its lifetime for completed tasks, and one per task with comments plus one for project comments. An account with 56 archived projects needs a few hundred requests on the first run; later runs fetch only newly archived projects.

## API findings

From contract probes against a Pro and a Free account (2026-10-18, `tests/contract/test_probe_*.py`). The findings files are not committed, so the results are recorded here.

- **`projects/archived`:** `{results, next_cursor}` with cursor pagination. The project payloads have the same schema as in the sync, with `is_archived: true`. `projects/archived/count` matches the list. Archived projects are not in the sync.
- **`projects/{id}/full`:** top-level keys `project`, `tasks`, `sections`, `subprojects`, `collaborators`, `collaborator_states`, `folder`, `comments_count`. `tasks` holds only open tasks, `sections` only sections that are not archived; there are no comments.
- **Completed tasks:** `by_completion_date` omits archived projects without a filter and returns all of a project's completions with `project_id`. The deprecated `tasks/completed` includes them without a filter; the activity log returns events with `parent_project_id`, but its history is limited (5 events against 18 entries in `tasks/completed` for one project).
- **Comments:** `GET comments?task_id=…` works for open and completed tasks of an archived project, and `?project_id=…` for project comments.
- **Archiving a project:** the next incremental sync returns only the project, with `is_archived: true` and `is_deleted: false`; its sections, tasks and comments are not in it and stay live and unchanged in `sync/`. The next full sync omits the project and all of its content, so the missing-object warning of `add-active-data-sync` (design D8) counts them (6 objects in the probe).
- **Completed tasks in the sync:** after a task is completed, the next incremental sync returns it with `checked: true` and `completed_at` set; the next full sync omits it. Its sync payload equals its `by_completion_date` entry, with no field and no value differing. The missing-object warning therefore counts every completed task stored in `sync/items/`.
- **Task fields across sources:** sync `items`, `by_completion_date` entries and the `tasks` of `/full` have the same 28 fields, none more or fewer, including `updated_at` (a string). Sections from `/full` have `updated_at` and `archived_at`. Entries of the deprecated `tasks/completed` have no `updated_at` of their own; the fields of their `item_object` are not recorded. Whether `updated_at` changes when a task is completed or commented, or when its project is archived or unarchived, is unknown: the probes scrub timestamps. That decides whether "newest `updated_at` wins" can tell which of several stored copies of a task is current.
- **Archiving a section:** completes the section's tasks (`checked: true`, `completed_at` set), so they appear in `by_completion_date`. The section appears once in the incremental sync with `is_archived: true` and `archived_at`, and afterwards neither in a full sync nor in `/full`.

## Open Questions

Resolved in `design.md`:

- **Storage location:** a separate tree, one self-contained directory per project, `archived/<project id>/`, with `full.json`, `completed.json`, `comments/<task id>.json`, `project_comments.json` and `project.json` (design D2). `sync/` stays as it is and keeps accumulating; nothing is removed from it once a snapshot holds it.
- **Completed tasks of archived projects:** in `archived/<project id>/completed.json`, merged from all windows (D2, D7). The comments of an archived project's tasks go to the snapshot's own `comments/`, not to `completed/comments/`, because those files are refreshed while their month is open and a snapshot is frozen (D2).
- **Project archived while a month is open:** accepted as described: the tasks drop out of the open month's file and are back in the snapshot; tasks in closed months are stored twice. The format description lists every place a task can be stored and how to pick the current copy (D11).
- **Detecting newly archived projects:** the listed payload is compared byte for byte with the stored `project.json`; a missing file means a new or unfinished snapshot, different bytes mean the project is fetched again, for example after it was unarchived and archived again (D4, D5). `project.json` is written last and serves as the checkpoint (D3).
- **Missing-object count:** excluded are projects and sections stored with `is_archived: true`, tasks stored with `checked: true`, objects whose `project_id` names an archived project, and comments of excluded tasks. The warning counts the rest; the excluded count is logged at INFO (D9).
- **Archived sub-projects:** every archived entry of `subprojects` is backed up with its own snapshot, whether the list of archived projects contains it or not (D8).

Still open, without effect on the code:

- Whether `updated_at` changes when a task is completed or commented, or when its project is archived or unarchived. It decides only the format description's rule for picking the current copy of a task (design D11, tasks 1.1 to 1.3).
- Whether `subprojects` holds direct children only or all descendants. Design D8 handles both.
