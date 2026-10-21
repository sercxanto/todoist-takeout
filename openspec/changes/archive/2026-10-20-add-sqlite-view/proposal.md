# Proposal

> Status: the scope was settled with the user in an explore session (2026-10-20).

## Why

The backup is complete and documented, but it is hundreds of JSON files in which the same task can be stored up to four times, deleted objects stay next to live ones, and the completions of recurring tasks are in separate event files. To look something up, a user has to know the format. A SQLite database derived from the backup, opened in [Datasette](https://datasette.io), lets them browse, filter and search their Todoist history in a web browser. This is roadmap item 5; with attachment downloads done, the backup holds everything such a view needs.

## What Changes

- A new command `todoist-takeout sqlite BACKUP DB` builds a SQLite database at `DB` from the backup in `BACKUP`. Both arguments are required; there is no default location for the database.
- The command works offline: it needs no token and makes no request. It only reads the backup, and holds the backup's lock while it reads, so it never sees a backup that a run is changing.
- The database is a simplified view for display, not a copy of the API's data model: one row per project, section, label, task and comment, each from its current copy; deleted objects are left out; resources that matter only to Todoist's apps (reminders, filters, collaborators, workspaces, settings, view options, statistics) are left out.
- Tables: `projects` (including archived ones), `sections`, `labels`, `tasks` (open and completed, with a status column), `task_labels`, `comments` (of tasks and projects, with the path of the attachment file in the backup) and `completions` (one row per completion, including every completion of a recurring task and completions of tasks deleted since). Foreign keys connect them, so Datasette shows names and links instead of IDs.
- Every run rebuilds the database's whole content in one SQLite transaction, in the existing file. A reader such as a running Datasette sees either the old or the new content, and sees the new content without a restart, on every platform.
- The command refuses to overwrite a file that is not a database it built.
- Next to the database, the command writes a Datasette metadata file whose name is derived from the database's (`todoist.db` gives `todoist.metadata.json`), with titles, descriptions of the tables and columns, facets, sort orders and full-text search, so that `datasette todoist.db -m todoist.metadata.json` shows a ready-made view.
- The database schema is versioned and briefly documented. It may change with any version of the tool, as the database is always rebuilt from the backup; the JSON backup stays the source of truth and is never changed by this command.

## Capabilities

### New Capabilities

- `sqlite-view`: the `sqlite` command, how it reads the backup, which tables and columns the database holds and which copy of an object they show, how the database is rebuilt and protected, and the Datasette metadata file.

### Modified Capabilities

None. The backup itself, its format and the `backup` command do not change. The new command takes the lock that the Single run per target directory requirement of `backup-cli` already describes, so a backup started while the database is built reports that another run is in progress.

## Impact

- New module for building the database, using `sqlite3` from the standard library; no new runtime dependency. Datasette is not a dependency: the user runs it separately, for example with `uvx datasette`.
- `cli.py`: a second subcommand; the token check moves into the `backup` command.
- Reads the backup through the existing helpers of `storage.py`, `sync.py`, `completed.py`, `archived.py` and `attachments.py`; the rule for the current copy of a task is the one `storage_format.md` documents.
- Documentation: a usage section and roadmap item 5 in the project `README.md`, a short description of the database schema, and the lock-file paragraph of `storage_format.md`, which says that the `sqlite` command holds the lock too.
- Tests run offline against backups built from the existing fixtures; the cross-platform CI matrix covers the in-place rebuild while another connection has the database open.
