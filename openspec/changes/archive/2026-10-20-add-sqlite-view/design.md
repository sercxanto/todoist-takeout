# Design

## Context

The backup format is described in `src/todoist_takeout/storage_format.md`; the requirements of the new command are in `specs/sqlite-view/spec.md`. The code it builds on:

- `cli.py`: one subcommand, `backup`. `main` reads the token for every command, sets up logging to stderr (`_Logging`, which also counts warnings), and maps `TakeoutError` subclasses to exit codes (sync D3).
- `lock.py`: `TargetLock` opens `<target>/.lock` with `O_RDWR | O_CREAT` and takes an exclusive, non-blocking lock; it raises `LockHeld` or `StorageError`.
- `storage.py`: `read_state` (raises `ConfigError` for a missing format version or a newer one), `decode_key`, `encode_key`, `write_bytes_atomic`, `encode_json`, `is_temp_name`.
- `sync.py`: `SYNC_DIR`, `DELETED_SUFFIX`; an object counts as deleted if `<key>.json` has `is_deleted: true` or `<key>.deleted.json` exists.
- `completed.py`: `read_stored` for response-shaped files, `parse_time`, `_id_text`; `archived.py`: the snapshot file names; `attachments.py`: `uploaded_file(comment)` and the layout `attachments/<encoded comment id>/<file>`.
- The recorded fixtures show the field names used below: tasks (`content`, `description`, `project_id`, `section_id`, `parent_id`, `checked`, `priority`, `due`, `deadline`, `labels` as names, `added_at`, `completed_at`, `updated_at`), projects (`name`, `parent_id`, `is_archived`, `created_at`, `updated_at`), sections (`name`, `project_id`, `is_archived`, `archived_at`), comments (`item_id` or `project_id`, `content`, `posted_at`, `file_attachment`), activity events (`id` above 64 bits, `object_id`, `event_type`, `event_date`, `parent_project_id`, `extra_data.content`) and backfill entries (`id`, `task_id`, `completed_at`, `content`, `project_id`).
- Python's `sqlite3` is in the standard library. Datasette's stable release is 0.65.5; 1.0 is in alpha (1.0a41). Its documentation keeps table options (facets, sort, label column, full-text search) readable from the metadata file "for backwards compatibility", next to the new `datasette.yaml`.

## Goals / Non-Goals

**Goals:**

- A command that reads only local files, runs in seconds on a large backup, and gives the same database for the same backup on every platform.
- A schema small enough to explain in the metadata file's descriptions.
- One place where the copy rules live in code, next to the format description that defines them.

**Non-Goals:**

- A stable interface for migration scripts. Those read the JSON backup, whose format is documented and versioned.
- Incremental updates of the database. Every run rebuilds it.
- Reminders, filters, collaborators, workspaces, user settings, view options, statistics, task durations, assignees and reactions.
- Canned queries in the metadata file. Datasette 1.0 moves them to `datasette.yaml`, and facets cover the common questions.
- Starting or bundling Datasette.

## Decisions

### D1: Module and command

A new module `sqlite_view.py` holds the whole command: reading the backup, choosing the copies, building the database, writing the metadata file. `cli.py` gets the `sqlite` subcommand with the positional arguments `BACKUP` and `DATABASE` and the same `--quiet`/`--verbose` group as `backup`. The token check moves from `main` into the `backup` branch, so `sqlite` never reads `TODOIST_API_TOKEN`; `_Logging` gets an empty token there.

The module is named after what it builds, not after Datasette, matching the command name.

### D2: Reading the backup

```
sqlite BACKUP DATABASE
  1. check     BACKUP is a directory with a valid state.json (read_state)  -> ConfigError (2)
               DATABASE and its metadata file may be used (D7, D8)        -> ConfigError (2)
  2. lock      TargetLock(BACKUP), or no lock on a read-only file system (below)
  3. read      collect the candidate copies of every object (D3), in memory
  4. unlock
  5. build     one transaction in DATABASE (D6)
  6. metadata  write <stem>.metadata.json atomically (D8)
  7. summary   INFO line with row counts and warnings (D9)
```

The command does not call `prepare_target`, `load_or_create_state` or `remove_temp_files`, which would write into the backup. Files whose names start with `.` (temporary files, `.lock`) are skipped while reading, as `attachments._json_files` already does. `pending/` is not read: the database shows what the last applied sync stored.

The lock is held only while reading, not while building, so a backup can start as soon as the data is in memory. `TargetLock` gets a way to tell "the file system is read-only" (`EROFS`, and `EACCES`/`EPERM` when opening `.lock`) apart from other storage errors; in that case the command logs a warning and reads without the lock, as no run can be writing to such a backup.

*Alternative:* no lock, relying on atomic file writes. Rejected: a task can move from `sync/items/` to `completed/` during a run, and a reader without the lock could see it in neither or in an inconsistent mix of places.

