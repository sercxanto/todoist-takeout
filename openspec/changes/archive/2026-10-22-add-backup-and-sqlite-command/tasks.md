# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation session.

- One session with `/opsx:apply add-backup-and-sqlite-command`, medium effort. Commit after each completed group, once its tests pass.
- No task needs a token or the real API; the tests use the fake API and the recorded fixtures through `client_factory`.
- The outer lock touches file locking (design D6). The manual CI run on Windows and macOS is left out while the CI minutes are used up; design.md records it.

## 1. Combined module

- [x] 1.1 Add `src/todoist_takeout/backup_and_sqlite.py` with `BACKUP_DIR`, `DATABASE_FILE`, `check_directory` and `run(directory, client, database=None) -> Outcome` in the order of design D2, holding `TargetLock(directory)` for the whole run (D3) and logging the backup summary before the rebuild (D4). Verify with tests in a new `tests/test_backup_and_sqlite.py` for the spec requirements "Directory validation" (every scenario, each asserting no request and no changed files where the spec says so), "Checks before the backup" (foreign database, foreign metadata file, newer format version), and "Backup, then database" (backup fails → database unchanged; rebuild fails because of a held database lock → backup complete; nothing changed → rebuilt)
- [x] 1.2 Add tests for the spec requirements "Directory layout" and "Database path option": the paths after a run, `backup` and `sqlite` working on the result, `--database` elsewhere creating no `todoist.db` in the directory, and `--database` inside `json/` working on two consecutive runs. Verify that they pass
- [x] 1.3 Add tests for "Single run per directory": a held `<dir>/.lock` stops a second run with status 1 and no changes; a held `json/.lock` stops it with status 1 and the database unchanged; a stale lock file after a killed run does not block. Verify that they pass

## 2. Command line

- [x] 2.1 Add the `backup-and-sqlite` subparser to `cli.py` with `TARGET`, `--database`, the verbosity group and `--max-rate-limit-wait` (moved into a helper shared with `backup`), read the token for `backup` and `backup-and-sqlite`, and print the Datasette hint and the database summary with the warning count of the whole run, sharing the summary helpers with `backup` and `sqlite` (design D1, D5). Verify with tests for the spec requirements "Backup-and-sqlite command" (including module invocation and the missing argument), "Options and token" (rate-limit wait reaches the client, token absent from stderr and from every file in the directory) and "Diagnostic output and exit status" (order of the summary lines, `1 warning`, empty stderr with `--quiet`), and that the existing tests of `tests/test_cli.py` and `tests/test_sqlite_view.py` still pass unchanged
- [x] 2.2 Map `KeyboardInterrupt` during `backup-and-sqlite` to the message of design D4 and status 130. Verify with a test that interrupts the rebuild and checks the message and that the database keeps its previous content

## 3. Documentation

- [x] 3.1 Add a section on `backup-and-sqlite` to `README.md`: the command, the layout of the directory (`json/`, `todoist.db`, `todoist.metadata.json`, `.lock`), `--database`, what happens when one step fails, the exit status, and how to move an existing backup into `json/` on Linux/macOS and Windows; switch the scheduling examples (cron, launchd, Task Scheduler) to `backup-and-sqlite`, and mention that `backup` plus `sqlite` remain for other layouts. Verify that every path, option and command in the section matches the spec and that the commands shown run as written in a scratch directory with the fake API's test setup or by reading them against `--help`

## 4. Integration

- [x] 4.1 Run `todoist-takeout backup-and-sqlite` once by hand on a scratch directory against a recorded-fixture setup or, if the user agrees, against the Free test account, then open the result with `uvx datasette <dir>/todoist.db -m <dir>/todoist.metadata.json`. Verify that the database shows the backup's data and that a second run reports `0 objects written` and rebuilds the database
- [x] 4.2 Run `uv run ruff format .`, `uv run ruff check .` and `uv run pytest`, and fix what they report. Verify that all three finish without findings or failures

## 5. Metadata file for Datasette's directory mode

- [x] 5.1 Add `METADATA_FILE = "metadata.json"` to `backup_and_sqlite.py` and an optional `metadata` parameter to `sqlite_view.run`; without `--database` the run checks and writes `<dir>/metadata.json`, with it `<stem>.metadata.json` next to the database (design D7). Verify with tests for "Directory layout" (paths after a run, a foreign `metadata.json` refused before any request) and "Database path option" (no `metadata.json` in the directory with `--database`), and that `tests/test_sqlite_view.py` still passes unchanged
- [x] 5.2 Print the Datasette hint as `datasette <dir>` without `--database` and as `datasette <database> -m <metadata file>` with it (design D5, D7). Verify with tests for "Diagnostic output and exit status", including a path with spaces
- [x] 5.3 Update the `backup-and-sqlite` section of `README.md`: `metadata.json` in the layout table, `uvx datasette ~/Backups/todoist` as the view command with a sentence on Datasette's directory mode, and the `-m` form for `--database`. Verify every path and command against the spec and `--help`
- [x] 5.4 Run `backup-and-sqlite` by hand on a scratch directory with the recorded-fixture setup and open it with `uvx datasette <dir>`; then run `uv run ruff format .`, `uv run ruff check .` and `uv run pytest`. Verify that Datasette shows the title "Todoist backup" from the metadata and only the database `todoist`, and that the three checks finish without findings
