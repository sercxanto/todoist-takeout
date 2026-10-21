# Spec Delta

## ADDED Requirements

### Requirement: Open tasks view
The database SHALL offer a view `open_tasks` with one row per open task in `tasks`, leaving out the tasks of archived projects. Each row SHALL hold the task's ID, content, priority, due date, whether it recurs, deadline and creation time, the names of its project and section, its label names in one column, and whether it is overdue. The rows SHALL be ordered by due date, tasks without one last, then by priority.

#### Scenario: Open task with names
- **WHEN** open task `T1` is in project `Home`, section `Garden` and has the labels `errand` and `weekend`
- **THEN** `open_tasks` has a row for `T1` with the project name `Home`, the section name `Garden` and both label names

#### Scenario: Completed task
- **WHEN** a task has status `completed`
- **THEN** `open_tasks` has no row for it

#### Scenario: Task of an archived project
- **WHEN** an open task belongs to an archived project
- **THEN** `open_tasks` has no row for it

#### Scenario: Task of a project not in the database
- **WHEN** an open task refers to a project that is not in `projects`
- **THEN** `open_tasks` has a row for it without a project name

#### Scenario: Order
- **WHEN** open tasks are due on 2026-10-25 with priority `p2`, on 2026-10-25 with priority `p1`, on 2026-10-22, and not at all
- **THEN** `open_tasks` lists them in the order 2026-10-22, 2026-10-25 `p1`, 2026-10-25 `p2`, no due date

### Requirement: Overdue tasks
A task in `open_tasks` SHALL count as overdue when its due date lies before the current day, or, for a due date with a time, when that time has passed. A due date or time without a time zone SHALL be compared in the local time of the computer that runs the query, a due time in UTC with the current UTC time. A task without a due date SHALL NOT count as overdue.

#### Scenario: Due date in the past
- **WHEN** an open task is due on 2000-01-01
- **THEN** its row in `open_tasks` marks it as overdue

#### Scenario: Due date in the future
- **WHEN** an open task is due on 2999-12-31, or at 2999-12-31T09:00:00Z
- **THEN** its row in `open_tasks` does not mark it as overdue

#### Scenario: Due time passed
- **WHEN** an open task is due at 2000-01-01T09:00:00 or at 2000-01-01T09:00:00Z
- **THEN** its row in `open_tasks` marks it as overdue

### Requirement: Values computed at query time
Values of the views that depend on the current date or time SHALL be computed each time the view is queried, not when the database is built.

#### Scenario: A day passes
- **WHEN** a task is due today, and the database was built today and is queried again tomorrow without a rebuild
- **THEN** the second query marks the task as overdue

### Requirement: Completion log view
The database SHALL offer a view `done_log` with one row per completion: one per row of `completions`, plus one per task in `tasks` with status `completed` for whose ID `completions` holds no row. Each row SHALL hold the task's ID, content, project ID and name, the completion time in UTC and in local time, its source (`activity`, `backfill` or `task`), and whether the task is in `tasks`. The rows SHALL be ordered by completion time, newest first.

#### Scenario: Completion recorded
- **WHEN** completed task `T1` has one row in `completions`
- **THEN** `done_log` has exactly one row for `T1`, with the content, project, time and source of that completion

#### Scenario: Completion not recorded
- **WHEN** task `T2` has status `completed` and `completions` holds no row for `T2`, for example because the activity log had a gap
- **THEN** `done_log` has one row for `T2` with the task's content, project and completion time, and the source `task`

#### Scenario: Recurring task
- **WHEN** recurring task `T3` is open and `completions` holds three rows for it
- **THEN** `done_log` has three rows for `T3`

#### Scenario: Task deleted since
- **WHEN** `completions` holds a row for a task that is not in `tasks`
- **THEN** `done_log` has a row for it that marks the task as no longer in the database

#### Scenario: Task completed again
- **WHEN** a non-recurring task was completed, uncompleted and completed again, and `completions` holds only the first completion
- **THEN** `done_log` has one row for the task, the first completion

### Requirement: Recurring tasks view
The database SHALL offer a view `recurring_stats` with one row per recurring task in `tasks`. Each row SHALL hold the task's ID, content, status, project name and due string, and from the task's rows in `done_log` the number of completions, the first and last completion time, the average number of days between completions, and the days since the last completion.

#### Scenario: Task completed three times
- **WHEN** recurring task `T1` has completions on 2026-10-01, 2026-10-08 and 2026-10-15, at the same time of day
- **THEN** its row in `recurring_stats` holds 3 completions, the first on 2026-10-01, the last on 2026-10-15, and an average interval of 7 days

#### Scenario: Task never completed
- **WHEN** recurring task `T2` has no completion
- **THEN** its row in `recurring_stats` holds 0 completions, and no first or last completion, interval or days since the last completion

#### Scenario: Task completed once
- **WHEN** recurring task `T3` has one completion
- **THEN** its row in `recurring_stats` holds 1 completion and no average interval

#### Scenario: Task that does not recur
- **WHEN** a task does not recur
- **THEN** `recurring_stats` has no row for it

## MODIFIED Requirements

### Requirement: Datasette metadata file
After the database, the tool SHALL write a Datasette metadata file next to it, named after the database with its last suffix replaced by `.metadata.json`. It SHALL describe the database, every table, every view and every column, set up facets, sort orders, row labels and full-text search for the tables, and set up facets for the views. It SHALL be written atomically.

#### Scenario: Name of the metadata file
- **WHEN** the database is `~/todoist.db`, or `~/todoist` without a suffix
- **THEN** the metadata file is `~/todoist.metadata.json`

#### Scenario: Opened in Datasette
- **WHEN** the user runs `datasette todoist.db -m todoist.metadata.json`
- **THEN** Datasette shows the descriptions, offers the facets, shows project, section and task names in place of their IDs, and offers full-text search

#### Scenario: Views in Datasette
- **WHEN** the user opens the view `open_tasks`, `done_log` or `recurring_stats` in Datasette with the metadata file
- **THEN** Datasette shows the descriptions of the view and its columns, and offers the view's facets

#### Scenario: Foreign metadata file
- **WHEN** a file with the metadata file's name exists and was not written by the tool
- **THEN** the tool prints an error to stderr, changes neither file, and exits with status 2
