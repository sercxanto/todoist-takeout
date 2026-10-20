# completed-tasks-backup Specification

## Purpose

Defines how todoist-takeout backs up the account's history of completions: completed tasks month by month, one event per completion including recurring tasks, and the comments of completed tasks. Months that are closed stay frozen, every run is resumable, and plan limitations degrade to warnings.

## Requirements

### Requirement: Completed tasks by month
The tool SHALL back up the completed tasks that `GET /api/v1/tasks/completed/by_completion_date` returns, one UTC month at a time. It SHALL cover every month from the month of the account's creation (`user.joined_at`) through the current month. For each month it SHALL request a single window that spans the whole month, and it SHALL follow every page.

#### Scenario: First run
- **WHEN** the account was created in March 2016 and the first run happens in October 2026
- **THEN** the tool fetches every month from 2016-03 through 2026-10 and stores the completed tasks of each month in that month's file

#### Scenario: Month boundary
- **WHEN** a task was completed at `2026-09-30T23:59:59.999999Z`
- **THEN** it is stored in the file for 2026-09 and not in the one for 2026-10

#### Scenario: Several pages
- **WHEN** a month holds 450 completed tasks and the API returns at most 200 per page
- **THEN** that month's file holds all 450 tasks

#### Scenario: Archived project
- **WHEN** a task was completed in a project that is archived now
- **THEN** this part does not store it, because the API returns it only for a request filtered by that project

### Requirement: Open months are fetched again
The tool SHALL fetch every month that is not closed again on each run, and it SHALL replace that month's stored tasks with the result. Within an open month the backup therefore follows completions, uncompletions and changes to completed tasks.

#### Scenario: Task completed since the last run
- **WHEN** a task is completed in the current month after the previous run
- **THEN** the next run stores it in the current month's file

#### Scenario: Task uncompleted in an open month
- **WHEN** a task stored in an open month's file is uncompleted in Todoist before the next run
- **THEN** the next run removes it from that month's file

#### Scenario: Task completed again in a later month
- **WHEN** a task stored in a closed month is uncompleted and completed again in the current month
- **THEN** the current month's file holds it with its new `completed_at`, and the closed month's file still holds it unchanged

### Requirement: Closed months are frozen
A month SHALL close once a run has fetched it at a time at least 7 days after the month ended, in UTC. The tool SHALL NOT fetch a closed month again. Changes made in Todoist later to the completed tasks of a closed month are not captured, including completions backdated into it.

#### Scenario: Within the grace period
- **WHEN** a run happens on 2026-10-05
- **THEN** it fetches 2026-09 again, and 2026-09 stays open

#### Scenario: After the grace period
- **WHEN** a run happens on 2026-10-08 and the next one on 2026-10-09
- **THEN** the first run fetches 2026-09 and closes it, and the second run does not fetch 2026-09

#### Scenario: Historical months
- **WHEN** the first run fetches 2016-03
- **THEN** 2016-03 is closed right after it is stored

#### Scenario: Backdated completion
- **WHEN** a task is completed with a `completed_at` inside a month that is already closed
- **THEN** the closed month's file stays unchanged

### Requirement: Resumable completed-tasks backup
If a run ends before all months are stored, the next run SHALL continue with the first month that is not closed. It SHALL NOT fetch a closed month again.

#### Scenario: Interrupted first run
- **WHEN** the first run is killed after it stored and closed every month through 2019-12
- **THEN** the next run starts with 2020-01 and makes no request for an earlier month

### Requirement: Start of completion events
The first run that backs up completed tasks SHALL record its start time as T0, and later runs SHALL keep it unchanged. Completion events at or after T0 SHALL come from the activity log; completions before T0 SHALL come from the one-time backfill. No completion SHALL be stored by both sources.

#### Scenario: Interrupted first run
- **WHEN** the first run is killed after it recorded T0
- **THEN** the next run uses the same T0

#### Scenario: Event before T0
- **WHEN** the activity log returns a completion event dated before T0
- **THEN** the tool does not store it among the activity log's events

### Requirement: Completion events from the activity log
From T0 on, the tool SHALL back up every task completion event (`item:completed`) of the activity log. This includes completions of recurring tasks and completions by other people in shared projects. Each run SHALL fetch the events since the previous run's fetch, and SHALL store each event once, however often it is fetched.

#### Scenario: Recurring task
- **WHEN** a recurring task is completed twice between two runs
- **THEN** the second run stores two completion events for it

#### Scenario: Overlapping fetches
- **WHEN** a run fetches an event that the previous run already stored
- **THEN** the event is stored once, with the content of the latest fetch

#### Scenario: Large event ID
- **WHEN** an event's `id` is an integer larger than 64 bits
- **THEN** the stored event holds exactly that integer

