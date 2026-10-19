# Design

## Context

This change builds on two archived changes, cited as "sync D<n>" (`2026-10-18-add-active-data-sync`) and "completed D<n>" (`2026-10-18-add-completed-tasks-backup`). The code it builds on:

- `api.py`: `Client.get` with retry and rate-limit handling, and `cursor_pages` (completed D1).
- `completed.py`: `fetch_pages`, `merge_pages` (response-shaped files, sorted by time and `id`, completed D6), `read_stored`, the month helpers (`month_window`, `next_month`, `month_of`), `_first_month`, and `_CommentStore`, which fetches comments for tasks whose `note_count` changed and merges them by `id` (completed D8). `_CommentStore` writes to a fixed directory, `completed/comments/`.
- `sync.py`: `run_backup` runs the sync rounds, then `completed.run`, then the plan warnings (`PLAN_FEATURES`). `apply_response` counts missing objects of a server-initiated full sync with `_stored_live_keys`, which treats every stored live file as expected.
- `storage.py`: `write_bytes_atomic` (skips unchanged bytes), `encode_key`, `encode_json`, the fault-injection hook, `state.json` with `FORMAT_VERSION = 1`.
- `tests/contract/probes.py` and `test_probe_*.py`: the probe spike that produced the API findings. Completed design left its removal to this change.

The API facts are in `proposal.md` under "API findings". The ones that shape this design: archived projects and their content are not in the sync; `/full` returns open tasks and non-archived sections only, without comments; completed tasks of an archived project come only from `by_completion_date` filtered by `project_id`; windows of up to 93 days are accepted, 100 are rejected; the sync, `by_completion_date` and `/full` return tasks with the same 28 fields.

## Goals / Non-Goals

**Goals:**

- One new module that runs after the completed-tasks part, inside the same lock and run, and reuses the storage primitives and the completed-tasks helpers.
- A snapshot per project that is self-contained: everything known about an archived project is in its directory.
- No checkpoint beyond the files themselves: whether a snapshot is stored, and whether a part of it is missing, can be read from the directory.
- Offline tests for every path, with the fake transport and fake clock of the completed-tasks tests.

**Non-Goals:**

- Removing anything from `sync/` once a snapshot holds it. `sync/` keeps accumulating; a condensed view, such as an SQLite import, deduplicates by `id` (D11).
- Refreshing a snapshot whose listed payload is unchanged, for example to pick up a comment added to a task of an archived project.
- Archived sections that the sync never reported. The API offers no way to list them.
- Downloading attachment files.
- Changing the summary line of the CLI.

## Decisions

### D1: Module and run sequence

A new module `archived.py`. `run_backup` calls `archived.run(target, client, state, now)` after `completed.run` and before the plan warnings:

```
… sync rounds (unchanged) …
completed.run(…)          activity log first: its history is limited
archived.run(…)
    list archived projects (all pages)                      (D4)
    queue = listed projects, in list order
    for each project in queue, once per run:
        no project.json, or listed payload differs  -> fetch snapshot   (D5)
        else                                        -> fill missing parts (D6)
        archived entries of subprojects             -> append to queue  (D8)
plan warnings ; last_successful_run_utc ; write state
```

The completed-tasks part runs first because the activity log loses events after its limit. Archived snapshots lose nothing by waiting. A run that ends early, for example on the rate limit, has then secured the activity log and the open months first.

### D2: Layout: one directory per project

```
archived/<project id>/
  full.json               GET projects/{id}/full, unchanged
  completed.json          by_completion_date?project_id=…, all windows merged, {"items": […]}
  comments/<task id>.json GET comments?task_id=…, merged by id, {"results": […]}
  project_comments.json   GET comments?project_id=…, merged by id, {"results": […]}
  project.json            the listed project payload; written last (D3)
```

Directory and file names use `storage.encode_key`, as object files do. The comments of every task of the project, open or completed, go to the snapshot's own `comments/`. `_CommentStore` takes its directory as a parameter so that both parts share it.

*Alternative: merge into `sync/`.* Rejected. `sync/` holds exactly what the sync endpoint returned, and its tombstone and restore rules (sync D8) depend on that. Completed tasks and REST comment pages are response-shaped and have no place there.

