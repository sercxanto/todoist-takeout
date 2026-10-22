# backup-and-sqlite Specification

## Purpose

Defines the `backup-and-sqlite` command, which backs up the account and rebuilds the SQLite database from it in one run, in one directory with a fixed layout, the target directory, that by default keeps the database outside the directory of the JSON backup in its subdirectory `json`.

## Requirements

### Requirement: Backup-and-sqlite command
The tool SHALL provide a `backup-and-sqlite` command that takes one directory as its only positional argument, brings the backup in it up to date with the account, and then rebuilds the database from that backup. It SHALL be available as `todoist-takeout backup-and-sqlite <dir>` and as `python -m todoist_takeout backup-and-sqlite <dir>`.

#### Scenario: Run both steps
- **WHEN** the user runs `todoist-takeout backup-and-sqlite ~/Backups/todoist` with a valid token
- **THEN** the tool backs up the account into `~/Backups/todoist/json`, builds `~/Backups/todoist/todoist.db` and `~/Backups/todoist/metadata.json` from it, and exits with status 0

#### Scenario: Module invocation
- **WHEN** the user runs `python -m todoist_takeout backup-and-sqlite ~/Backups/todoist`
- **THEN** the tool behaves exactly as `todoist-takeout backup-and-sqlite ~/Backups/todoist`

#### Scenario: Missing directory argument
- **WHEN** the user runs `todoist-takeout backup-and-sqlite` without a directory
- **THEN** the tool prints a usage message to stderr, changes no files, and exits with status 2

### Requirement: Directory layout
The target directory SHALL hold the backup in its subdirectory `json`, and, unless `--database` is given, the database in the file `todoist.db` and the Datasette metadata file in `metadata.json`, so that by default the database is not in the directory that holds the JSON files, and Datasette opens the target directory with the database and its metadata. The names SHALL be fixed. The backup in `json` SHALL have the same format as one written by the `backup` command.

#### Scenario: Paths after a run
- **WHEN** a run on `~/Backups/todoist` completes
- **THEN** `~/Backups/todoist/json` holds the backup with its `state.json`, `~/Backups/todoist/todoist.db` holds the database, and no database or metadata file is in `~/Backups/todoist/json`

#### Scenario: Opened in Datasette as a directory
- **WHEN** a run on `~/Backups/todoist` has completed and the user runs `datasette ~/Backups/todoist`
- **THEN** Datasette opens `todoist.db` with the metadata from `metadata.json`, and no other database

#### Scenario: Backup used by the other commands
- **WHEN** a run on `~/Backups/todoist` has completed
- **THEN** `todoist-takeout backup ~/Backups/todoist/json` continues that backup, and `todoist-takeout sqlite ~/Backups/todoist/json ~/Backups/todoist/todoist.db` rebuilds that database

### Requirement: Database path option
The `backup-and-sqlite` command SHALL accept `--database PATH`, which replaces `todoist.db` in the target directory as the database file. The metadata file SHALL then be named and placed as the `sqlite` command names and places it for that path. Any path SHALL be accepted that the `sqlite` command accepts as its database, also one inside `json`.

#### Scenario: Database elsewhere
- **WHEN** the user runs `todoist-takeout backup-and-sqlite --database ~/todoist.db ~/Backups/todoist`
- **THEN** the backup is in `~/Backups/todoist/json`, the database is `~/todoist.db` with its metadata file `~/todoist.metadata.json`, and neither `todoist.db` nor `metadata.json` is created in `~/Backups/todoist`

#### Scenario: Database inside the backup directory on request
- **WHEN** the user runs `todoist-takeout backup-and-sqlite --database ~/Backups/todoist/json/todoist.db ~/Backups/todoist`
- **THEN** the tool builds the database there and exits with status 0, and the next run on `~/Backups/todoist` with the same option continues the backup and rebuilds that database

### Requirement: Directory validation
The tool SHALL create the target directory, missing parents included, and its `json` subdirectory if they do not exist. It SHALL accept an existing directory that is empty or holds a `json` subdirectory, and refuse any other existing path without changing it. A directory that holds `state.json` itself SHALL be refused with a message that it is a backup directory and how to move its content into `json`.

#### Scenario: New directory
- **WHEN** the target directory does not exist
- **THEN** the tool creates it and `json` inside it, and runs both steps

#### Scenario: Empty directory
- **WHEN** the target directory exists and is empty
- **THEN** the tool creates `json` inside it and runs both steps

#### Scenario: Foreign non-empty directory
- **WHEN** the target directory is not empty and holds no `json` subdirectory
- **THEN** the tool prints an error to stderr, changes no files, makes no API request, and exits with status 2

#### Scenario: Backup directory given
- **WHEN** the target directory holds `state.json` and no `json` subdirectory
- **THEN** the tool prints an error saying that it is a backup directory and that its content can be moved into a subdirectory `json`, changes no files, makes no API request, and exits with status 2

#### Scenario: Path is a file
- **WHEN** the target directory path is a regular file
- **THEN** the tool prints an error to stderr, makes no API request, and exits with status 2