### D3: Rows are built in Python, not as SQL views over raw JSON

Every stored file is parsed with Python's `json`, which keeps integers of any size, and turned into rows of plain columns. The tables hold no raw JSON.

*Alternative:* a table of raw payloads with views that use `json_extract`. Rejected: SQLite's JSON functions return integers above 64 bits as `REAL`, so the activity events' IDs would be corrupted (`storage_format.md`, Large integers); the copy and deletion rules would need window functions in every view; and Datasette is slower on views and cannot show a view's foreign keys as links.

### D4: Copies and deletions

Each object type collects candidate copies with a place rank, then keeps one per `id`:

| Table | Places, in rank order (best first on ties) | Rule |
|---|---|---|
| `tasks` | `archived/*/completed.json`, `archived/*/full.json` `tasks`, `completed/<YYYY-MM>.json`, `sync/items/` | newest `updated_at`, then rank |
| `projects` | `archived/*/project.json`, `archived/*/full.json` `subprojects`, `sync/projects/` | newest `updated_at`, then rank |
| `sections` | `archived/*/full.json` `sections`, `sync/sections/` | newest `updated_at`, then rank |
| `comments` | `archived/*/comments/*`, `archived/*/project_comments.json`, `completed/comments/*`, `sync/notes/`, `sync/project_notes/` | rank only |
| `labels` | `sync/labels/` | one copy |

The task rule is the one `storage_format.md` documents. Two snapshots of the same task in different archived projects (a task moved between projects that were both archived later) rank equally; then the snapshot whose `project.json` is newer wins, and on a tie the one whose directory name sorts last, so the result is deterministic. A missing or invalid `updated_at` counts as older than any valid one. Times are compared as instants (`completed.parse_time`), not as text. Within `completed/`, a task in two months' files (completed, uncompleted, completed again) is ordered by `updated_at` like any other copy.

Projects and sections follow the task rule, which the format description does not state for them. Comments have no `updated_at`; the rank is the order `storage_format.md` gives for comments ("the copy under `archived/` is current") extended by `sync/` last, as for tasks.

**Deletions.** While reading `sync/`, the IDs of objects that count as deleted are collected per resource (`items`, `projects`, `sections`, `labels`, `notes`, `project_notes`). Every candidate whose `id` is in the deleted set of its type is dropped, whatever its place. A deletion payload is never used as a copy. Completions are not filtered (spec, Completions table).

### D5: Schema