*Alternative: share `completed/comments/<task id>.json`.* Rejected. Those files are refreshed while their month is open, and a snapshot is frozen. Mixing the two in one file makes neither rule hold. A completed task of a project archived after T0 can therefore have its comments in both places. D11 documents that.

### D3: `project.json` is the checkpoint

A snapshot is stored once `archived/<id>/project.json` exists. The tool writes it after every other file of the snapshot. The write order of a fetch is: task comment files, `project_comments.json`, `completed.json`, `full.json`, `project.json`. Each write goes through `write_bytes_atomic`, so it skips unchanged bytes and is durable before the next one starts. `state.json` gets no new field.

Comments are written before `full.json` and `completed.json` because D5 decides which comments to fetch by comparing with the stored task lists. If a crash happens after the comments but before the task lists, the next run compares with the old lists and fetches those comments again. The merge by `id` makes that harmless, as in completed D8.

*Alternative: a list of finished project IDs in `state.json`.* Rejected. It duplicates what the directory already says, it grows with every archived project, and the two can disagree after a crash.

### D4: Detecting new and re-archived projects

For each listed project, the tool encodes the listed payload with `encode_json` and compares the bytes with the stored `project.json`:

| Stored `project.json` | Action |
|---|---|
| missing | fetch the snapshot (new, or unfinished by an earlier run) |
| equal bytes | fill missing parts only (D6) |
| different bytes | fetch the snapshot again (D5) |

Byte equality is the same rule `write_bytes_atomic` uses. It needs no knowledge of which fields change when. The INFO log names the number of projects fetched again, so that frequent refetches would be noticed (see Risks).

*Alternative: compare `updated_at` only.* Rejected for now. Probe W8 shows that a project's `updated_at` moves forward on archiving and on unarchiving, but not which other changes move it. Missing a re-archive would lose data, while an extra refetch costs only requests.

*Alternative: take the signal from the sync, which reports `is_archived` changes.* Rejected as the only rule. The sync sees an unarchive only if a run happens while the project is active, and it never sees projects archived before the first backup.

### D5: Fetching a snapshot

1. `GET projects/{id}/full`. On 404 the project is skipped: DEBUG log, no file written (spec: Project gone during the run).
2. If the plan allows completed tasks: the windows (D7) of `by_completion_date` with `project_id`, all pages, merged with `merge_pages(…, "items", "completed_at")`.
3. If the plan allows comments: for each task in the fetched `tasks` and completed `items` with `note_count` > 0 whose `note_count` differs from the stored copy in `full.json` or `completed.json`, or whose comment file is missing, `_CommentStore` fetches and merges. Then `GET comments?project_id=…`, merged by `id` into `project_comments.json`.
4. Write in the order of D3.

On a refetch, `full.json` and `completed.json` are replaced, so a task that was uncompleted or deleted in the meantime drops out of them. While the project was active, the sync stored its state under `sync/`. Comment files are only ever merged, never shrunk (spec: Changed or re-archived project).

A `PlanRestricted` on step 2 or 3 makes that part count as not allowed for the rest of the run. The snapshot is still finished, without that part, and D6 adds the part later. A `PlanRestricted` on the list or on `/full` warns once and ends this part for the run.

### D6: Filling in missing parts

The presence of a file says whether a part was stored:

- `completed.json` missing and completed tasks allowed: fetch the windows, write `completed.json`. If comments are allowed, fetch the comments of its tasks whose comment file is missing first.
- `project_comments.json` missing and comments allowed: fetch the comments of every task in `full.json` and `completed.json` with `note_count` > 0 whose comment file is missing, then the project comments, and write `project_comments.json` last.

`project_comments.json` is therefore the marker for the comments part. A task whose comment request answers 404 is skipped, and it is not retried on every run, because the marker is written anyway. A stored snapshot costs two file-existence checks per run when nothing is missing.

### D7: Windows over the project's lifetime

Windows are three calendar months long, starting at the month of the project's `created_at`: `since` = the first day of month *m* at `00:00:00.000000Z`, `until` = the last day of month *m*+2 at `23:59:59.999999Z`. They continue until a window contains the current month. That is at most 92 days, within the 93 the probes accepted. The month helpers of `completed.py` compute them.

