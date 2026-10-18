# Proposal

> Status: placeholder. The scope is agreed, but several questions below depend on facts about the API. Answer them, preferably with the contract test from `add-active-data-sync`, before writing the specs and design.

## Why

Completed tasks are the part of the history the user cares about most, and the incremental sync of active data does not include them. Without them the backup is incomplete and not good enough for a migration. Accounts can hold tens of thousands of completed tasks, so they have to be fetched efficiently and in a way that survives interruptions.

Archived projects have the same gap. The sync endpoint returns neither archived projects nor the sections and tasks inside them, so today they are missing from the backup entirely, although they are often exactly the history a user wants to keep.

## What Changes

- The tool backs up all completed tasks through the endpoint `/api/v1/tasks/completed/by_completion_date`, including the full history from the start of the account.
- Completed tasks are stored as one JSON file per month, at `completed/<YYYY-MM>.json`, with month boundaries in UTC. Each file holds the raw API payloads unchanged.
- The current month is written too, so the backup is complete after every run and could be used for a migration at any time.
- Proposed but not yet confirmed: a single forward marker in `state.json`, called `completed_until`, covers both the initial backfill and later incremental runs. A run walks month by month from the marker to now, fetches each month with a small overlap, merges the results into the month file without duplicates, writes the file atomically, and only then advances the marker.
- Months that have passed are never rewritten. Changes made later to completed or archived tasks are deliberately not captured.
- On plans without access to completed tasks, the tool warns and skips this part instead of failing.
- The tool also backs up archived projects, archived sections, and the tasks they contain, both completed and not completed, together with their comments where the API provides them.
- A project that is archived after it was backed up keeps its stored data; its tasks do not silently disappear from the backup.

## Capabilities

### New Capabilities

- `completed-tasks-backup`: fetching completed tasks incrementally, splitting them into month files, removing duplicates, and handling plan limitations.
- `archived-projects-backup`: listing archived projects and archived sections, and backing up their content, including tasks that are not completed.

### Modified Capabilities

- `backup-storage`: adds the `completed/` layout, the `completed_until` checkpoint, and the storage location for archived projects and their content. This capability is introduced by `add-active-data-sync` and must be archived before this change modifies it.

## Impact

- Depends on `add-active-data-sync`, for the CLI, API access, storage and checkpoint handling.
- The first run on large accounts takes several hundred requests and must stay within the rate limits.
- Depends on the API endpoints for archived projects and their content. The number of requests grows with the number of archived projects.

## Open Questions

- **Deduplication key:** Recurring tasks may be completed several times under the same task ID. Is there a separate ID for each completion, or is the key task ID plus `completed_at`?
- **Comments and attachments of completed tasks:** Does the completed-tasks endpoint include them, for example through a parameter like the old `annotate_notes`? If it does not, fetching comments per task means tens of thousands of requests, and this becomes a separate design problem. The source code of the official SDK may answer this.
- **Overlap with the sync:** Tasks completed while the tool is running may show up both in `sync/items/` and in `completed/`. Do the two endpoints return the same object format? Where are the two sources reconciled?
- **Free plans:** Does the endpoint reject the request, return a limited history, or return nothing? The tool must handle every case without failing. *Observed (2026-10-18):* the Free test account's `user_plan_limits.current` has `completed_tasks: true` and `activity_log_limit: 7`. The endpoint is therefore probably available with a limited history; a contract test must confirm it.
- **Forward marker:** Confirm the `completed_until` approach, and the account start date (for example `joined_at`) as the starting point of the backfill.
- **Archived projects endpoint:** Which endpoint lists them (`/api/v1/projects/archived`?), and how does it paginate?
- **Open tasks in archived projects:** Can uncompleted tasks of an archived project be fetched at all, and through which endpoint? If they cannot, which part is lost, and can the backup at least warn about it?
- **Storage location:** Do archived projects go into `sync/projects/<id>.json` like active ones (the payload carries `is_archived`), or into a separate `archived/` tree? Where do their open tasks go, since they do not fit the month files?
- **Archiving between runs:** What does the incremental sync return when a stored project is archived? Does it send the project with `is_archived: true`, and are its tasks reported as deleted? The tool must not turn them into tombstones. *Answered by the write contract test of `add-active-data-sync` (2026-10-18):* the next incremental sync returns the project with `is_archived: true` and `is_deleted: false`, so `sync/projects/<id>.json` is updated with that payload. Its tasks are not in the response at all, neither as deleted nor as archived. They therefore stay live and unchanged in `sync/items/`, and no tombstones are written. Still open: whether a later full sync omits them too, so that the missing-object warning of `add-active-data-sync` (design D8) counts them, and how the backup should mark these tasks as belonging to an archived project.
- **Archived sections in active projects:** Does the full sync include them, or only the incremental sync at the moment of archiving?
