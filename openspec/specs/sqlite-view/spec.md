# sqlite-view Specification

## Purpose

Defines the `sqlite` command, which derives a simplified SQLite database from a backup for browsing it, for example in Datasette, together with a Datasette metadata file that describes the database.

## Requirements

### Requirement: SQLite command
The tool SHALL provide a `sqlite` command that takes the backup directory and the database file as its two positional arguments, both required, and builds the database from the backup. It SHALL be available as `todoist-takeout sqlite <backup-dir> <database>` and as `python -m todoist_takeout sqlite <backup-dir> <database>`, and accept `--quiet` and `--verbose` as the `backup` command does.

#### Scenario: Build a database
- **WHEN** the user runs `todoist-takeout sqlite ./my-backup ./todoist.db` on a backup
- **THEN** the tool writes the database `./todoist.db` and its metadata file `./todoist.metadata.json`, and exits with status 0

#### Scenario: Module invocation
- **WHEN** the user runs `python -m todoist_takeout sqlite ./my-backup ./todoist.db`
- **THEN** the tool behaves exactly as `todoist-takeout sqlite ./my-backup ./todoist.db`

#### Scenario: Missing database argument
- **WHEN** the user runs `todoist-takeout sqlite ./my-backup`
- **THEN** the tool prints a usage message to stderr, changes no files, and exits with status 2

### Requirement: Offline operation
The `sqlite` command SHALL make no network request and SHALL NOT need the token. It SHALL NOT read `TODOIST_API_TOKEN`.

#### Scenario: No token
- **WHEN** `TODOIST_API_TOKEN` is unset and the user runs the `sqlite` command on a backup
- **THEN** the database is built and the exit status is 0

#### Scenario: No network
- **WHEN** the computer has no network connection
- **THEN** the `sqlite` command builds the database as it would with one

### Requirement: Backup is read, never changed
The `sqlite` command SHALL accept only a directory that holds a backup in a format version the tool supports, and SHALL NOT write, change or remove any file in it, apart from creating the lock file if it is missing. It SHALL show the data of the backup as the last applied sync and the stored files hold it.

#### Scenario: Not a backup
- **WHEN** the backup directory does not exist, is a file, or has no `state.json`
- **THEN** the tool prints an error to stderr, writes no database, and exits with status 2

#### Scenario: Backup from a newer tool version
- **WHEN** `state.json` declares a format version that the tool does not support
- **THEN** the tool prints an error to stderr, writes no database, and exits with status 2

#### Scenario: Backup unchanged
- **WHEN** the `sqlite` command has run on a backup
- **THEN** every file in the backup directory has the same content and modification time as before

#### Scenario: Response not applied yet
- **WHEN** the backup holds a sync response in `pending/` that an interrupted run did not apply
- **THEN** the database does not show the objects of that response

### Requirement: Backup lock
The `sqlite` command SHALL hold the backup's lock while it reads the backup, so that it never reads a backup that a run is changing. When the lock file cannot be opened because the backup is on a read-only file system, it SHALL read the backup without the lock and print a warning.

#### Scenario: Backup in progress
- **WHEN** a backup run holds the lock of the backup directory and the user runs the `sqlite` command on it
- **THEN** the tool prints an error saying that another run is in progress, leaves the database unchanged, and exits with status 1

#### Scenario: Database build in progress
- **WHEN** the `sqlite` command holds the lock of a backup directory and a backup run is started on it
- **THEN** the backup run prints an error saying that another run is in progress and exits with status 1

#### Scenario: Read-only backup
- **WHEN** the backup directory is on a read-only file system
- **THEN** the tool prints a warning that it reads the backup without the lock, builds the database, and exits with status 0

### Requirement: Database target
The tool SHALL create the database file, and missing parent directories, if it does not exist. It SHALL accept an existing empty file or a database it built itself, and SHALL refuse any other existing file without changing it.

#### Scenario: New database
- **WHEN** the database path does not exist
- **THEN** the tool creates the database file

#### Scenario: Database built before
- **WHEN** the database path holds a database that the `sqlite` command built, by this or another version of the tool
- **THEN** the tool rebuilds its content

#### Scenario: Foreign SQLite database
- **WHEN** the database path holds a SQLite database that the `sqlite` command did not build
- **THEN** the tool prints an error to stderr, leaves the file unchanged, and exits with status 2

#### Scenario: Other file or directory
- **WHEN** the database path is a directory, or a file that is not a SQLite database
- **THEN** the tool prints an error to stderr, leaves it unchanged, and exits with status 2

