# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation sessions.

- Start each session fresh with `/opsx:apply add-completed-tasks-backup`, name the groups it covers, and stop at the end of the last one.
- Commit after each completed group, once its tests pass.
- Session 1: groups 1 and 2, medium effort.
- Session 2: groups 3 and 4, high effort. Month windows, closing, checkpoint order and the comment rule decide what is never fetched again.
- Session 3: groups 5 and 6, high effort.
- Session 4: group 7, high effort. Crash and resume tests, and the expected tree.
- Session 5: groups 8 and 9, medium effort. Tasks 8.2, 8.4 and 8.5 need the user with a real token: ask before starting them, never ask for a token in chat, use the Pro account (`TODOIST_CONTRACT_TOKEN`) for read-only steps only, and the Free test account (`TODOIST_CONTRACT_WRITE_TOKEN`) for writes.

## 1. API client: GET requests and pagination

- [x] 1.1 Generalize the retry loop of `Client.post` into `Client.request(method, path, params)` and add `Client.get` (design D1). In `UrllibTransport`, send GET parameters as a query string with no body and no `Content-Type`, and name the real method in error and rate-limit messages. Verify with an offline test against a local `http.server` thread that a GET request carries the query, the bearer header and no body, that POST requests are unchanged, that a 400 on a GET names `GET` in its message, that the request log shows no query string, and that `tests/test_api.py` and `tests/test_contract_probes.py` still pass
- [x] 1.2 Add `cursor_pages` (follows `next_cursor` until it is absent or null; a repeated cursor raises `ApiError`) and `offset_pages` (advances `offset` until a page is shorter than `limit`) to `api.py`. Verify with fake-transport unit tests for one page, three pages, a null cursor, a repeated cursor and an offset walk that ends on an exactly full page followed by an empty one

## 2. State file

- [x] 2.1 Add helpers that read and update the `completed` object of `state.json` (design D3); `FORMAT_VERSION` stays 1 (D4). Verify with tests in `tests/test_state.py` that the object round-trips with unknown fields kept, and that a state without it starts this part as on a first run
- [x] 2.2 Document the `completed` object of `state.json` in `storage_format.md`, with the meaning of each field; the format version stays 1. Verify that `tests/test_storage_format.py` passes with the new state example

## 3. Fixtures and test harness

- [x] 3.1 Add hand-written fixtures under `tests/fixtures/completed/`, in the shapes recorded by the probes but with invented IDs and content: by-completion-date pages with and without `next_cursor`, `tasks/completed` pages with `projects`, `sections` and `item_object`, activity pages with 128-bit event IDs and an event dated before a test T0, comments pages with a `file_attachment`, and error bodies for 404, 410 and a plan restriction. Verify with a fixture-loading test that every file parses, and that a 128-bit ID survives `json.loads` and `storage.encode_json` unchanged
- [x] 3.2 Add a fake transport for GET endpoints that answers by method, path and parameters, records the requests, and returns empty pages for endpoints a test does not configure. Make the existing `FixtureTransport` of the run and CLI tests use it, so that they keep passing once the completed part runs after the sync. Verify that the whole offline suite passes unchanged

## 4. Completed-task months and their comments

- [x] 4.1 Create `completed.py` with `run(target, client, state, now)`, and call it from `run_backup` after the sync rounds and before the plan warnings (design D2). Set `t0_utc` and `first_month` (from `sync/user.json` `joined_at`, else 2007-01 with an INFO line) before the first request of this part, and add the `completed_tasks` row to `PLAN_FEATURES`. Verify with run tests that T0 and `first_month` are written before any request of this part, that a second run keeps them, and that with `completed_tasks: false` the run warns once, makes no request of this part, and exits with status 0
- [x] 4.2 Implement the page merge and ordering of design D6 (list concatenated, maps merged, last value wins for other keys, `next_cursor` dropped, sort by time field then `id`, integer IDs compared as integers) and the merge by `id` for stored files. Verify with unit tests covering three pages, maps from two pages, an entry without the time field, mixed page order giving the same bytes, and a merge in which a new entry replaces a stored one with the same `id`
- [x] 4.3 Implement the month fetch: windows from D5, all pages, `completed/<YYYY-MM>.json` written also when empty, the closing rule of D7, and `tasks_closed_through` advanced after each closed month. Verify with run tests on a fake clock that cover the spec scenarios: first run from `first_month` through the current month, the month boundary at `T23:59:59.999999Z`, several pages, within and after the grace period, historical months closing as stored, and a second run that requests only open months
- [x] 4.4 Implement the comments of completed tasks (design D8): the `note_count` comparison with the stored month file, `completed/comments/<task id>.json` merged by `id` and written before the month file, no requests with `comments: false`, 404 skipped at DEBUG, `PlanRestricted` warned once, and the `item_id` check that raises `StorageError` on a case collision. Verify with run tests for every scenario of the spec requirements "Comments of completed tasks" and "Comments refreshed while the month is open", plus the crash case in which the comments are written but the month file is not
- [x] 4.5 Document `completed/<YYYY-MM>.json` and `completed/comments/` in `storage_format.md`: the response-shaped contents, ordering, open and closed months, the 7-day grace period, a task that appears in two months, backdated completions not captured, and deleted comments kept. Verify that the path-pattern tests of `tests/test_storage_format.py`, extended to `completed/`, find every produced path documented and every documented pattern produced

