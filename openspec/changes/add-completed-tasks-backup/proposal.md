# Proposal

> Status: the API questions are answered by contract probes (2026-10-18, see API findings), and the source of recurring completions is decided (variant D below). Next: specs and design. Archived projects were split off into `add-archived-projects-backup` (2026-10-18).

## Why

Completed tasks are the part of the history the user cares about most, and the incremental sync of active data does not include them. Without them the backup is incomplete and not good enough for a migration. Accounts can hold tens of thousands of completed tasks, so they have to be fetched efficiently and in a way that survives interruptions.

A recurring task is never completed as a task: each completion moves it to its next date. Its completions exist only as events, and the endpoint for completed tasks does not return them. In the sampled month of a real account, about a third of all completions were of this kind.

## What Changes

- **Completed tasks:** the tool backs up every completed task since the account was created, from `GET /api/v1/tasks/completed/by_completion_date`. It returns full task objects, the same payload as the sync's `items`.
- **Completion events:** the tool also backs up one event per completion, including the completions of recurring tasks. Completions before the start of the first run (T0) come once from `GET /api/v1/tasks/completed`; completions from T0 on come from the activity log (`GET /api/v1/activities`). The two sources cover separate time ranges, so events never need to be matched across them.
- **Comments of completed tasks:** for each completed task whose payload has `note_count > 0`, the tool backs up its comments from `GET /api/v1/comments?task_id=…`, with their attachment metadata as it appears in the payload. Attachment files are not downloaded.
- Completed tasks are stored as one JSON file per month, at `completed/<YYYY-MM>.json`, with month boundaries in UTC. Files hold the raw API payloads unchanged. Where events and comments go within this layout is a design question.
- The current month is written too, so the backup is complete after every run and could be used for a migration at any time.
- A checkpoint in `state.json` covers both the initial backfill and later incremental runs, so that an interrupted run resumes without fetching stored months again. Months that have been closed are never fetched again; changes made later to completed tasks are deliberately not captured.
- Limitations degrade to warnings, never to failures:
  - the plan does not include completed tasks (`user_plan_limits.current.completed_tasks` is false): this part is skipped;
  - `tasks/completed` is no longer available: recurring completions before T0 cannot be backed up;
  - the time since the last run exceeds the activity log's limit (7 days on the Free plan): completion events in the gap may be lost.

## Capabilities

### New Capabilities

- `completed-tasks-backup`: fetching completed tasks, completion events and the comments of completed tasks incrementally, the checkpoint, and handling plan limitations and the deprecated endpoint.

### Modified Capabilities

- `backup-storage`: adds the `completed/` layout, the completed-tasks checkpoint and T0 in `state.json`, and their description in the format documentation.

## Impact

- Builds on the CLI, API access, storage and checkpoint handling of `add-active-data-sync` (archived).
- The API client sends only form-encoded POST requests today; all endpoints here are GET requests with query parameters.
- The plan-limit check gains a row for `completed_tasks`.
- Requests on the first run: one or more per month since the account was created (about 120 for ten years), the pages of the one-time event backfill (200 events per page), and one per completed task with comments. Later runs: a few requests for the open months, the activity log since the last run, and new comments.
- Activity log event IDs are integers larger than 64 bits. The tool keeps them exactly; the format description warns that JavaScript and SQLite `INTEGER` would corrupt them.
- Completed tasks inside archived projects are not returned without a project filter; `add-archived-projects-backup` fetches them. Completion events from both sources include archived projects.

## API findings

From contract probes against a Pro and a Free account (2026-10-18, `tests/contract/test_probe_*.py`). The findings files are not committed, so the results are recorded here.

- **`by_completion_date`:**
  - One entry per task, in its current state, with `id` being the task ID. The payload is identical to the task's payload in the incremental sync.
  - Cursor pagination with `limit` up to 200; `next_cursor` is absent on the last page.
  - `since` and `until` are both inclusive, compared to the microsecond, so a month ends at `…T23:59:59.999999Z`. Windows of 93 days are accepted, 100 days are rejected (`INVALID_ARGUMENT_VALUE`, "must not exceed 3 months").
  - An uncompleted task disappears; completed again, it returns with a new `completed_at`. New completions are visible within a second.
  - It includes shared projects, with completions by other people. It excludes archived projects unless `project_id` is given. It never includes completions of recurring tasks.
  - History reaches back to the start of the account on the Pro account. The Free plan does not hide old completions: one backdated by 30 days was visible despite `activity_log_limit: 7`.
- **`tasks/completed`:**
  - Deprecated: every response carries `Warning: 119 - "Deprecated endpoint, please use GET /api/v*/completed/by_completion_date instead."`
  - One entry per completion, with its own UUID `id` and the `task_id`, including recurring completions and archived projects, but not completions by other people.
  - Offset pagination; `annotate_items` adds the full task as `item_object`, `annotate_notes` adds the comments as `notes`.
- **Activity log:**
  - `object_event_types=["item:completed"]`, a JSON list, selects completions; a plain value is rejected.
  - One event per completion, including recurring tasks and other people's completions, with `extra_data` holding a snapshot (content, due date, `is_recurring`, labels, `note_count`, section), not a full task.
  - Its history is limited: it had no events in a window about eight years back where `by_completion_date` has completions, and the Free plan keeps 7 days. New events appeared within 14 seconds.
- **Comments:** `GET comments?task_id=…` returns the comments of a completed task, also in archived projects. `note_count` in the payloads of `by_completion_date` and the activity log tells which tasks have comments.
- **Sync:** a completed task appears in the incremental sync with `checked: true`, with the same payload as in `by_completion_date`, and is absent from a full sync. Both copies are kept; there is nothing to reconcile.
- **Backdating:** `item_complete` accepts a `completed_at` in the past, so a completion can land in a month that is already closed.

## Open Questions

For the design:

- **Checkpoint:** a month marker is recommended over a timestamp marker. `closed_through` names the last closed month. Every run fetches whole months after it through the current month, and a month closes once it has been fetched at least a grace period after its end. A timestamp marker with an overlap would rewrite the previous month and is easy to get wrong.
- **Backdated completions** in closed months: accept the loss and document it, or re-check closed months occasionally.
- **Layout** of completion events and comments: inside the month files or beside them, and the key of each (event `id` per source, comment `id`).
- **T0:** how it is recorded in `state.json`, and how the one-time backfill from `tasks/completed` stays resumable (for example month by month with `since` and `until`).
- **Comments added after completion:** fetched once when the task is stored, consistent with frozen months, or refreshed while the month is open.
- **Contract tests:** which probes become permanent contract tests, and removal of the probe spike.
