# Design

## Context

The active-data sync is implemented (archived change `2026-10-18-add-active-data-sync`; its design decisions are cited here as "sync D<n>"). The code this change builds on:

- `api.py`: `Transport.send(method, path, form)` and a `Client` whose retry, rate-limit and error-classification loop exists only as `Client.post`. `UrllibTransport` always form-encodes a body, and error messages always say `POST`.
- `storage.py`: JSON encoding, file-name encoding, `write_bytes_atomic` (skips unchanged bytes), the fault-injection hook, and `state.json` with `FORMAT_VERSION = 1`; a newer version is refused, unknown fields are kept.
- `sync.py`: `run_backup` runs the sync rounds under the lock, then `_warn_about_plan_limits` (table `PLAN_FEATURES`, sync D12), then writes `last_successful_run_utc`.
- `tests/contract/probes.py`: the probe spike, which sends GET requests by wrapping the transport (`GetTransport`).

The API facts this design relies on are recorded in `proposal.md` under "API findings". The ones that shape the decisions:

- `by_completion_date`: windows of at most 3 months; `since` and `until` are inclusive to the microsecond; cursor pagination with `limit` ≤ 200; the response has only `items` and, except on the last page, `next_cursor`.
- `tasks/completed`: offset pagination with `limit` ≤ 200; the response has `items`, `projects` and `sections`; `annotate_items` adds `item_object`.
- Activity log: `object_event_types=["item:completed"]` as a JSON list; `date_from` takes a date; cursor pagination over `results`; event `id`s are integers of about 128 bits. Python's `json` parses and writes them exactly.
- Comments: `GET comments?task_id=…` with cursor pagination over `results`.

## Goals / Non-Goals

**Goals:**

- One new module that runs after the sync, inside the same lock and run, and reuses the storage primitives unchanged.
- Every unit of work (a month of tasks, a month of backfill, one fetch of the activity log, the comments of one task) is written atomically before the checkpoint that covers it advances.
- Offline tests for every path, with a fake clock and a fake transport that answers GET requests.

**Non-Goals:**

- Completed tasks of archived projects. `add-archived-projects-backup` covers them.
- Re-checking closed months, uncompletion events and any other activity-log event type.
- Matching completion events to completed tasks, or events of the two sources to each other.
- Changing the summary line of the CLI. It keeps counting sync objects (D13).
- Removing the probe spike. `add-archived-projects-backup` may still need it, and removes it.

## Decisions

### D1: GET requests in the API client

`Transport.send(method, path, params)` keeps its signature. `UrllibTransport` sends `params` as the form body for POST, as before. For GET it appends them to the URL as a query string and sends no body or `Content-Type`. `Client.post` becomes a thin wrapper around a general `Client.request(method, path, params)` that holds today's retry loop, and `Client.get` is added beside it. Error messages and the rate-limit message name the real method. Requests are still logged by method and path only, without the query.

GET is idempotent, so retries after network errors and 5xx stay safe. List-valued parameters are passed as JSON text (`'["item:completed"]'`), as the sync already does for `resource_types`.

Pagination lives in `api.py` as two generators over parsed pages: `cursor_pages(client, path, params, limit)` follows `next_cursor` until it is absent or null, and `offset_pages(...)` advances `offset` until a page is shorter than `limit`. A cursor that repeats raises `ApiError` instead of looping forever.

The probes' `GetTransport` passes the query inside `path` with an empty form, so it keeps working unchanged.

*Alternative:* a second client for the REST endpoints. Rejected because it would duplicate the retry and rate-limit logic, and the per-run rate-limit budget must be shared.

### D2: Module layout and run sequence

A new module `completed.py` holds this part. `run_backup` calls it after the sync rounds and before the plan warnings:

```
… sync rounds (unchanged) …
completed.run(target, client, state, now)
    plan check: completed_tasks false → return (sync D12 table warns)
    set T0 and first_month if missing → write state
    1. activity log        (D9)
    2. completed-task months and their comments (D6, D8)
    3. backfill            (D10)
plan warnings ; last_successful_run_utc ; write state
```