All IDs are `TEXT`. Times are `TEXT` as the backup holds them (UTC ISO 8601 for timestamps; `due_date` and `deadline_date` are Todoist's floating dates or local date-times). Booleans are `INTEGER` 0 or 1. Every table has a `TEXT PRIMARY KEY` except `task_labels`, whose key is `(task_id, label_name)`.

| Table | Columns |
|---|---|
| `projects` | `id`, `name`, `description`, `parent_id` -> projects, `is_archived`, `is_favorite`, `color`, `created_at`, `updated_at` |
| `sections` | `id`, `name`, `project_id` -> projects, `is_archived`, `added_at`, `archived_at` |
| `labels` | `id`, `name`, `color`, `is_favorite` |
| `tasks` | `id`, `content`, `description`, `project_id` -> projects, `section_id` -> sections, `parent_id` -> tasks, `status` (`open`/`completed` from `checked`), `priority` (`p1`..`p4`, from the API's 4..1), `due_date`, `due_string`, `is_recurring`, `deadline_date`, `added_at`, `completed_at`, `updated_at` |
| `task_labels` | `task_id` -> tasks, `label_name`, `label_id` -> labels (the label with that name, or NULL) |
| `comments` | `id`, `task_id` -> tasks, `project_id` -> projects, `content`, `posted_at`, `attachment_name`, `attachment_path`, `attachment_url` |
| `completions` | `id`, `task_id` -> tasks, `content`, `project_id` -> projects, `completed_at`, `source` (`activity`/`backfill`) |

- **Foreign keys** are declared, so Datasette links them, but `PRAGMA foreign_keys` stays off: a reference to a deleted or missing row keeps its ID.
- **`task_labels`** links to labels by `label_id` and not by name: Datasette links a foreign key to the referenced row's page by primary key, and a name need not be unique among stored labels.
- **`comments`**: `task_id` comes from `item_id` (or `task_id`, if a later API version renames it). `attachment_name` is `file_attachment.file_name`. `attachment_path` is `attachments/<encoded id>/<file>`, with `/` on every platform, when that directory holds a file other than a temporary file. `attachment_url` is `file_attachment.url` for a link (`resource_type` `url`).
- **`completions`**: activity events with `event_type` `item:completed` give `id`, `object_id`, `extra_data.content`, `parent_project_id` and `event_date`; backfill entries give `id`, `task_id`, `content` (or `item_object.content`), `project_id` and `completed_at`. An event ID that Python parsed as an integer is written with `str()`.
- Rows are inserted in order of `id`, so that the same backup gives the same database file content apart from SQLite's page layout.
- The schema version is `PRAGMA user_version` (1 for this change).

### D6: Rebuild in one transaction, in place

```python
con = sqlite3.connect(path, isolation_level=None, timeout=10)  # busy timeout
con.execute("BEGIN IMMEDIATE")
#   drop every table, view, index and trigger in sqlite_master (virtual tables drop their shadow tables)
#   create the schema, insert the rows, fill the FTS tables (D10)
#   PRAGMA application_id = ...; PRAGMA user_version = 1
con.execute("COMMIT")
```

SQLite's DDL is transactional, so dropping and recreating the schema is part of the same atomic commit. The default rollback journal (`journal_mode=DELETE`) is kept: it leaves no extra files next to the database after the commit and works on network file systems. A new database is created with `PRAGMA auto_vacuum = FULL` before its first table, so the file shrinks again after a rebuild with fewer rows; no `VACUUM` is needed. For a new (missing or empty) file the transaction therefore starts with a deferred `BEGIN` and that pragma as its first statement: inside `BEGIN IMMEDIATE` SQLite ignores it, and outside a transaction it writes a header without the `application_id` at once, which a failed first run would leave behind for D7 to refuse. SQLite's own tables (`sqlite_sequence`, `sqlite_stat1`) are not dropped, which SQLite does not allow for all of them; dropping the tables empties their rows.

`isolation_level=None` makes the Python module leave transaction control to the explicit statements, which behaves the same on Python 3.11 to 3.13. The busy timeout of 10 seconds covers a reader that is in the middle of a query; a reader that keeps a transaction open beyond that fails the run with `StorageError` (exit 1), and the rollback leaves the previous content.

*Alternative:* write a new file next to it and replace it atomically, as the backup does. Rejected: on Windows, the replace fails while Datasette has the file open, and on Linux and macOS Datasette keeps reading the replaced file until it is restarted.

*Alternative:* WAL mode, so that readers never block the writer. Rejected: it adds `-wal` and `-shm` files, does not work on network file systems, and the rebuild takes seconds, which the busy timeout covers.

### D7: Recognising the tool's database

`PRAGMA application_id` is set to a fixed value (the ASCII bytes `TdTk`, `0x5464546B`). Before connecting, the command looks at the database path:

| Found | Result |
|---|---|
| nothing | create the file, and missing parent directories |
| an empty file | use it as a new database |
| a file starting with the SQLite header whose `application_id` (bytes 68-71 of the header) is `TdTk` | rebuild it |
| a directory, any other file, or a SQLite database with another `application_id` | `ConfigError` (exit 2), nothing changed |

Reading the 100-byte header directly avoids opening a foreign file with `sqlite3`, which could roll back a hot journal and so change it. `user_version` is not checked: any version of the schema is replaced (spec, Older schema).

### D8: Datasette metadata file

The file is JSON, written with `storage.encode_json` and `write_bytes_atomic`, because Datasette's `-m` reads JSON as well as YAML and the standard library writes no YAML. Its path is the database path with the last suffix replaced (`Path.with_suffix(".metadata.json")`, which also appends to a name without a suffix).

```json
{
  "title": "Todoist backup",
  "description": "Built by todoist-takeout from <backup> (last backup run <time>).",
  "source": "todoist-takeout",
  "source_url": "https://github.com/sercxanto/todoist-takeout",
  "databases": {
    "<database file stem>": {
      "tables": {
        "tasks": {
          "description": "...",
          "columns": {"status": "open or completed", "...": "..."},
          "facets": ["status", "project_id", "priority", "is_recurring"],
          "sort_desc": "updated_at",
          "label_column": "content",
          "fts_table": "tasks_fts"
        }
      }
    }
  }
}
```

Facets: `tasks` as above; `completions` by `source` and `project_id`, sorted by `completed_at` descending; `comments` sorted by `posted_at` descending. Label columns: `name` for projects, sections and labels, `content` for tasks. Every table and column has a description; these descriptions are the schema documentation (D11). The database key is the file stem, which is the name Datasette gives the database.

`"source": "todoist-takeout"` marks the file as the tool's own: an existing metadata file is overwritten only if it is a JSON object with that `source`; anything else is `ConfigError` (exit 2), checked in step 1 of D2, before the database is touched. The file is written after the commit. If writing it fails, the run fails with exit 1 and the next run writes it again; the database is already complete.

The table options are the ones Datasette 0.65 reads from metadata and that 1.0 still accepts there. The automated test runs against 0.65 (D12); compatibility with the current 1.0 alpha is checked once by hand during implementation.

### D9: Messages and exit status

- `INFO reading the backup in <backup>`, `INFO building <database>`, warnings as they occur (read-only backup, no full-text search), then
  `INFO view it with: datasette <database> -m <metadata file>` (a path with a space in double quotes) and, last, as the spec requires,
  `INFO database complete: 24 projects, 9 sections, 7 labels, 113 tasks, 41 task labels, 15 comments, 340 completions, 0 warnings`.
- Exit 2: argparse errors and `ConfigError` (not a backup, newer format, unusable database or metadata path). Exit 1: `LockHeld`, `StorageError`, and `sqlite3.Error` wrapped as `StorageError` with the database path. Exit 130 on interrupt, as for `backup`, with a message that the database was left unchanged.

### D10: Full-text search

FTS5 tables with external content: `tasks_fts(content, description)` over `tasks` and `comments_fts(content)` over `comments`, filled with `INSERT INTO ..._fts(rowid, ...) SELECT rowid, ... FROM ...` inside the same transaction. Datasette finds them through `fts_table` in the metadata file and matches by `rowid`. If `CREATE VIRTUAL TABLE ... USING fts5` raises `sqlite3.OperationalError` (no such module), the command warns once, builds the rest, and leaves `fts_table` out of the metadata file. The python.org builds for Windows and macOS and the usual Linux builds include FTS5.

### D11: Documentation

- `README.md`: a section "Viewing the backup" with the command, the Datasette call (`uvx datasette todoist.db -m todoist.metadata.json`), the list of tables in one sentence each, a note not to use Datasette's immutable mode (`-i`), which assumes the file never changes, and roadmap item 5 marked implemented.
- The schema itself is documented by the descriptions in the metadata file, generated from the same table definitions as the schema, so the two cannot drift apart. A short table of tables and columns goes into the README section.
- `storage_format.md`, Lock file: the `sqlite` command holds the lock too, while it reads.
- `CONTRIBUTING.md`, Key decisions: the derived SQLite database now exists; the JSON stays the source of truth.

### D12: Tests

Offline, in a new `tests/test_sqlite_view.py`:

- Backups are built in `tmp_path` by running `run_backup` against the existing fake API and fixtures, where a test needs a realistic tree, and by writing small hand-made files where a test needs one specific case (copy ranks, tombstones, large event IDs, shared labels, attachments present and missing).
- One test per scenario of the spec. The interrupted build is simulated by raising inside the transaction after some inserts; the locked database by a second connection that holds `BEGIN IMMEDIATE`, with the busy timeout made short through a module constant.
- The open-database scenario uses a second `sqlite3` connection that queries before and after the rebuild; the CI matrix runs it on Windows, macOS and Linux.
- One test loads the database and the metadata file into Datasette in-process (`Datasette(files=[...], metadata=...)`, its `client.get`) and checks that the facets, label columns and search are active. `datasette>=0.65.5,<0.66` joins the `dev` dependency group; it is a test-only dependency.
- The CLI tests check the argument handling, that `sqlite` runs without `TODOIST_API_TOKEN`, and the exit codes.

### D13: Cross-platform

- No file is replaced while open: the database is changed in place, and the metadata file, which Datasette reads only at start, is replaced by `write_bytes_atomic`, which already retries sharing violations on Windows.
- The lock is the existing `TargetLock` (`msvcrt` on Windows, `flock` elsewhere).
- Paths stored in the database use `/`, so the database content is the same on every platform. Database and backup paths with spaces and non-ASCII characters are covered by a test.

## Risks / Trade-offs

- [Datasette 1.0 drops table options from the metadata file] -> The test pins 0.65; when 1.0 is released, the tool can write a `datasette.yaml` next to the metadata file. That changes only the metadata requirement. Checked by hand on 2026-10-20 with a database built from a real backup, in Datasette 0.65.5 and 1.0a41: both read the metadata file without a visible problem. The check was a look over the pages, not of every option; no `datasette.yaml` is written for now.
- [The copy rule for projects and sections is not in the format description] -> It is the task rule applied to them; the metadata file's table descriptions state it.
- [The read-only fallback reads without the lock] -> Only when `.lock` cannot be opened for writing, where no backup run can be writing either; it warns.
- [A deletion the backup never saw] -> A task completed before the first backup and deleted later is still shown, because only `sync/` records deletions. The database cannot know more than the backup.
- [Memory] -> All rows are held in memory before the transaction; tens of thousands of tasks take tens of megabytes, which the backup's own runs already need.
- [Datasette started with `-i`] -> Immutable mode can show wrong results after a rebuild; the README says not to use it.
- [A reader holds a transaction for a long time] -> The run fails after the busy timeout with exit 1 and the old content; running it again succeeds once the reader is done.

## Migration Plan

None. The command is new and the backup format does not change. Removing the feature means deleting the database and its metadata file.
