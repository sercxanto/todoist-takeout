# Viewing the backup

How to turn the backup into a SQLite database and browse it in [Datasette](https://datasette.io), with filters, facets and full-text search. For installation and a quick start, see the [README](../README.md); for the backup itself, see [Backing up](backup.md).

There are two ways:

- [`backup-and-sqlite`](#backup-and-database-in-one-directory) runs a backup and rebuilds the database in one directory with a fixed layout. Use it unless you need the backup and the database in unrelated places.
- [`sqlite`](#the-sqlite-command) builds the database from an existing backup.

Datasette is not part of todoist-takeout; `uvx` runs it without installing it, and `pipx run datasette` works as well. Open the address that Datasette prints, usually `http://127.0.0.1:8001`.

## Backup and database in one directory

The `backup-and-sqlite` command runs a backup and then builds the SQLite database from it, in one directory:

```sh
todoist-takeout backup-and-sqlite ~/Backups/todoist
# the same:
python -m todoist_takeout backup-and-sqlite ~/Backups/todoist
# view the result:
uvx datasette ~/Backups/todoist
```

The directory has a fixed layout, which keeps the database out of the directory that holds the JSON files:

| Path | Holds |
|---|---|
| `json/` | The backup, in the same format as one written by `backup`. `todoist-takeout backup ~/Backups/todoist/json` continues it. |
| `todoist.db` | The database, as `todoist-takeout sqlite ~/Backups/todoist/json ~/Backups/todoist/todoist.db` builds it. |
| `metadata.json` | The Datasette metadata file of the database. |
| `.lock` | The lock that lets only one `backup-and-sqlite` run at a time use the directory. |

`uvx datasette ~/Backups/todoist` uses Datasette's configuration directory mode: it opens the database files at the top level of the directory, here `todoist.db`, and loads `metadata.json` from there, so no `-m` is needed. It does not look into `json/`. A `.db` file or Datasette configuration (`settings.json`, `templates/`, `plugins/`) that you add to the directory is picked up as well.

The directory and `json/` are created if they do not exist. The command refuses a directory that is not empty and has no `json/` subdirectory, and a `json/` that is not a todoist-takeout backup. Every check that needs no request to Todoist, including those of the database and its metadata file, runs before the backup starts, so a configuration error never costs a backup run.

`--database PATH` builds the database at `PATH` instead of `todoist.db`, with its metadata file next to it, named as by `sqlite`: `--database ~/todoist.db` writes `~/todoist.db` and `~/todoist.metadata.json`, and no `metadata.json` in the directory; open it with `uvx datasette ~/todoist.db -m ~/todoist.metadata.json`. Any path that `sqlite` accepts works, also one inside `json/`.

The database is rebuilt only after a complete backup, and after every one, also when nothing changed. If the backup fails, the database is not rebuilt and keeps its previous content. If the rebuild fails, for example because another program keeps the database locked, the backup stays complete, and the next run rebuilds the database. An interrupted run is simply started again, as for `backup`.

The command takes `-q`/`--quiet`, `-v`/`--verbose` and `--max-rate-limit-wait` like [`backup`](backup.md#options), and reads the [token](backup.md#token) like `backup`. A successful run ends with the summary line of the backup, the command to open the database in Datasette, and the summary line of the database, which counts the warnings of the whole run:

```
INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning
INFO view it with: datasette /home/me/Backups/todoist
INFO database complete: 24 projects, 9 sections, 7 labels, 113 tasks, 41 task labels, 15 comments, 340 completions, 1 warning
```

It exits with 0 when the backup and the database are complete, possibly with warnings; 1 when the run failed after it started, for example because of the network, because another run holds the directory or `json/`, or because the rebuild failed; 2 for usage or configuration errors, which change nothing.

### Moving an existing backup into the layout

A directory that holds a backup itself, with `state.json` at its top, is refused. Move the backup into a subdirectory `json`, while no backup runs, on Linux and macOS with:

```sh
mv ~/Backups/todoist ~/Backups/todoist-json
mkdir ~/Backups/todoist
mv ~/Backups/todoist-json ~/Backups/todoist/json
```

and on Windows, in PowerShell, with:

```powershell
Rename-Item "$env:USERPROFILE\Backups\todoist" todoist-json
New-Item -ItemType Directory "$env:USERPROFILE\Backups\todoist" | Out-Null
Move-Item "$env:USERPROFILE\Backups\todoist-json" "$env:USERPROFILE\Backups\todoist\json"
```

The backup holds no absolute paths, so it works unchanged at its new place. Keep an existing database where it is with `--database`, or move it into the directory as `todoist.db`; the next run writes its metadata file as `metadata.json`, so delete the old `.metadata.json` file.

`backup` and `sqlite` remain for other layouts, such as a backup and a database in unrelated places.

## The sqlite command

The `sqlite` command builds a SQLite database from a backup:

```sh
todoist-takeout sqlite ~/Backups/todoist ~/todoist.db
uvx datasette ~/todoist.db -m ~/todoist.metadata.json
```

The command works offline, needs no token, and never changes the backup. It holds the backup's lock while it reads, so it stops with "another run is in progress" while a backup is running, and the other way round. Next to the database it writes the Datasette metadata file, named after the database with `.metadata.json` in place of its suffix, which describes every table, view and column. Both arguments are required. The command refuses to overwrite a file that is not a database or metadata file it wrote itself.

Every run rebuilds the whole database from the backup, so run it again after each backup, or use [`backup-and-sqlite`](#backup-and-database-in-one-directory), which does both. A running Datasette shows the new content on its next page load; do not start it with `-i` (immutable mode), which assumes the file never changes. If another program keeps the database locked for more than 10 seconds, the run fails and the database keeps its previous content.

`sqlite` takes `-q`/`--quiet` and `-v`/`--verbose` like `backup`. A successful run ends with a summary line such as:

```
INFO database complete: 24 projects, 9 sections, 7 labels, 113 tasks, 41 task labels, 15 comments, 340 completions, 0 warnings
```

It exits with 0 when the database is complete, possibly with warnings; 1 when the build failed after it started, for example because a backup run holds the backup directory or another program keeps the database locked, and the database keeps its previous content; 2 for usage or configuration errors, such as a directory that is not a backup, or a database or metadata file path that holds a file the tool did not write.

## What the database contains

The database is a simplified view for browsing, not a copy of the backup: each object appears once, from its current copy; deleted objects are left out; reminders, filters, collaborators, workspaces, settings, view options and statistics are left out. Its tables:

| Table | Holds |
|---|---|
| `projects` | Every project, archived ones included: name, description, parent project, archived, favorite, color, creation and update times. |
| `sections` | Every section, also of archived projects: name, project, archived, creation and archive times. |
| `labels` | The personal labels: name, color, favorite. |
| `tasks` | Every task, open and completed: name, description, project, section, parent task, status (`open` or `completed`), priority (`p1` to `p4` as Todoist shows it), due date and whether it recurs, deadline, creation, completion and update times. |
| `task_labels` | One row per label of a task, with a link to the personal label of that name, if there is one. |
| `comments` | Every comment of a task or project: text, time, the name of an attached file and its path in the backup (if downloaded), or the URL of an attached link. |
| `completions` | One row per completion, every completion of a recurring task included, also of tasks deleted since: the task, its name and project at the time, the completion time, and whether it comes from the activity log or from before the first backup. |

Three views combine the tables for everyday questions, with names in place of IDs:

| View | Shows |
|---|---|
| `open_tasks` | The open tasks, without those of archived projects, by due date, then priority: ID and name, project, section and label names, priority, due date and whether it recurs, deadline, creation time, and whether the task is overdue. |
| `done_log` | One row per completion, newest first: every row of `completions`, plus each completed task that no completion record covers: the task, its name, project ID and name, the completion time in UTC and in local time, its source (`activity`, `backfill` or `task`), and whether the task is still in `tasks`. |
| `recurring_stats` | One row per recurring task: ID, name, status, project name and due date as entered, the number of its completions, the first and last one, the average number of days between them, and the days since the last one. |

Whether a task is overdue, and the days since the last completion, are computed each time a view is queried, so they stay current between rebuilds. They use the local time of the computer that runs the query, which is the one running Datasette. A task due today without a time counts as overdue from tomorrow, as in Todoist.

Full-text search covers the names and descriptions of tasks and the text of comments. The schema may change with any version of the tool, as the database is always rebuilt; for migration scripts, read the JSON backup, whose [format](../src/todoist_takeout/storage_format.md) is documented and versioned.
