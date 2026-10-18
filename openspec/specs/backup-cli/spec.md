# backup-cli Specification

## Purpose

Defines how the user invokes todoist-takeout from the command line or a scheduler, how it is configured, and how it reports progress, warnings and its exit status in unattended batch runs.

## Requirements

### Requirement: Backup command
The tool SHALL provide a `backup` command that takes the target directory as its only positional argument and backs up the account into it. The command SHALL be available as `todoist-takeout backup <target-dir>` and as `python -m todoist_takeout backup <target-dir>`.

#### Scenario: Run a backup
- **WHEN** the user runs `todoist-takeout backup ./my-backup` with a valid token
- **THEN** the tool backs up the account into `./my-backup` and exits with status 0

#### Scenario: Module invocation
- **WHEN** the user runs `python -m todoist_takeout backup ./my-backup`
- **THEN** the tool behaves exactly as `todoist-takeout backup ./my-backup`

#### Scenario: Missing target directory argument
- **WHEN** the user runs `todoist-takeout backup` without a target directory
- **THEN** the tool prints a usage message to stderr and exits with status 2

### Requirement: Token from environment
The tool SHALL read the Todoist personal API token from the environment variable `TODOIST_API_TOKEN`. It SHALL NOT accept the token as a command-line argument, and it SHALL NOT write the token to stdout, stderr or any file.

#### Scenario: Token is set
- **WHEN** `TODOIST_API_TOKEN` contains a token and the user runs a backup
- **THEN** the tool uses that token to authenticate

#### Scenario: Token is missing or empty
- **WHEN** `TODOIST_API_TOKEN` is unset or empty
- **THEN** the tool prints an error naming the variable to stderr, makes no API request, changes no files, and exits with status 2

#### Scenario: Token never appears in output
- **WHEN** any run ends, successfully or with an error, at any verbosity
- **THEN** neither stdout, stderr nor any file in the target directory contains the token

### Requirement: Target directory validation
The tool SHALL create the target directory, including missing parent directories, if it does not exist. It SHALL refuse to use an existing directory that is not empty and is not a todoist-takeout backup.

#### Scenario: New directory
- **WHEN** the target directory does not exist
- **THEN** the tool creates it and writes the backup into it

#### Scenario: Existing backup
- **WHEN** the target directory contains a backup written by this tool
- **THEN** the tool continues that backup

#### Scenario: Foreign non-empty directory
- **WHEN** the target directory exists, is not empty, and contains no backup state file
- **THEN** the tool prints an error to stderr, changes no files, and exits with status 2

#### Scenario: Target is a file
- **WHEN** the target path exists and is a regular file
- **THEN** the tool prints an error to stderr and exits with status 2

### Requirement: Unattended operation
The tool SHALL run to completion without user interaction. It SHALL NOT prompt for input and SHALL NOT require a terminal.

#### Scenario: Run from a scheduler
- **WHEN** the tool runs from cron, launchd or Windows Task Scheduler with stdin closed and no terminal attached
- **THEN** it completes the backup without waiting for input

### Requirement: Diagnostic output
The tool SHALL write all progress messages, warnings and errors to stderr, one line each, with the level (`INFO`, `WARNING`, `ERROR`) at the start of the line. At the end of each run it SHALL print a summary line with the number of objects written, unchanged and marked deleted, and the number of warnings. `--quiet` SHALL suppress `INFO` lines; `--verbose` SHALL add `DEBUG` lines.

#### Scenario: Default verbosity
- **WHEN** a run completes with one warning
- **THEN** stderr contains `INFO` progress lines, one `WARNING` line, and the summary line

#### Scenario: Quiet run
- **WHEN** the user passes `--quiet` and the run produces one warning
- **THEN** stderr contains the `WARNING` line and no `INFO` lines

### Requirement: Exit status
The tool SHALL exit with status 0 when the backup is complete, even if warnings were reported; with status 1 when the run failed after it started working with the API or the target directory; and with status 2 for usage or configuration errors detected before any API request.

#### Scenario: Success with warnings
- **WHEN** a run completes and reports a plan limitation as a warning
- **THEN** the exit status is 0

#### Scenario: Runtime failure
- **WHEN** a run fails because of a network error that persists after all retries
- **THEN** the exit status is 1 and the last line on stderr explains the failure

#### Scenario: Configuration error
- **WHEN** the token is missing or the target directory is invalid
- **THEN** the exit status is 2

### Requirement: Single run per target directory
The tool SHALL allow only one run at a time per target directory. A second run started while the first holds the target SHALL exit without changing any file.

#### Scenario: Concurrent run
- **WHEN** a run is in progress on a target directory and a second run is started on the same directory
- **THEN** the second run prints an error saying another run is in progress and exits with status 1, and the first run is not affected

#### Scenario: Lock after a crash
- **WHEN** a previous run was killed and the user starts a new run on the same directory
- **THEN** the new run proceeds without manual cleanup

### Requirement: Version and help
The tool SHALL print its version with `--version` and a usage description with `--help`, both to stdout with exit status 0.

#### Scenario: Version
- **WHEN** the user runs `todoist-takeout --version`
- **THEN** the tool prints its version number and exits with status 0