The activity log comes first because its history is limited (7 days on the Free plan). A run that later fails, for example on the rate limit during a long first backfill, has still secured the events. The month files come before the backfill because they are the primary data. The backfill is a one-time job that can take several runs.

`PLAN_FEATURES` gains the row `completed_tasks` → "completed tasks, completion events and their comments". `completed.run` reads the same stored `user_plan_limits`, through a helper shared with `sync.py`.

### D3: Checkpoint in `state.json`

All values of this part live in one nested object, so that the format description can explain them together:

```json
"completed": {
  "t0_utc": "2026-10-18T10:00:05.123456Z",
  "first_month": "2016-03",
  "tasks_closed_through": "2026-09",
  "backfill_through": "2026-10",
  "backfill_status": "complete",
  "activity_fetched_utc": "2026-10-18T10:00:05.123456Z"
}
```

| Field | Written | Meaning |
|---|---|---|
| `t0_utc` | once, before the first request of this part | the split between backfill and activity log |
| `first_month` | with `t0_utc` | month of `user.joined_at` |
| `tasks_closed_through` | after each closed month | last closed month of completed tasks, or null |
| `backfill_through` | after each backfill month | last backfill month stored, or null |
| `backfill_status` | when it changes | `in_progress`, `complete` or `unavailable` |
| `activity_fetched_utc` | after each activity fetch is written | start time of the last complete fetch |

Each update goes through `storage.write_state`, so it is written after the files it covers and flushed with `F_FULLFSYNC` on macOS. A first run makes a few hundred state writes. That costs seconds, and it is what makes resuming at month granularity possible.

`first_month` is stored rather than read from `sync/user.json` on every run, so that the covered range never changes. If `joined_at` is missing or unparseable, the first month is January 2007, the year Todoist launched, and the tool logs an INFO line. That costs about 120 empty months once. R6 found no completions before `joined_at`.

*Alternative:* a timestamp checkpoint with an overlap. Rejected, as the proposal argues: it rewrites the previous month in part, and its edges are easy to get wrong.

### D4: Format version stays 1

The tool has not been released, so format version 1 is extended in place, and `FORMAT_VERSION` stays 1. A development backup made before this change gains `completed/` and the `completed` object on its next run: without the object, this part starts as on a first run. Nothing under `sync/` changes. A version bump, with an upgrade path for older backups, becomes necessary only once the tool is released.

### D5: Month windows and time handling

A month window is `since=YYYY-MM-01T00:00:00.000000Z` and `until=<last day>T23:59:59.999999Z`, computed with `datetime` in UTC. For the current month, `until` lies in the future; R1 used exactly that. Timestamps from the API are parsed with `datetime.fromisoformat`, which accepts `Z` from Python 3.11 on.

The clock is the injected `now` of `run_backup`, so tests control the month, the grace period and T0.

### D6: Contents of the files under `completed/`

Every file is a response-shaped object (spec: Contents of completed-tasks files). One merge function builds it from pages:

- the list key (`items` or `results`) is concatenated;
- object-valued keys (`projects`, `sections`) are merged, and a later page wins per key;
- any other key takes the value of the last page that has it;
- `next_cursor` is dropped.

The list is then sorted by the entry's time field (`completed_at`, `event_date` or `posted_at`), then by `id`. Integer IDs are compared as integers, other IDs as strings. The time strings all have the form `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so string order is chronological. An entry without the field sorts first. Sorting makes the bytes independent of the API's page order, so an unchanged open month is never rewritten, and it gives the merged files one order.

How each kind of file is updated:

| File | Update |
|---|---|
| `completed/<YYYY-MM>.json` | replaced by the month's fetch, also when empty |
| `completed/backfill/<YYYY-MM>.json` | replaced by the month's fetch, also when empty |
| `completed/activity/<YYYY-MM>.json` | stored entries and new ones merged by `id`, new ones win; only months with events |
| `completed/comments/<task id>.json` | stored entries and new ones merged by `id`, new ones win |

The month files mirror the unfiltered query for their window. They hold nothing from other sources, so they can be replaced while open.

