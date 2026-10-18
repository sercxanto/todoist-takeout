# Proposal

> Status: the API questions are answered by contract probes (2026-10-18, see API findings). The storage location and the incremental strategy are design questions. Split off from `add-completed-tasks-backup` (2026-10-18), because archived projects are snapshots per project rather than a time series.

## Why

The sync endpoint returns neither archived projects nor the sections, tasks and comments inside them, so today they are missing from the backup entirely, although they are often exactly the history a user wants to keep. A project that is archived after it was backed up stays in `sync/`, but nothing inside it is ever fetched again, and the next full sync reports all of its objects as missing.

## What Changes

- The tool lists archived projects through `GET /api/v1/projects/archived` and backs up, for each one:
  - the project and its open tasks, sections and sub-projects, from `GET /api/v1/projects/{id}/full`;
  - its completed tasks, from `GET /api/v1/tasks/completed/by_completion_date` with `project_id`, in windows of up to 3 months over the project's lifetime;
  - the comments of its tasks (for tasks with `note_count > 0`) and of the project itself, from `GET /api/v1/comments`, with attachment metadata as in the payload and no attachment files.
- An archived project is fetched once; changes made later to its content are deliberately not captured, as for completed tasks.
- A project that is archived after it was backed up keeps its stored data; its tasks do not silently disappear from the backup.
- Objects of archived projects no longer count as missing when a server-initiated full sync omits them.
- Archived sections cannot be listed through the API. The tool keeps what the sync reported when a section was archived. Sections archived before the first backup are lost apart from the `section_id` of their tasks; the format description says so.
- Where part of this data is not available, the tool warns and skips that part instead of failing.

## Capabilities

### New Capabilities

- `archived-projects-backup`: listing archived projects and backing up their content: open and completed tasks, sections, sub-projects and comments.

### Modified Capabilities

- `backup-storage`: adds the storage location for archived projects and their content.
- `active-data-sync`: objects of archived projects do not count as missing after a full sync.

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
- **Archiving a section:** completes the section's tasks (`checked: true`, `completed_at` set), so they appear in `by_completion_date`. The section appears once in the incremental sync with `is_archived: true` and `archived_at`, and afterwards neither in a full sync nor in `/full`.

## Open Questions

For the design:

- **Storage location:** `sync/projects/<id>.json` like active projects, since the payload carries `is_archived`, or a separate `archived/` tree. Where the open tasks, sections and comments of archived projects go.
- **Completed tasks of archived projects:** the payload and the endpoint are those of `add-completed-tasks-backup`, but its month files `completed/<YYYY-MM>.json` cannot hold them. Those files mirror the unfiltered `by_completion_date` query and are replaced while the month is open (its design D6), so anything added from another source would be dropped again. They need a location of their own, for example per project. The comment files `completed/comments/<task id>.json` can be shared.
- **Project archived while a month is open:** the next fetch of that month no longer returns the project's completed tasks, so they drop out of `completed/<YYYY-MM>.json`. They are back in the backup once this change fetches the newly archived project. Tasks of that project in months that were already closed stay there too, so they are stored twice; the format description should say so.
- **Detecting newly archived projects:** for example projects listed as archived that are not stored as archived yet. What happens when a project is unarchived and archived again.
- **Missing-object count:** the rule for excluding objects of archived projects, for example by the stored project's `is_archived`.
- **Archived sub-projects:** none occurred in the probed accounts; whether they are listed separately or only as `subprojects` of their parent.