If `created_at` is missing or unparseable, the windows start at `completed.first_month` from `state.json`. If that is missing too, they start at the month of `user.joined_at`, or at 2007-01 (completed D3).

*Alternative: start at the account's first month for every project.* Rejected. It would make 56 projects × about 40 windows on a ten-year-old account, against a few hundred requests in total.

### D8: Sub-projects

After `/full` of a project is fetched (or read from `full.json` for a stored snapshot), each entry of its `subprojects` with `is_archived: true` whose `id` has not been handled in this run is appended to the queue. Its entry from `subprojects` then plays the role of the listed payload. A set of handled IDs stops cycles and duplicates. Entries without `is_archived: true` are active and therefore in the sync.

This rule gives the right result whether the list of archived projects contains sub-projects or not, which the probes could not tell. If a sub-project is first stored from its parent's `subprojects` and later appears in the list with different bytes, D4 fetches it once more.

### D9: Missing-object count of a full sync

`_stored_live_keys` already parses every stored live file. It now also leaves out the objects that the spec excludes, decided from the stored payloads before the response is applied:

1. Collect `A`, the IDs of stored projects with `is_archived: true`.
2. Leave out projects in `A`; items with `checked: true` or a `project_id` in `A`; sections with `is_archived: true` or a `project_id` in `A`; any other collection object with a `project_id` in `A` (project notes, collaborator states, …); notes whose `item_id` names a left-out item.

The warning counts the rest, as before. The number of left-out objects that the response did not contain is logged at INFO. `ApplyStats.missing` keeps its meaning, the count behind the warning.

*Alternative: decide from the snapshots under `archived/`.* Rejected. Completed tasks and archived sections have no snapshot, and the stored sync payloads already carry the flags.

### D10: Plan limits and errors

The snapshot parts follow the existing flags of `user_plan_limits.current`: `completed_tasks` for `completed.json`, `comments` for the comment files. The `PLAN_FEATURES` texts gain "including those of archived projects", so the existing plan warnings cover this part. No flag is known for the list of archived projects. A `PlanRestricted` answer produces the warning of D5.

Errors other than plan restrictions and the 404s named in D5 and D6 behave as in the sync: `RunError`, exit status 1. Snapshots already finished stay finished.

### D11: Format description

`storage_format.md` gains a section "Archived projects" with the layout of D2, the checkpoint rule of D3, the refetch rule of D4 and the fill-in rule of D6. A section "Copies of the same task" lists where a task can be stored and how a reader picks the current copy:

| Copy | When it exists |
|---|---|
| `sync/items/<id>.json` | the task was active, or completed while a backup ran |
| `completed/<YYYY-MM>.json` | completed; month file fetched before the project was archived |
| `archived/<project id>/full.json` | open task of an archived project |
| `archived/<project id>/completed.json` | completed task of an archived project |

All copies share the task's `id`. The rule is: the copy with the newest `updated_at` is current, and on equal values `archived/` beats `completed/`, which beats `sync/`. Probe W8 (proposal, API findings) supports it: completing a task moves `updated_at` forward, so a completed copy beats the open copy it replaced. Archiving and unarchiving the project leave `updated_at` unchanged, so the copies from before and after archiving tie, and the source order picks the snapshot, which was fetched last. A new comment does not change `updated_at` either; the format description therefore says that `note_count` of the chosen copy can be older than the comments stored for the task. The README states the rule.

Comments are stored in `completed/comments/` and `archived/<id>/comments/`. A comment's copies share its `id`; the union of both files is the task's comments, and on the same `id` the copy in `archived/` beats the one in `completed/`, by the same source order.

The section on archived sections states that sections archived before the first backup are not in the backup, apart from the `section_id` of their tasks.

`FORMAT_VERSION` stays 1. The tool has not been released (no tag), and completed D4 extended version 1 in place the same way. A development backup gains `archived/` on its next run.

### D12: Cross-platform