*Alternative:* a bare JSON array per file. Rejected because it would drop `projects` and `sections` of `tasks/completed`, which may be the only names left of projects deleted since, and any top-level key the API adds later.

### D7: Closing months

After a month is fetched and written, it closes when the run's `now` is at least 7 days after the month's end. `tasks_closed_through` advances only across a contiguous run of closed months. The first run's historical months therefore close one by one as they are stored. During the first week of a month, the previous month stays open, along with the current one.

### D8: Comments of completed tasks

For each fetched month, before the month file is written:

1. Read the stored month file, if any, and map task `id` → `note_count`.
2. For each fetched task with `note_count` > 0 whose stored `note_count` is missing or different, fetch `GET comments?task_id=<id>` (all pages, `limit=100`), merge the result into `completed/comments/<id>.json` (D6), and write it.
3. Write the month file, then advance the checkpoint if the month closes.

Because the month file is written after the comments, a crash in between makes the next run compare against the old month file and fetch those comments again. The merge makes that harmless. A task whose `note_count` dropped to 0 is not fetched: there is nothing to add, and stored comments stay.

The comments file of a task is keyed by the task ID, with `storage.encode_key`. Before merging into an existing file, the tool checks that its entries' `item_id` equals the task ID. A mismatch, which means two task IDs that differ only in letter case on a case-insensitive file system, raises `StorageError`, as sync D7 does.

When the plan has `comments: false`, step 2 is skipped; the sync D12 warning already covers it. A comment request answered with 404, for example for a task deleted after the month was fetched, is logged at DEBUG and skipped. `PlanRestricted` produces one warning and skips the comments for the rest of the run.

*Alternative:* `annotate_notes` on the backfill, which would save one request per task with comments before T0. Rejected: it depends on the deprecated endpoint, misses completions by other people, and repeats a recurring task's comments for every completion.

### D9: Activity log

Each run sends `GET activities` with `object_event_types=["item:completed"]`, `limit=100` and `date_from` = the UTC date of (`activity_fetched_utc`, or `t0_utc` on the first fetch) minus one day, and follows the cursor.

- The overlap of at least a day covers the delay before new events appear (14 s observed) and any time zone in which `date_from` might be interpreted. Merging by `id` (D6) removes the duplicates.
- Events with `event_date` before T0 are dropped (spec: Start of completion events). An event without a parseable `event_date` is stored in the month of the fetch, with a warning.
- Events are grouped by the month of their `event_date` and merged into their files. Then `activity_fetched_utc` is set to the time the fetch started.
- Before the fetch, if `activity_log_limit` in `user_plan_limits.current` is positive and `now - activity_fetched_utc` exceeds it in days, one warning names the period from `activity_fetched_utc` to `now - limit` as possibly missing.
- `PlanRestricted` → warning, skipped for this run. The checkpoint does not move, so the gap warning appears once the log is available again.

### D10: One-time backfill

While `backfill_status` is `in_progress` (or unset), for each month from `backfill_through + 1` (or `first_month`) through the month of T0, the tool sends `GET tasks/completed` with `since` and `until` of that month (`until = t0_utc` in the last month), `annotate_items=true`, `limit=200`, and offset pagination. Entries with `completed_at` at or after T0 are dropped, so that the boundary is exact whatever the endpoint's own `until` precision is. The file is written, then `backfill_through` advances. After the month of T0, `backfill_status` becomes `complete`.

- `annotate_items` adds the full task object (`item_object`) at no extra request. For a recurring task that was deleted since, it may be the only full payload left.
- HTTP 404 or 410 → `backfill_status: unavailable`, one warning, no further requests (spec: Deprecated endpoint unavailable). `PlanRestricted` → warning, retried next run.
- The deprecation `Warning` header is logged at DEBUG only. The contract test watches the endpoint instead (D14).

### D11: Errors in this part

Errors other than plan restrictions, 404/410 on the backfill and 404 on comments behave as in the sync: `RunError`, exit status 1, and the checkpoints already written stay. Data degrades only where the specs name a warning.

### D12: Cross-platform

