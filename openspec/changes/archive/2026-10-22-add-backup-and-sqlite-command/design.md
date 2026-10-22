# Design

## Context

See proposal.md for the motivation and `specs/backup-and-sqlite/spec.md` for the requirements. The code it builds on:

- `cli.main` parses the arguments, reads the token only for `backup`, maps `TakeoutError` to its exit code, and prints the summary lines. `ClientFactory` lets tests inject the fake API.
- `sync.run_backup(target, client)` calls `prepare_target` (creates the directory and `state.json`, refuses foreign directories with `ConfigError`), then holds `TargetLock(target)` for the run and returns `ApplyStats`.
- `sqlite_view.run(backup, database)` calls `check_backup`, `check_database` and `check_metadata`, holds the backup's lock only while it reads, then rebuilds the database in one transaction and writes the metadata file atomically. It returns `Built`.
- `TargetLock` is a non-blocking `flock` (POSIX) or `msvcrt.locking` (Windows) on `<dir>/.lock`. A second `TargetLock` on the same file in the same process conflicts with the first on both platforms, so the backup's lock cannot be held across both steps without changing `run_backup` and `sqlite_view.run`.
- Neither the backup nor `sqlite` enumerates the top level of the backup directory except `prepare_target` (only before `state.json` exists) and `remove_temp_files` (only `.*.tmp`), so a database inside `json/` (the `--database` case) does not disturb them; `sqlite` already allows it today.

## Goals / Non-Goals

**Goals:**

- `backup-and-sqlite` is a thin composition of `run_backup` and `sqlite_view.run`; neither changes behavior for `backup` or `sqlite`.
- Every configuration error is found before the first API request.

**Non-Goals:**

- Overriding the name of `json/` or pointing `backup-and-sqlite` at an existing backup directory elsewhere; `backup` plus `sqlite` cover that.
- Migrating an existing backup into the layout automatically; the error message says how to do it by hand.
- A configuration file.

## Decisions

### D1: Module and command

A new module `backup_and_sqlite.py` with `run(directory, client, database=None) -> Outcome`, where `Outcome` holds the `ApplyStats` and the `Built`. The constants `BACKUP_DIR = "json"` and `DATABASE_FILE = "todoist.db"` live there. `cli.py` adds the subparser `backup-and-sqlite` with the positional `TARGET`, `--database PATH`, the verbosity group and `--max-rate-limit-wait` (factored out of the `backup` parser into a helper, as `_add_verbosity` already is). The token is read when the command is `backup` or `backup-and-sqlite`.

*Alternative:* `backup TARGET --sqlite DATABASE`. Rejected by the user in favor of a separate command; it would also mix two summaries and two layouts into one command.

### D2: Order of checks

`backup_and_sqlite.run` does, before any request:

1. `check_directory(directory)`: refuse a path that is not a directory; if it exists and is not empty, require a `json` subdirectory; if it has `state.json` but no `json`, raise `ConfigError` with the hint "it is a backup directory; move its content into a subdirectory json, or use the backup and sqlite commands".
2. Resolve the database path (`--database` or `directory / DATABASE_FILE`) and the metadata file's path (D7), then `sqlite_view.check_database` and `sqlite_view.check_metadata` on that path. Both only read.
3. Take the outer lock (D3).
4. `run_backup(directory / "json", client)`, whose `prepare_target` creates `json/` and refuses a foreign one, and whose `load_or_create_state` refuses a newer format version, all before the first request.
5. `sqlite_view.run(directory / "json", database, metadata)`, which repeats its checks; they pass, unless a file changed in between.

The token check stays in `cli.main`, before `backup_and_sqlite.run`, as for `backup`. The target directory itself is created in step 3, because the lock file lives in it; a directory refused in step 1 or a database refused in step 2 is therefore never created.

*Alternative:* call `sqlite_view.run` only and let it fail after the backup. Rejected: a foreign `todoist.db` would then cost a whole backup run and end with status 1 instead of 2.

### D3: An outer lock for the whole run

`backup_and_sqlite.run` holds `TargetLock(directory)` on `<dir>/.lock` from step 3 to the end. The backup and `sqlite` steps then take `json/.lock` as they do today, one after the other. Without the outer lock, a second `backup-and-sqlite` run could take `json/.lock` between the two steps of the first, and the first would fail its rebuild with "another run is in progress". With it, the second `backup-and-sqlite` stops at once with `LockHeld` (status 1). A plain `backup` run on `json/` between the two steps can still make the rebuild fail; that is the documented behavior of `sqlite` and needs no new rule.