### Requirement: Rebuild in one transaction
Each run SHALL replace the whole content of the database, schema included, in one SQLite transaction in the existing file. A run that fails or is interrupted SHALL leave the previous content intact. The database SHALL record the version of its schema.

#### Scenario: Run fails while building
- **WHEN** a run is killed while it builds the database
- **THEN** the database holds the complete content of the previous run, or no tables if there was none

#### Scenario: Older schema
- **WHEN** the database was built by an older version of the tool with an older schema
- **THEN** the run replaces it with the current schema and content

#### Scenario: Same backup twice
- **WHEN** the `sqlite` command runs twice on an unchanged backup
- **THEN** both runs produce the same tables, rows and values

### Requirement: Open database
The tool SHALL rebuild a database that another program, such as Datasette, has open for reading, on Windows, macOS and Linux. That program SHALL see the new content on its next query, without a restart. If the other program keeps the database locked for longer than a few seconds, the run SHALL fail and leave the previous content.

#### Scenario: Datasette is running
- **WHEN** another process has the database open, between queries, while the tool rebuilds it
- **THEN** the rebuild succeeds, and the other process's next query returns the new content

#### Scenario: Database kept locked
- **WHEN** another process holds a transaction on the database for the whole time the tool waits
- **THEN** the tool prints an error naming the database, leaves its previous content, and exits with status 1

### Requirement: Current copy of each object
The database SHALL hold one row per project, section, task and comment, built from its current copy in the backup. For tasks the current copy SHALL be the one the format description defines. For projects and sections it SHALL be the copy with the newest `updated_at`, and on equal values the one under `archived/`. For comments the copy under `archived/` SHALL win, then `completed/`, then `sync/`.

#### Scenario: Task completed and archived
- **WHEN** a task is stored in `sync/items/` as open, in `completed/2026-05.json` as completed, and in the snapshot of its archived project with the same `updated_at` as in the month file
- **THEN** the database has one row for the task, built from the snapshot's copy

#### Scenario: Comment stored twice
- **WHEN** a comment is stored in `completed/comments/` and, with other content, in the snapshot of an archived project
- **THEN** the database has one row for the comment, with the snapshot's content

### Requirement: Deleted objects are left out
A project, section, label, task or comment that counts as deleted under `sync/` SHALL NOT be in the database, even if other copies of it are stored under `completed/` or `archived/`.

#### Scenario: Deleted task
- **WHEN** a task's file in `sync/items/` has a tombstone, and the task is also in a month file under `completed/`
- **THEN** the task is not in the `tasks` table

#### Scenario: Restored task
- **WHEN** a task was deleted and later restored, so that its tombstone was removed
- **THEN** the task is in the `tasks` table

### Requirement: Only resources for display
The database SHALL hold the tables `projects`, `sections`, `labels`, `tasks`, `task_labels`, `comments` and `completions`, and SHALL NOT hold the other resources of the backup, such as reminders, filters, collaborators, workspaces, user settings, view options or statistics.

#### Scenario: Reminders
- **WHEN** the backup holds reminders under `sync/reminders/`
- **THEN** the database has no table or column holding them

### Requirement: Projects table
The `projects` table SHALL hold every project that is not deleted, archived projects included, with its name, its parent project, whether it is archived, and its creation and update times.

#### Scenario: Archived project
- **WHEN** the backup holds a snapshot of archived project `P1`
- **THEN** `projects` has a row for `P1` that marks it as archived

#### Scenario: Sub-project
- **WHEN** project `P2` has `parent_id` `P1`
- **THEN** the row of `P2` refers to the row of `P1` as its parent

### Requirement: Sections table
The `sections` table SHALL hold every section that is not deleted, from `sync/sections/` and from the snapshots of archived projects, with its name, its project, and whether it is archived.

#### Scenario: Section of an archived project
- **WHEN** the snapshot of an archived project holds a section in its `full.json`
- **THEN** `sections` has a row for it that refers to the project

### Requirement: Labels of tasks
The `labels` table SHALL hold every label under `sync/labels/` that is not deleted. The `task_labels` table SHALL hold one row per label name of each task in `tasks`, also for a name that `labels` does not hold, such as a shared label.

#### Scenario: Task with two labels
- **WHEN** a task's current copy has `labels` `["home", "errand"]`
- **THEN** `task_labels` has two rows for the task, one per name