No new primitives. `archived/`, `comments/`, `full.json`, `completed.json`, `project.json` and `project_comments.json` are ASCII. Project and task IDs go through `encode_key`, with its length limit and reserved-name rule. A task comment file is about 70 characters below the backup root, well within Windows' 260-character limit for reasonable roots. Every write goes through `write_bytes_atomic`, the temp-file cleanup already walks the whole target, and the lock is unchanged. On a case-insensitive file system, two project IDs that differ only in case would share a directory: before writing into an existing snapshot, the tool checks that the `id` in its `project.json` (or `full.json`) equals the project's `id`, and raises `StorageError` otherwise. The comment files keep their `item_id` check (completed D8).

### D13: Messages

One INFO line per run: archived projects listed, snapshots fetched (new and again), parts filled in, comment files written. DEBUG lines per request. The summary line keeps its meaning (sync objects).

### D14: Tests

- **Fixtures** under `tests/fixtures/archived/`, hand-written in the shapes the probes recorded: archived-list pages with and without `next_cursor`, `/full` responses with tasks, sections and an archived sub-project, `by_completion_date` pages filtered by project, comment pages for a task and a project, and error bodies for 404 and a plan restriction. IDs are invented.
- **Unit tests:** windows from `created_at` and the fallbacks (D7), the detection table (D4), the sub-project queue with a cycle and a duplicate (D8), the missing-object rule with each excluded kind and a mixed case (D9), and the case-collision check (D12).
- **Run tests:** first run, repeat run with only the list requested and no file rewritten, re-archived project with a merged comment, plan without completed tasks and then upgraded (D6), plan without comments, list restricted, 404 on `/full`, a sub-project not in the list.
- **Crash tests:** with the fault-injection hook at several counts during a first run with three archived projects, rerunning gives the tree of an uninterrupted run, and finished snapshots get no second `/full` request.
- **Contract tests**, which the user runs, each with a real token:
  - Read-only, both accounts: the archived list and one `/full`, compared with shapes recorded by `scripts/record_fixtures.py`.
  - Write, Free account only, behind the existing guard: create a project with a sub-project, a section, an open task, a completed task and comments, archive the parent, run the tool into a temporary backup, force a full sync, and assert the snapshots and no missing-object warning.
- **Probe:** a last write probe on the Free account records whether `updated_at` of a task changes on completion, on a new comment and when its project is archived and unarchived, and whether the list of archived projects contains an archived sub-project. It records booleans only, no values. After the result is in `proposal.md`, the probe spike is removed.

## Risks / Trade-offs

- [The listed payload of an archived project changes without a re-archive, for example `child_order` when a sibling is archived] → Each such project is fetched again, which costs about as many requests as its first fetch. The INFO line counts refetches. If they turn out to be frequent, D4 can be narrowed to selected fields in a later change, once the probe shows which fields move.
- [A project is unarchived, changed and archived again between two runs, and its listed payload ends up byte-identical] → The changes are missed. This is accepted: it needs an unchanged `updated_at` as well, and the sync usually catches the interim state.
- [Completions before the project's `created_at`, for example of tasks moved in from another project] → Not fetched. The probes found none, and fetching from the account's first month would multiply the requests.
- [The first run on an account with many archived projects exceeds the rate-limit budget] → It exits with status 1, and the next run continues with the first project without `project.json`. The README advises a larger `--max-rate-limit-wait` for the first run.
- [The missing-object rule hides a real deletion of a completed task or of an object of an archived project] → Accepted. Deletions normally arrive as `is_deleted` in an incremental sync. The rule only removes warnings that are wrong today.
- [The same task or comment is stored up to four times] → Documented with a rule for picking the current copy (D11). A condensed view deduplicates by `id`.
- [A task is changed without a new `updated_at`, for example by a new comment, and its copies tie] → The source order picks the copy fetched last. Probe W8 found that comments and archiving do not move `updated_at`; the format description says so (D11).

## Migration Plan

None. Format version 1 is extended in place (D11). To roll back, delete `archived/`. The missing-object rule changes only a log message.

## Open Questions

None. Answered by probe W8 (proposal, API findings):

- `updated_at` of a task moves forward on completion, and not on a new comment, on archiving or on unarchiving. D11 settles the rule for picking the current copy on that basis.
- The list of archived projects holds archived sub-projects at every depth, and `subprojects` of an archived parent's `/full` holds none of them. D8 handles this: the list alone yields every snapshot, and the queue only guards against a sub-project that the list leaves out.