## 5. Completion events from the activity log

- [x] 5.1 Implement the activity fetch (design D9): `object_event_types=["item:completed"]`, `date_from` one day before the checkpoint (or T0), the cursor walk, events before T0 dropped, grouping by the month of `event_date` (an unparseable one goes to the fetch month with a warning), the merge by `id` into `completed/activity/<YYYY-MM>.json`, and `activity_fetched_utc` set after the files are written. Verify with run tests for the spec scenarios "Recurring task", "Overlapping fetches", "Large event ID" and "Event before T0", and the checkpoint scenario "Killed before the checkpoint"
- [x] 5.2 Implement the gap warning (`activity_log_limit` in days, `-1` and missing mean no limit) and the `PlanRestricted` handling, in which the checkpoint stays. Verify with run tests for the spec scenarios "Free plan, long pause" and "Within the limit", and for "Activity log restricted"
- [x] 5.3 Document `completed/activity/` in `storage_format.md`: the source, the T0 split, months by `event_date`, merging by `id`, and the warning that event IDs exceed 64 bits and are corrupted by JavaScript and SQLite `INTEGER`. Verify with the path-pattern tests and a test that the description contains the warning

## 6. One-time backfill

- [x] 6.1 Implement the backfill (design D10): months from `backfill_through + 1` (or `first_month`) through the month of T0, `until = t0_utc` in the last month, entries at or after T0 dropped, `annotate_items=true`, `limit=200` with offset pagination, `completed/backfill/<YYYY-MM>.json` written also when empty, `backfill_through` advanced after each month, and `backfill_status` set to `complete` after the month of T0. Verify with run tests for the spec scenarios "First run", "Interrupted backfill" and "Backfill complete", and that the request carries `annotate_items=true`
- [x] 6.2 Handle the deprecated endpoint: HTTP 404 or 410 sets `backfill_status: unavailable` with one warning and no further requests in later runs; `PlanRestricted` warns and is retried next run. Verify with run tests for "Endpoint removed" and "Later run", and that a plan restriction leaves `backfill_status` at `in_progress`
- [x] 6.3 Document `completed/backfill/` in `storage_format.md`: the source, `item_object`, the `projects` and `sections` maps, completions by other people missing, and what `unavailable` means. Verify with the path-pattern tests

## 7. Whole runs

- [x] 7.1 Fix the order activity log → months → backfill (design D2) and add the INFO lines of D13. Verify with a run test in which the backfill fails with `RateLimitExhausted`: the exit status is 1, and the activity checkpoint and the closed months of that run are kept
- [x] 7.2 Add crash and resume tests for this part: with the fault-injection hook at several counts during a first run with completed tasks, a rerun produces the tree of an uninterrupted run byte for byte, and requests no closed month and no completed backfill month again
- [x] 7.3 Add an idempotence test: two runs with unchanged fake responses leave every file under `completed/` with the same bytes and modification time
- [x] 7.4 Extend the end-to-end CLI test of `tests/test_cross_platform.py` and `tests/fixtures/expected_tree/` with `completed/` files, with the clock fixed by the test. Verify that the produced tree matches byte for byte on Linux, and that the test runs in the CI matrix
- [x] 7.5 Update the project `README.md`: status and roadmap step 2, the backup scope, and advice for the first run on a large account (several runs or a larger `--max-rate-limit-wait`). Verify that every link in the changed sections resolves

## 8. Fixture recording and contract tests

- [x] 8.1 Extend `scripts/record_fixtures.py` to record one month of `by_completion_date`, one page each of `tasks/completed` (annotated), the activity log and the comments of one completed task, read-only, scrubbed by the existing rules, under `tests/fixtures/recorded/<account>/`, and to add these endpoints to `shapes.json`. Verify with an offline scrubber test that 128-bit event IDs become pseudonyms of the same length that are not 0 and stay consistent within a recording
- [x] 8.2 Ask the user to record both accounts with the new command, then review the recordings for personal data with them and regenerate `shapes.json`. Verify that the shapes test passes against the committed recordings
- [x] 8.3 Add the read-only contract test `tests/contract/test_completed_read.py`, parametrized over both accounts: compare the four endpoints with `shapes.json` (added fields fail), and fail if `tasks/completed` is gone or the activity filter is rejected. Verify offline that it is skipped without its variables
- [x] 8.4 Ask the user to run 8.3 against both accounts. Verify that it passes, or record any difference as a finding and resolve it before continuing
- [x] 8.5 Extend the write contract test, on the write account only and behind the existing guard: complete a recurring task twice and a task with a comment, run the tool into a temporary backup, and assert two completion events in `completed/activity/` and the comment file. Verify offline that it is skipped without its variables and that the guard still refuses equal tokens and equal users; then ask the user to run it against the write account
- [x] 8.6 Document the new contract test and recording command in the README's Development section. Verify that the documented commands run as written (the skipped path at minimum)

## 9. Integration

- [x] 9.1 Run the full offline suite and `openspec validate add-completed-tasks-backup --strict`. Verify that both pass, and that the CI matrix (Linux, macOS, Windows × Python 3.11 and 3.13) is green once the branch is pushed
