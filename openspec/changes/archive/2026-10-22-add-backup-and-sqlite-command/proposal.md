# Proposal

## Why

To keep both the JSON backup and the Datasette database current, users now run two commands and pass two paths, typically in one scheduled job. It is easy to put the database inside the backup directory, where it sits among the JSON files it is derived from. One command with one directory that runs both steps, and keeps the database outside the JSON files, makes the common setup a single line.

## What Changes

- A new command `todoist-takeout backup-and-sqlite <dir>` (also `python -m todoist_takeout backup-and-sqlite <dir>`) that runs a backup and then rebuilds the SQLite database, in one run.
- `<dir>` has a fixed layout: the backup in `<dir>/json/`, by default the database in `<dir>/todoist.db` and its Datasette metadata file in `<dir>/metadata.json`, the name that Datasette's configuration directory mode loads, so that `datasette <dir>` opens the database with its metadata without `-m`. By default the database therefore never lies in the directory that holds the JSON files.
- `--database PATH` puts the database elsewhere, with the metadata file next to it as for `sqlite`. Any path is accepted, also one inside `json/`, when the user asks for it explicitly.
- The command creates `<dir>` and `json/` if missing, and refuses a directory that is not empty and holds no `json/` subdirectory. A directory that is itself a backup (it holds `state.json`) is refused with a hint on how to move it into `json/`.
- All checks that need no API request, including those of the database and metadata file, run before the backup starts, so a configuration error never costs a backup run.
- If the backup fails, the database is not rebuilt and keeps its previous content. If the rebuild fails, the backup stays complete.
- The command takes `--quiet`, `--verbose` and `--max-rate-limit-wait` like `backup`, reads the token like `backup`, and only one `backup-and-sqlite` run at a time can use a directory.
- `backup` and `sqlite` are unchanged; they still accept any paths, including a database inside a backup directory.

## Capabilities

### New Capabilities
- `backup-and-sqlite`: the `backup-and-sqlite` command, its directory layout and validation, the database path and its override, the order of backup and database rebuild, and its messages and exit status.

### Modified Capabilities
<!-- none: `backup` and `sqlite` keep their requirements; `backup-and-sqlite` reuses them. -->

## Impact

- Code: `src/todoist_takeout/cli.py` (new subcommand, summary output), a new module `src/todoist_takeout/backup_and_sqlite.py` that checks the layout and calls `sync.run_backup` and `sqlite_view.run`; the existing checks in `sqlite_view` (`check_database`, `check_metadata`, `metadata_file`) are reused before the backup starts. `sqlite_view.run` gets an optional parameter for the metadata file's path; the `sqlite` command does not use it and is unchanged.
- Tests: new `tests/test_backup_and_sqlite.py`, offline against the existing fake API and fixtures.
- Docs: README (usage, layout, scheduling example). The layout is a new on-disk convention and is documented there; the backup format in `json/` is unchanged.
- No new dependencies.
