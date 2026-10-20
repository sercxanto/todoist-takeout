# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation sessions.

- Start each session fresh with `/opsx:apply add-sqlite-view`, name the groups it covers, and stop at the end of the last one.
- Commit after each completed group, once its tests pass.
- No task needs a token or the real API.
- Session 1: groups 1 and 2, medium effort.
- Session 2: group 3, high effort. The transaction, the open-database behaviour on Windows and the protection of foreign files decide whether the command is safe to run next to Datasette.
- Session 3: groups 4, 5 and 6, medium effort. Task 6.1 needs the user to look at Datasette in a browser.

## 1. Command and reading the backup

- [x] 1.1 Add the `sqlite` subcommand to `cli.py` with `BACKUP`, `DATABASE`, `--quiet` and `--verbose`, move the token check into the `backup` branch, and create `sqlite_view.py` with an entry point that the CLI calls (design D1). Verify with CLI tests: `todoist-takeout sqlite` without the database argument exits 2 with a usage message, `python -m todoist_takeout sqlite --help` works, `sqlite` runs with `TODOIST_API_TOKEN` unset, and every existing test of `tests/test_cli.py` passes unchanged
- [x] 1.2 Check the backup directory with `storage.read_state` and without writing to it (design D2). Verify with tests for the scenarios "Not a backup" and "Backup from a newer tool version" (exit 2, no database file created), and a test that records the content and modification time of every file in a backup before and after a run ("Backup unchanged")
- [x] 1.3 Hold `TargetLock` while reading, and let it report a read-only file system separately so that the command warns and reads without the lock (design D2). Verify with tests for the scenarios "Backup in progress" and "Database build in progress" of the spec requirement "Backup lock", and for "Read-only backup" by making the lock file's open fail with `EROFS` and with `EACCES`
- [x] 1.4 Extend the Lock file section of `storage_format.md` (and so the backup's `README.md`) with the `sqlite` command holding the lock while it reads (design D11). Verify that `tests/test_storage_format.py` passes and the expected tree's `README.md` matches

## 2. Copies and deletions

- [x] 2.1 Collect the candidate copies of tasks, projects, sections, comments and labels from every place of design D4, skipping names that start with `.` and the `pending/` directory. Verify with tests on hand-made backups that each place is read, a temporary file is ignored, and a sync response in `pending/` does not appear ("Response not applied yet")
- [x] 2.2 Choose the current copy per `id` by `updated_at` and place rank, with the tie rules of design D4 for two snapshots and for missing or invalid times. Verify with tests for the scenarios of the spec requirement "Current copy of each object", a task in two month files, two snapshots of the same task, and a copy without `updated_at`
- [x] 2.3 Collect the deleted IDs per resource under `sync/` and drop every copy of those objects (design D4). Verify with tests for the scenarios of the spec requirement "Deleted objects are left out", for a blanked deletion payload in `<key>.json`, and for a deleted project, section, label and comment
- [x] 2.4 Collect the completions from `completed/activity/` and `completed/backfill/` without the deletion filter, keeping event IDs exact. Verify with tests for the scenarios of the spec requirements "Completions table" and "Exact IDs", and for an event with another `event_type`, which is left out

## 3. Database

- [x] 3.1 Implement the check of the database path from design D7 by reading the SQLite header, before any connection. Verify with tests for the scenarios of the spec requirement "Database target": missing file and parents, empty file, a database the tool built, a foreign SQLite database, a text file and a directory, each foreign one unchanged byte for byte
- [x] 3.2 Create the schema of design D5 with its primary and foreign keys, `application_id` and `user_version`, and turn the chosen copies into rows: status, priority mapping, due and deadline, task labels with `label_id`, comment attachments with `attachment_path` only when the file is stored. Verify with tests for the scenarios of the spec requirements "Only resources for display", "Projects table", "Sections table", "Labels of tasks", "Tasks table", "Comments table" and "Links between tables", reading `PRAGMA foreign_key_list` for each table
- [x] 3.3 Rebuild the database in one `BEGIN IMMEDIATE` transaction with the busy timeout and `auto_vacuum` of design D6, dropping every existing table, view, index and trigger first. Verify with tests for the scenarios of the spec requirement "Rebuild in one transaction" (failure raised after some inserts leaves the previous content; an old schema with another table is replaced; two runs on the same backup give equal rows), and that the file does not grow when the next run has fewer rows
- [ ] 3.4 Verify the spec requirement "Open database" with a second `sqlite3` connection that queries before and after a rebuild, and one that holds `BEGIN IMMEDIATE` while the busy timeout is shortened (exit 1, previous content kept, error naming the database). Verify that both tests run, not skip, on every platform of the CI matrix
- [x] 3.5 Add the FTS5 tables of design D10, and the fallback without them. Verify with tests for the scenarios of the spec requirement "Full-text search", the fallback simulated by making the `CREATE VIRTUAL TABLE` statement fail with "no such module: fts5"

## 4. Datasette metadata file

- [x] 4.1 Generate the metadata file of design D8 from the same table definitions as the schema, with descriptions of every table and column, facets, sort orders, label columns and `fts_table` (left out without FTS5), and write it atomically after the commit. Verify with tests for the scenarios "Name of the metadata file" and "Foreign metadata file" (checked before the database is touched), that every table and column of the database has a description, and that the file is byte-identical on two runs over the same backup
- [x] 4.2 Add `datasette>=0.65.5,<0.66` to the `dev` dependency group and update `uv.lock`, then add the in-process Datasette test of design D12 for the scenario "Opened in Datasette": descriptions shown, facets offered on `tasks`, the project's name in place of `project_id`, and a search over `tasks` that finds a word of a description. Verify that `uv sync --locked` succeeds and the test passes

## 5. Messages, documentation and the whole command

- [ ] 5.1 Add the messages and exit codes of design D9, with `sqlite3.Error` reported as a storage error naming the database. Verify with CLI tests for the scenarios of the spec requirement "Diagnostic output and exit status", that an interrupt exits 130, and that the last stderr line of a failed run explains it
- [ ] 5.2 Run the whole command on a backup built by `run_backup` from the existing fixtures, with a backup path and a database path that contain a space and a non-ASCII character (design D13). Verify that the database holds the expected row counts and that `attachment_path` uses `/`
- [ ] 5.3 Write the README section "Viewing the backup" of design D11: the command, `uvx datasette todoist.db -m todoist.metadata.json`, the tables and their columns in short, the warning against Datasette's `-i`, and roadmap item 5 marked implemented with links to the spec and the archived planning. Update the SQLite line in CONTRIBUTING.md's key decisions. Verify that the documented commands run as written against a test backup

## 6. Integration checks

- [ ] 6.1 With the user, open a database built from a real backup in Datasette 0.65 and in the current 1.0 alpha (`uvx --with datasette==1.0a41 ...`), and check that the descriptions, facets, labels and search work in both. Record the result for 1.0 in design.md, Risks; if 1.0 ignores an option, settle with the user whether to also write a `datasette.yaml` before archiving
- [ ] 6.2 Run `uv run ruff format .`, `uv run ruff check .` and `uv run pytest`, and fix what they report. Verify that all three finish without findings or failures

## Workflow follow-up

- Archive the change with `/opsx:archive` once the pull request is reviewed, which creates the main spec `openspec/specs/sqlite-view/spec.md`.