The outer and inner locks are on different files, so they do not conflict in one process on any platform.

*Alternative:* hold `json/.lock` across both steps by passing a held lock into `run_backup` and `sqlite_view.run`. Rejected: it changes two signatures and the lock contract (D11 of the active-data-sync design) for a race that the outer lock already closes for `backup-and-sqlite` runs.

### D4: Failure of one step

`run_backup` raising ends the run before `sqlite_view.run`; the database keeps its content. `sqlite_view.run` raising after a complete backup ends with its own error, which already names the database; `backup_and_sqlite.run` logs `backup complete: ...` at `INFO` before starting the rebuild, so the stderr of a failed rebuild shows that the backup is complete. `KeyboardInterrupt` gets the message "interrupted; the next run resumes from the last checkpoint, and the database keeps its previous content".

### D5: Output

`cli.main` prints, for `backup-and-sqlite`, the backup summary (from inside `backup_and_sqlite.run`, see D4), the Datasette hint (`datasette <dir>` in the default layout, D7; with `--database` the database and metadata paths as for `sqlite`), and the database summary with the warning count of the whole run, reusing the existing format strings. Factoring the two summaries into helpers keeps `backup`, `sqlite` and `backup-and-sqlite` on one wording.

### D7: Name of the metadata file

Unless `--database` is given, the metadata file is `<dir>/metadata.json` (`METADATA_FILE`), not `todoist.metadata.json`. Datasette's configuration directory mode then applies: `datasette <dir>` opens every `*.db`, `*.sqlite` and `*.sqlite3` file at the top level of `<dir>` and loads `metadata.json` (or `.yaml`/`.yml`) from it. Checked with Datasette 0.65.5: the metadata is loaded, `json/` is not scanned, and the database is opened mutable. The mode also reads `settings.json`, `templates/`, `plugins/` and `static/`, and an `inspect-data.json` would make the databases immutable; the tool writes none of them. The metadata is keyed by the database's stem (`todoist`), which is also its name in directory mode.

With `--database`, the metadata file keeps the name `sqlite` gives it (`<stem>.metadata.json` next to the database), because that directory may hold other databases and other files. `sqlite_view.run(backup, database, metadata=None)` takes the path, defaulting to `metadata_file(database)`, so `sqlite` is unchanged. Step 2 of D2 checks the same path, under the same rule: a `metadata.json` the tool did not write is refused.

*Alternatives:* keep `todoist.metadata.json` and `-m`, rejected as one more argument for the common case; `metadata.yaml`, rejected because writing YAML needs a dependency and `sqlite` writes JSON.

### D6: Cross-platform

Paths are joined with `pathlib`; the names `json`, `todoist.db` and `metadata.json` are valid on Windows, macOS and Linux. The outer lock reuses `TargetLock`, the atomic writes and the in-place rebuild are those of `backup` and `sqlite`. On Windows, `msvcrt.locking` on `<dir>/.lock` and on `<dir>/json/.lock` from one process lock different files and do not interfere. Nothing else is platform-specific.

Not checked: the manual CI run on Windows and macOS was left out, as the CI minutes of the private repository are used up. The tests of the outer lock run on Linux only.

## Risks / Trade-offs

- [Existing users keep their backup at `~/Backups/todoist` with `state.json` at the top] → `backup-and-sqlite` refuses it with a hint (D2); moving the content into `json/` is a plain `mv`, since the backup holds no absolute paths. The README describes it.
- [A `backup` run on `json/` between the two steps of a `backup-and-sqlite` run makes the rebuild fail with status 1] → the backup is complete, the next run rebuilds; same behavior as `backup` followed by `sqlite` today.
- [`--database` inside `json/` puts derived files among the JSON files] → only on explicit request; the backup ignores them, and the files are excluded from nothing, so a copy of `json/` also copies the database.
- [A later Datasette version stops loading `metadata.json` in directory mode] → `datasette <dir>/todoist.db -m <dir>/metadata.json` still works; the README shows both forms.
- [The checks of step 2 and the rebuild of step 5 are apart in time] → a file put at the database path during the backup is still refused by `sqlite_view.run`, with status 2 after a complete backup; acceptable for a race the user causes.

## Migration Plan

No migration of data. Users who want `backup-and-sqlite` move their backup into `<dir>/json/` and, if they want, their database to `<dir>/todoist.db`; the README shows the commands for Linux/macOS and for Windows. `backup` and `sqlite` keep working on the old paths.