### Requirement: Gap in the activity log
When the time since the previous fetch of the activity log exceeds the plan's `activity_log_limit` (in days; `-1` means no limit), the tool SHALL warn that completion events in that period may be missing. It SHALL store the events the log still returns and finish with exit status 0.

#### Scenario: Free plan, long pause
- **WHEN** the plan keeps 7 days of activity and the previous fetch was 10 days ago
- **THEN** the tool prints a warning naming the period that may be missing, stores the events it receives, and exits with status 0

#### Scenario: Within the limit
- **WHEN** the plan keeps 7 days of activity and the previous fetch was 2 days ago
- **THEN** the tool prints no warning about the activity log

### Requirement: One-time backfill of earlier completions
The tool SHALL back up the completions before T0 once, from `GET /api/v1/tasks/completed`, with one entry per completion, including those of recurring tasks. It SHALL fetch them month by month from the account's first month through the month of T0. It SHALL resume an interrupted backfill at the first month not stored, and SHALL NOT repeat a backfill that is complete.

#### Scenario: First run
- **WHEN** the first run backs up an account created in March 2016, with T0 in October 2026
- **THEN** it stores the completions of every month from 2016-03 through 2026-10, those of 2026-10 only up to T0

#### Scenario: Interrupted backfill
- **WHEN** a run is killed after the backfill stored every month through 2018-06
- **THEN** the next run continues the backfill with 2018-07

#### Scenario: Backfill complete
- **WHEN** the backfill is complete
- **THEN** later runs make no request to `tasks/completed`

### Requirement: Deprecated endpoint unavailable
When `tasks/completed` is no longer available (HTTP 404 or 410), the tool SHALL warn that completions of recurring tasks before T0 cannot be backed up, keep the backfill months already stored, and finish the rest of the backup with exit status 0. Later runs SHALL NOT request the endpoint again.

#### Scenario: Endpoint removed
- **WHEN** `tasks/completed` answers HTTP 410 during the backfill
- **THEN** the tool prints one warning, keeps the months it already stored, backs up everything else, and exits with status 0

#### Scenario: Later run
- **WHEN** a run follows the one that found `tasks/completed` unavailable
- **THEN** it makes no request to `tasks/completed` and prints no warning about it

### Requirement: Comments of completed tasks
For each completed task whose payload has a `note_count` greater than 0, the tool SHALL back up its comments with their attachment metadata as it appears in the payload. Uploaded attachment files are downloaded as the `attachment-downloads` capability describes. A comment once stored SHALL stay in the backup, even when it is later deleted in Todoist.

#### Scenario: Task with comments
- **WHEN** a completed task with `note_count: 2` is stored for the first time
- **THEN** the tool stores its two comments, including any `file_attachment` object unchanged

#### Scenario: Task without comments
- **WHEN** a completed task has `note_count: 0`
- **THEN** the tool makes no comment request for it

#### Scenario: Comments not on the plan
- **WHEN** the plan information says comments are not available
- **THEN** the tool makes no comment request for completed tasks

### Requirement: Comments refreshed while the month is open
While a month is open, the tool SHALL fetch a task's comments again when the task's `note_count` differs from the one in its stored payload. It SHALL add new comments and update changed ones. It SHALL NOT fetch comments of tasks in closed months again.

#### Scenario: Comment added in an open month
- **WHEN** a comment is added to a task of an open month after the previous run
- **THEN** the next run stores the new comment next to the existing ones

#### Scenario: Comment deleted in an open month
- **WHEN** one of a stored task's two comments is deleted in Todoist and its `note_count` drops to 1
- **THEN** the next run keeps both comments in the backup

#### Scenario: Unchanged count
- **WHEN** a task's `note_count` is the same as in its stored payload
- **THEN** the run makes no comment request for it

### Requirement: Plan without completed tasks
When `user_plan_limits.current.completed_tasks` is false, the tool SHALL warn that completed tasks cannot be backed up on this plan, and SHALL make no request for completed tasks, completion events or their comments. When one of these requests is rejected as restricted by the plan, the tool SHALL warn naming that part and skip it. In both cases the run SHALL finish with exit status 0.

#### Scenario: Flag is false
- **WHEN** the plan information says `completed_tasks: false`
- **THEN** the tool prints a warning, makes no request of this part, stores the active data, and exits with status 0

#### Scenario: Activity log restricted
- **WHEN** the activity log request is rejected as restricted by the plan
- **THEN** the tool warns that completion events cannot be backed up, stores the completed tasks and the backfill, and exits with status 0

### Requirement: Idempotent completed-tasks backup
Two consecutive runs with no change in Todoist in between SHALL leave every file under `completed/` with the same content, and SHALL NOT rewrite any of them.

#### Scenario: Repeat run
- **WHEN** a run completes and a second run follows with no change in Todoist
- **THEN** every file under `completed/` keeps its content and modification time