No new primitives. Directory and month names are ASCII (`completed/activity/2026-10.json`). Comment files use the existing file-name encoding and length limit. Every write goes through `write_bytes_atomic`, temp-file cleanup already walks the whole target, and the lock is unchanged. Byte-identical output on every platform follows from `encode_json`. CI covers Windows, macOS and Linux as before.

### D13: Messages

Each part logs INFO lines: months fetched and closed, comment files written, new events stored, backfill months stored, plus DEBUG per request. The summary line keeps its meaning (sync objects) and its format, so `backup-cli` does not change.

### D14: Tests

- **Offline fixtures** under `tests/fixtures/completed/`, hand-written in the shapes the probes recorded: by-completion-date pages (with and without `next_cursor`), `tasks/completed` pages with `projects`, `sections` and `item_object`, activity pages with 128-bit IDs, comments pages with a `file_attachment`, and error bodies for 404, 410 and a plan restriction. IDs are invented, never taken from findings.
- **Fake transport** that answers by method, path and parameters, and records the requests, so that tests can assert which months were requested. A fake clock drives `now`.
- **Unit tests:** month windows, merge and sort (D6), the comment fetch rule (D8), the activity window and gap check (D9), the T0 boundary, and the closing rule (D7).
- **Run tests:** first run, repeat run with no requests for closed months and byte-identical files, the open-month transitions of the spec scenarios, plan flag false, 410 on the backfill, activity restricted, and a case collision of comment files.
- **Crash tests:** with the fault-injection hook at several counts during a first run with completed tasks, rerunning gives the tree of an uninterrupted run, and closed months are not requested again.
- **Contract tests**, which the user runs; each needs a real token:
  - Read-only, both accounts: one month of `by_completion_date`, one page each of `tasks/completed`, the activity log and comments, compared with shapes recorded by `scripts/record_fixtures.py` (new endpoints in `shapes.json`). Added fields fail. The test also fails if `tasks/completed` is gone or the activity filter is rejected, so that the change is noticed before users lose data.
  - Write, write account only (behind the existing guard): complete a recurring task twice and a task with a comment, run the tool into a temporary backup, and assert two activity events and the comment file.
- **Recording:** `scripts/record_fixtures.py` records these endpoints read-only and scrubs them as before. Numeric IDs, including the 128-bit event IDs, become pseudonyms of the same length, never 0, so that merging by `id` still works in fixtures.

## Risks / Trade-offs

- [The first run on a large account exceeds the per-run rate-limit budget] → It exits with status 1 and the next run continues at the first month not stored. The README advises a larger `--max-rate-limit-wait` for the first run.
- [Runs pause longer than the plan's activity window] → Warning (spec). Non-recurring completions are still in the month files.
- [`tasks/completed` disappears before a user's first run] → Warning. Recurring completions before T0 are lost; nothing else is affected.
- [Backdated completions and later changes in closed months are lost] → Accepted and documented. The 7-day grace period catches the common cases.
- [The local clock is wrong] → Closing and T0 use the local clock. The 7-day grace period dwarfs normal skew, and the activity overlap is a full day.
- [A task completed again appears in two month files] → Documented: the entry with the newest `completed_at` is the task's current state.
- [Downstream tools corrupt the 128-bit event IDs] → The format description warns about JavaScript and SQLite `INTEGER`.
- [Large current-month files are rewritten on every run] → Only when their bytes change. About 1 KB per task, so a busy month is a few MB.
- [`add-archived-projects-backup` wants to put completed tasks into the same month files] → The month files mirror the unfiltered query and are replaced while open, so that change needs its own files.

## Migration Plan

None. Format version 1 is extended in place (D4). To roll back, delete `completed/` and the `completed` object from `state.json`.

## Open Questions

- Whether `item_object` in `tasks/completed` holds the task as it was at completion or as it is now. This changes only the format description's wording.
- The real error tags of plan restrictions on these endpoints. They extend `PLAN_RESTRICTED_TAGS`, as in sync D4.
- The largest `limit` the activity log accepts. 100 works, and a larger value only saves requests.