#### Scenario: Shared label
- **WHEN** a task has a label name that is not in `sync/labels/`
- **THEN** `task_labels` holds the name for the task, and `labels` has no row for it

### Requirement: Tasks table
The `tasks` table SHALL hold every task that is not deleted, open and completed, with its content, description, project, section, parent task, a status of `open` or `completed`, its priority as Todoist shows it (`p1` highest to `p4`), its due date and whether it recurs, its deadline, and its creation, completion and update times.

#### Scenario: Completed task
- **WHEN** a task's current copy has `checked` true and a `completed_at`
- **THEN** its row has status `completed` and that completion time

#### Scenario: Priority
- **WHEN** a task's current copy has the API's `priority` 4
- **THEN** its row has the priority `p1`

#### Scenario: Recurring task
- **WHEN** a task's current copy has a `due` with `is_recurring` true
- **THEN** its row shows the due date of the next occurrence and marks the task as recurring

### Requirement: Comments table
The `comments` table SHALL hold every comment that is not deleted, of tasks and of projects, with its task or project, content and time. For an uploaded file it SHALL hold the file's name and its path in the backup, relative to the backup directory, or no path when the file is not downloaded. For a link it SHALL hold the URL.

#### Scenario: Downloaded attachment
- **WHEN** comment `C1` has an uploaded file `preise.pdf` stored at `attachments/C1/preise.pdf`
- **THEN** its row holds the name `preise.pdf` and the path `attachments/C1/preise.pdf`

#### Scenario: Attachment not downloaded
- **WHEN** a comment has an uploaded file that is not in the backup
- **THEN** its row holds the file's name and no path

#### Scenario: Project comment
- **WHEN** the backup holds a comment of project `P1`
- **THEN** its row refers to `P1` and to no task

### Requirement: Completions table
The `completions` table SHALL hold one row per completion from `completed/activity/` and `completed/backfill/`, with the completed task's ID, its content and project at the time of completion, the completion time, and the source. It SHALL keep completions of tasks that are not in `tasks`, such as deleted tasks.

#### Scenario: Recurring task completed three times
- **WHEN** the activity log holds three completion events of a recurring task
- **THEN** `completions` has three rows for the task, one per completion time

#### Scenario: Completion before T0
- **WHEN** the backfill holds a completion before T0
- **THEN** `completions` has a row for it whose source is the backfill

#### Scenario: Completion of a deleted task
- **WHEN** a completion event refers to a task that counts as deleted
- **THEN** `completions` has a row for it with the task's ID and the content from the event

### Requirement: Exact IDs
Every ID in the database SHALL be stored as text, exactly as the backup holds it, including the IDs of activity events that exceed 64 bits.

#### Scenario: Large event ID
- **WHEN** a completion event has the `id` `152735830295847392018475629384756102938`
- **THEN** its row in `completions` holds exactly that text as its ID

### Requirement: Links between tables
The database SHALL declare the references between its tables as foreign keys: sections, tasks and comments to their project, tasks and comments to their task, tasks to their section, task labels to their task, and projects and tasks to their parent. A reference to a row that is not in the database, such as a deleted project, SHALL keep the stored ID.

#### Scenario: Task of a deleted project
- **WHEN** a task refers to a project that is not in `projects`
- **THEN** its row keeps the project's ID, and the database is built without an error

### Requirement: Full-text search
The database SHALL offer full-text search over the content and description of tasks and the content of comments. If the SQLite library of the Python installation lacks full-text search, the tool SHALL build the database without it and print a warning.

#### Scenario: Search a word
- **WHEN** a task's description contains `Steuerberater`
- **THEN** a full-text search for `Steuerberater` in `tasks` finds it

#### Scenario: No full-text search available
- **WHEN** the SQLite library cannot create full-text search tables
- **THEN** the tool builds the database without search, prints a warning, and exits with status 0

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

### Requirement: Diagnostic output and exit status
The `sqlite` command SHALL write its messages to stderr as the `backup` command does, and end a successful run with a summary line naming the number of rows per table and the number of warnings. It SHALL exit with status 0 on success, even with warnings, 1 when the build failed after it started, and 2 for usage or configuration errors.

#### Scenario: Successful run
- **WHEN** a run completes without warnings
- **THEN** stderr ends with an `INFO` summary line naming the rows of `tasks`, `completions` and the other tables and `0 warnings`, and the exit status is 0

#### Scenario: Quiet run
- **WHEN** the user passes `--quiet` and the run produces no warning
- **THEN** stderr is empty