#### Scenario: Foreign json subdirectory
- **WHEN** `json` exists, is not empty, and is not a todoist-takeout backup
- **THEN** the tool prints an error to stderr, changes no files, makes no API request, and exits with status 2

### Requirement: Checks before the backup
The tool SHALL check the token, the target directory, the backup in `json`, the database file and the metadata file before it makes any API request. The database and metadata file SHALL be accepted under the same rules as by the `sqlite` command. A failed check SHALL change no files and exit with status 2.

#### Scenario: Foreign database file
- **WHEN** the database file, `todoist.db` in the target directory or the path given with `--database`, is a file that the tool did not build
- **THEN** the tool prints an error naming the file, makes no API request, changes no files, and exits with status 2

#### Scenario: Foreign metadata file
- **WHEN** a file with the metadata file's name exists and was not written by the tool
- **THEN** the tool prints an error naming the file, makes no API request, changes no files, and exits with status 2

#### Scenario: Token is missing
- **WHEN** `TODOIST_API_TOKEN` is unset or empty
- **THEN** the tool prints an error naming the variable, makes no API request, changes no files, and exits with status 2

#### Scenario: Backup from a newer tool version
- **WHEN** `json/state.json` declares a format version that the tool does not support
- **THEN** the tool prints an error to stderr, makes no API request, changes no files, and exits with status 2

### Requirement: Backup, then database
The tool SHALL rebuild the database only after the backup completed, from the backup as that run left it. The backup SHALL behave as a `backup` run on `json`, and the rebuild as a `sqlite` run from `json` into the database file, with their guarantees on interruption, atomic writes and an open database. The database SHALL be rebuilt on every successful backup, also when nothing changed.

#### Scenario: Backup fails
- **WHEN** the backup fails, for example because of a network error that persists after all retries
- **THEN** the database and metadata file keep their previous content, or are not created, and the exit status is 1

#### Scenario: Rebuild fails
- **WHEN** the backup completes and the rebuild then fails, for example because another program keeps the database locked
- **THEN** the backup in `json` is complete, the database keeps its previous content, the last line on stderr explains the failure, and the exit status is 1

#### Scenario: Interrupted
- **WHEN** the user interrupts a run
- **THEN** the next run resumes the backup from its last checkpoint and the database holds the complete content of a previous rebuild, or no tables if there was none

#### Scenario: Nothing changed
- **WHEN** a run starts on a backup whose account did not change since the previous run
- **THEN** the database is rebuilt and the exit status is 0

### Requirement: Single run per directory
The tool SHALL allow only one `backup-and-sqlite` run at a time per target directory, for the whole run. A second `backup-and-sqlite` run started while the first holds the directory SHALL exit without changing any file. A `backup` or `sqlite` run on `json` SHALL be kept out as it is by those commands.

#### Scenario: Concurrent run
- **WHEN** a run is in progress on a directory and a second run is started on it
- **THEN** the second run prints an error saying another run is in progress, changes no files, and exits with status 1, and the first run completes the backup and the rebuild

#### Scenario: Backup run in progress
- **WHEN** a `backup` run holds `json` and a run is started on the directory
- **THEN** the run prints an error saying another run is in progress, leaves the database unchanged, and exits with status 1

#### Scenario: Lock after a crash
- **WHEN** a previous run was killed and the user starts a new one on the same directory
- **THEN** the new run proceeds without manual cleanup

### Requirement: Options and token
The `backup-and-sqlite` command SHALL accept `--quiet`, `--verbose` and `--max-rate-limit-wait` with the meaning they have for `backup`. It SHALL read the token as `backup` does, and SHALL NOT write it to stdout, stderr or any file in the target directory.

#### Scenario: Rate limit wait
- **WHEN** the user runs `todoist-takeout backup-and-sqlite --max-rate-limit-wait 3600 ~/Backups/todoist`
- **THEN** the backup gives up only when waiting for the rate limit would exceed 3600 seconds in total

#### Scenario: Token never appears in output
- **WHEN** any run ends, successfully or with an error, at any verbosity
- **THEN** neither stdout, stderr nor any file in the target directory contains the token

### Requirement: Diagnostic output and exit status
The `backup-and-sqlite` command SHALL write its messages to stderr as the `backup` command does. A successful run SHALL end with the backup's summary line, then the line naming how to open the database in Datasette, then the database's summary line, which counts the warnings of the whole run. It SHALL exit with status 0 on success, even with warnings, 1 when the run failed after it started, and 2 for usage or configuration errors.

#### Scenario: Successful run
- **WHEN** a run completes with one warning from the backup
- **THEN** stderr holds `INFO` lines, the `WARNING` line, the `backup complete` summary line, the Datasette hint, and ends with the `database complete` summary line naming `1 warning`, and the exit status is 0

#### Scenario: Datasette hint
- **WHEN** a run on `~/Backups/todoist` completes without `--database`, or with `--database ~/todoist.db`
- **THEN** the hint names `datasette ~/Backups/todoist`, or `datasette ~/todoist.db -m ~/todoist.metadata.json`

#### Scenario: Quiet run
- **WHEN** the user passes `--quiet` and the run produces no warning
- **THEN** stderr is empty
