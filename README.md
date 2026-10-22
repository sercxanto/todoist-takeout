# todoist-takeout

A command-line tool that keeps a complete, long-term readable local backup of a [Todoist](https://www.todoist.com) account.

> **Status:** not released yet. It backs up active data (projects, sections, labels, tasks, comments and the other resources of the sync API), completed tasks, with every completion of recurring tasks and the comments of completed tasks, archived projects, and the files uploaded to comments, and builds a SQLite database for browsing the backup in Datasette.

## Why

Todoist is a cloud service, so your data is only reachable through it. If the service shuts down, or you lose access to your account for any other reason, the data is gone. todoist-takeout keeps a local copy that:

- stays readable for years, without Todoist and without this tool,
- includes your history, meaning completed tasks and not only the ones still open,
- can serve as the source for migrating to another service.

## Installation

todoist-takeout needs Python 3.11 or newer and has no other runtime dependencies. It is not on PyPI yet, so install it from a checkout of this repository with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .
todoist-takeout --version
```

Without uv, `pipx install .` or `python -m pip install .` work as well.

## Usage

### Token

The tool authenticates with your personal API token, which you find in Todoist under *Settings → Integrations → Developer*. It reads the token only from the environment variable `TODOIST_API_TOKEN`; there is no command-line option for it, and the tool never writes it to its output or into the backup. The token is sent only over HTTPS to Todoist's own hosts; when the file host redirects the download of an attachment file to a storage host, the tool follows the redirect without the token.

```sh
export TODOIST_API_TOKEN=0123456789abcdef0123456789abcdef01234567
```

### Running a backup

```sh
todoist-takeout backup ~/Backups/todoist
# the same:
python -m todoist_takeout backup ~/Backups/todoist
```

The target directory is created if it does not exist. The first run downloads all active data and every completed task since the account was created; later runs download only what changed since the previous run. An interrupted run is simply started again: it resumes from the last checkpoint and does not download data it already stored. The tool refuses to write into a directory that is not empty and is not a todoist-takeout backup. Only one run at a time can use a target directory.

The backup contains a `README.md` that describes its format; the same description is in [src/todoist_takeout/storage_format.md](src/todoist_takeout/storage_format.md).

### What the backup contains

- **Active data** under `sync/`: one file per project, section, label, task, comment and every other object the sync API returns. Objects deleted in Todoist stay, marked as deleted.
- **Completed tasks** under `completed/`, one file per month (UTC). A month is fetched again on every run until 7 days after it ended, then it is frozen: later changes to its tasks, and completions backdated into it, are not captured.
- **One event per completion**, including every completion of a recurring task, which the completed tasks do not show. Completions from the first run on come from the activity log; earlier ones are fetched once, on the first runs.
- **Comments of completed tasks**, with the metadata of their attachments.
- **Archived projects** under `archived/`, one directory per project with its open and completed tasks, its sections, its archived sub-projects, and the comments of the project and of its tasks. A project is fetched once, when it first shows up as archived; later changes to its content are not captured, unless it is unarchived and archived again. A snapshot stays in the backup after the project is unarchived or deleted.
- **Attachment files** under `attachments/`: the file uploaded to each stored comment, in `attachments/<comment id>/` under its original name, cleaned up only as far as Windows, macOS and Linux need. Each file is downloaded once and checked against the size Todoist reports. Attachments that link to other websites are not downloaded. A file the server refuses, for example because its comment was deleted, produces a warning and is tried again on every run.

Not included: sections that were archived before the first backup, because the API cannot list them. The summary line counts the objects under `sync/`; separate `INFO` lines report the completed tasks, the archived projects and the attachment files.

**The same task can be stored more than once,** for example in `sync/items/`, in a month file under `completed/` and in the snapshot of its archived project. All copies share the task's `id`. The current copy is the one with the newest `updated_at`; on equal values, the copy under `archived/` is current, then the one under `completed/`, then the one under `sync/`. The [format description](src/todoist_takeout/storage_format.md#copies-of-the-same-task) lists every place.

**The first run on a large account** makes a few requests per month since the account was created, and one per completed task that has comments. Each archived project adds one request for the project, one per three months since it was created, one per task with comments and one for the project's comments, so an account with 50 archived projects needs a few hundred more; later runs fetch only newly archived projects. The first run can therefore reach the API's rate limit, and stops with exit status 1 once waiting would exceed `--max-rate-limit-wait`. Simply run it again, as often as needed: each run continues where the previous one stopped, and does not fetch the archived projects it already stored. Or allow a longer wait for the first run, for example `--max-rate-limit-wait 3600`.

**Accounts with many attachments.** The first run downloads every file uploaded to the stored comments, one at a time, after all other data; later runs download only new ones. On an account with years of attachments this can take hours and as much disk space as the files take in Todoist, so check the free space of the target first. A run that stops, because of the network or because you cancel it, keeps every file it completed; run it again to download the rest. Files already stored are never downloaded again.

**On the Free plan,** the activity log keeps only 7 days. Run the backup at least weekly; otherwise the completions of recurring tasks in the gap may be lost, and the tool warns about it.

**Todoist's own backups are not downloaded.** On paid plans, Todoist keeps up to 21 daily backups under *Settings → Backups* in the web and desktop apps: ZIP files with one CSV file per active project. They hold nothing that this backup lacks: no completed tasks, no archived projects, at most 500 comments per task, and only links to attachments. They do not exist on the Free plan, and when two-factor authentication is on, the API hands them out only with a one-time code from the authenticator app, which rules out unattended runs. They offer one thing this tool does not: a project's CSV file can be imported into Todoist directly (*Import from CSV*), which brings back a deleted project with its active tasks. If you want that option, download them yourself in the app.

Options of `backup`:

| Option | Meaning |
|---|---|
| `-q`, `--quiet` | Print only warnings and errors. Useful under a scheduler that mails any output. |
| `-v`, `--verbose` | Also print debug messages, and a traceback when the run fails. |
| `--max-rate-limit-wait SECONDS` | Give up when waiting for the API's rate limit would take longer than this in total (default 900). |

All messages go to stderr, one line each, starting with `INFO`, `WARNING` or `ERROR`. A successful run ends with a summary line such as:

```
INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning
```

Features your Todoist plan does not include, such as comments on some plans, produce a warning and never make the run fail.

### Exit status

| Status | Meaning |
|---|---|
| 0 | The backup is complete and up to date, possibly with warnings. |
| 1 | The run failed after it started: network or API error, invalid token, persistent rate limit, a storage error, or another run holds the target directory. The last line on stderr explains why. The backup stays consistent; the next run resumes. |
| 2 | Usage or configuration error, detected before any request: invalid arguments, missing token, a target that is a file or a foreign directory, or a backup written by a newer version of the tool. Nothing was changed. |

### Backup and database in one directory

The `backup-and-sqlite` command runs a backup and then builds the SQLite database for [viewing the backup](#viewing-the-backup) from it, in one directory:

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

The command takes `-q`/`--quiet`, `-v`/`--verbose` and `--max-rate-limit-wait` like `backup`, and reads the token like `backup`. A successful run ends with the summary line of the backup, the command to open the database in Datasette, and the summary line of the database, which counts the warnings of the whole run:

```
INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning
INFO view it with: datasette /home/me/Backups/todoist
INFO database complete: 24 projects, 9 sections, 7 labels, 113 tasks, 41 task labels, 15 comments, 340 completions, 1 warning
```

It exits with 0 when the backup and the database are complete, possibly with warnings; 1 when the run failed after it started, for example because of the network, because another run holds the directory or `json/`, or because the rebuild failed; 2 for usage or configuration errors, which change nothing.

**Moving an existing backup into the layout.** A directory that holds a backup itself, with `state.json` at its top, is refused. Move the backup into a subdirectory `json`, while no backup runs, on Linux and macOS with:

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

### Scheduling

The tool needs no terminal and never asks for input, so it runs unattended. The examples run `backup-and-sqlite`, which keeps the backup and the database current; replace it with `backup` to keep only the backup. Keep the token in a file that only you can read, for example `~/.config/todoist-takeout/token` with mode `600`, and set the variable from it.

**cron** (Linux, macOS), daily at 03:00, with `crontab -e`:

```
0 3 * * * TODOIST_API_TOKEN="$(cat "$HOME/.config/todoist-takeout/token")" "$HOME/.local/bin/todoist-takeout" backup-and-sqlite --quiet "$HOME/Backups/todoist"
```

**launchd** (macOS): save as `~/Library/LaunchAgents/com.example.todoist-takeout.plist`, replace `/Users/me`, and load it with `launchctl load ~/Library/LaunchAgents/com.example.todoist-takeout.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.todoist-takeout</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>TODOIST_API_TOKEN="$(cat /Users/me/.config/todoist-takeout/token)" exec /Users/me/.local/bin/todoist-takeout backup-and-sqlite --quiet /Users/me/Backups/todoist</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardErrorPath</key>
  <string>/Users/me/Library/Logs/todoist-takeout.log</string>
</dict>
</plist>
```

**Task Scheduler** (Windows): save a script such as `C:\Users\me\todoist-takeout.ps1`:

```powershell
$env:TODOIST_API_TOKEN = Get-Content "$env:USERPROFILE\.config\todoist-takeout\token" -Raw
todoist-takeout backup-and-sqlite --quiet "$env:USERPROFILE\Backups\todoist"
exit $LASTEXITCODE
```

and register it to run daily at 03:00:

```powershell
schtasks /Create /SC DAILY /ST 03:00 /TN "todoist-takeout" /TR "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\me\todoist-takeout.ps1"
```

### Viewing the backup

The `sqlite` command builds a SQLite database from a backup, which [Datasette](https://datasette.io) shows in your web browser, with filters, facets and full-text search:

```sh
todoist-takeout sqlite ~/Backups/todoist ~/todoist.db
uvx datasette ~/todoist.db -m ~/todoist.metadata.json
```

Then open the address that Datasette prints, usually http://127.0.0.1:8001. Datasette is not part of todoist-takeout; `uvx` runs it without installing it, and `pipx run datasette` works as well.

The command works offline, needs no token, and never changes the backup. It holds the backup's lock while it reads, so it stops with "another run is in progress" while a backup is running, and the other way round. Next to the database it writes the Datasette metadata file, named after the database with `.metadata.json` in place of its suffix, which describes every table, view and column. Both arguments are required. The command refuses to overwrite a file that is not a database or metadata file it wrote itself.

Every run rebuilds the whole database from the backup, so run it again after each backup, or use [`backup-and-sqlite`](#backup-and-database-in-one-directory), which does both. A running Datasette shows the new content on its next page load; do not start it with `-i` (immutable mode), which assumes the file never changes. If another program keeps the database locked for more than 10 seconds, the run fails and the database keeps its previous content.

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

Full-text search covers the names and descriptions of tasks and the text of comments. The schema may change with any version of the tool, as the database is always rebuilt; for migration scripts, read the JSON backup, whose format is documented and versioned.

`sqlite` takes `-q`/`--quiet` and `-v`/`--verbose` like `backup`. A successful run ends with a summary line such as:

```
INFO database complete: 24 projects, 9 sections, 7 labels, 113 tasks, 41 task labels, 15 comments, 340 completions, 0 warnings
```

It exits with 0 when the database is complete, possibly with warnings; 1 when the build failed after it started, for example because a backup run holds the backup directory or another program keeps the database locked, and the database keeps its previous content; 2 for usage or configuration errors, such as a directory that is not a backup, or a database or metadata file path that holds a file the tool did not write.

### Platform notes

- **macOS certificates:** Python from python.org ships without CA certificates. If runs fail with `cannot verify the TLS certificate`, run *Install Certificates.command* from the Python folder in Applications, or point `SSL_CERT_FILE` at a CA bundle. The tool never turns certificate verification off.
- **Keep the target on a local disk.** The lock that prevents overlapping runs may not work on network file systems. If the target must live on one, make sure runs never overlap. Folders that a sync client such as OneDrive or Dropbox watches work, but a local directory that your regular file backup covers is the better choice.
- **Windows long paths:** file names in the backup stay short (an attachment file name has at most 100 characters), but a deeply nested target directory can exceed Windows' 260-character path limit. Use a short target path, or enable long path support (`LongPathsEnabled` in the registry or group policy).

## Contributing

Setting up a development environment, running the checks and the tests, the requirements and the key design decisions are described in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
