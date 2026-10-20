# todoist-takeout

A command-line tool that keeps a complete, long-term readable local backup of a [Todoist](https://www.todoist.com) account.

> **Status:** early. Backing up active data (projects, sections, labels, tasks, comments and the other resources of the sync API), completed tasks, with every completion of recurring tasks and the comments of completed tasks, archived projects, and the files uploaded to comments works. See the [Roadmap](#roadmap) for what comes next.

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

### Scheduling

The tool needs no terminal and never asks for input, so it runs unattended. Keep the token in a file that only you can read, for example `~/.config/todoist-takeout/token` with mode `600`, and set the variable from it.

**cron** (Linux, macOS), daily at 03:00, with `crontab -e`:

```
0 3 * * * TODOIST_API_TOKEN="$(cat "$HOME/.config/todoist-takeout/token")" "$HOME/.local/bin/todoist-takeout" backup --quiet "$HOME/Backups/todoist"
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
    <string>TODOIST_API_TOKEN="$(cat /Users/me/.config/todoist-takeout/token)" exec /Users/me/.local/bin/todoist-takeout backup --quiet /Users/me/Backups/todoist</string>
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
todoist-takeout backup --quiet "$env:USERPROFILE\Backups\todoist"
exit $LASTEXITCODE
```

and register it to run daily at 03:00:

```powershell
schtasks /Create /SC DAILY /ST 03:00 /TN "todoist-takeout" /TR "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\me\todoist-takeout.ps1"
```

### Platform notes

- **macOS certificates:** Python from python.org ships without CA certificates. If runs fail with `cannot verify the TLS certificate`, run *Install Certificates.command* from the Python folder in Applications, or point `SSL_CERT_FILE` at a CA bundle. The tool never turns certificate verification off.
- **Keep the target on a local disk.** The lock that prevents overlapping runs may not work on network file systems. If the target must live on one, make sure runs never overlap. Folders that a sync client such as OneDrive or Dropbox watches work, but a local directory that your regular file backup covers is the better choice.
- **Windows long paths:** file names in the backup stay short (an attachment file name has at most 100 characters), but a deeply nested target directory can exceed Windows' 260-character path limit. Use a short target path, or enable long path support (`LongPathsEnabled` in the registry or group policy).

## Requirements

These are the goals the tool is built against. As each one is implemented, it moves into the specs under [openspec/specs/](openspec/specs/).

### Platform and development

- Runs on Windows, Linux and macOS.
- Written in Python, without a Todoist SDK; the tool talks to the Todoist API v1 directly.
- The production code prefers the Python standard library and uses third-party libraries only where they clearly pay off.
- Good test coverage, in two directions:
  - offline tests against recorded fixtures or mocks, which never call the real API;
  - an opt-in contract test against the real API, which detects when Todoist changes its API.

### Behaviour

- A command-line interface, suitable for unattended batch runs.
- Running the tool again gives the same result. An interrupted run resumes where it stopped, and data that was already backed up is not downloaded again.
- After every successful run the backup is complete and up to date.
- Respects the API's rate limits.
- Works on free plans too. Features a plan does not include are skipped with a warning and never cause the tool to fail.

### Scope of the backup

- Active data and **all** completed tasks.
- Archived projects, with their sections, tasks and comments.
- Efficient even with tens of thousands of completed tasks.
- As much metadata as the API offers, not just task titles: projects, sections, labels, comments, attachments and so on.

### Storage

- A long-term stable format that people and machines can both read.
- A documented storage schema.

## Key decisions so far

These will be written up in more detail in the design documents of the corresponding changes.

- **Raw payloads are the source of truth.** API responses are stored unchanged as JSON, so fields the tool does not know about are preserved. A queryable SQLite database may be derived from them later.
- **The backup holds the current state, with tombstones.** Objects deleted in Todoist stay in the backup and are marked as deleted. Older versions are not kept, because a file-based backup of the backup directory provides history.
- **Layout:** one file per active object at `sync/<type>/<id>.json`, one file per month for completed tasks at `completed/<YYYY-MM>.json`, with month boundaries in UTC, one directory per archived project at `archived/<project id>/`, and one directory per comment with an uploaded file at `attachments/<comment id>/`. The current month is included.
- **Completed tasks are frozen.** A month of completed tasks is fetched again until 7 days after it ended; changes made later to its tasks are not captured.
- **Archived projects are snapshots.** An archived project is fetched once, and again only when its listed payload changes, for example after it was unarchived and archived again. Nothing is removed from `sync/` once a snapshot holds it.

## Roadmap

1. **Sync active data** *(implemented)*: a full sync, then incremental syncs, with checkpointing and tombstones. The format is described in [storage_format.md](src/todoist_takeout/storage_format.md). The behaviour is specified in [openspec/specs/](openspec/specs/); the planning is archived in [openspec/changes/archive/2026-10-18-add-active-data-sync/](openspec/changes/archive/2026-10-18-add-active-data-sync/).
2. **Back up completed tasks** *(implemented)*: month files for completed tasks, written incrementally, with every completion of recurring tasks and the comments of completed tasks. The behaviour is specified in [openspec/specs/completed-tasks-backup/](openspec/specs/completed-tasks-backup/); the planning is archived in [openspec/changes/archive/2026-10-18-add-completed-tasks-backup/](openspec/changes/archive/2026-10-18-add-completed-tasks-backup/).
3. **Back up archived projects** *(implemented)*: a snapshot per archived project with its sections, open and completed tasks, sub-projects and comments, which the sync endpoint does not return. The behaviour is specified in [openspec/specs/archived-projects-backup/](openspec/specs/archived-projects-backup/); the planning is archived in [openspec/changes/archive/2026-10-19-add-archived-projects-backup/](openspec/changes/archive/2026-10-19-add-archived-projects-backup/).
4. **Download attachment files** *(implemented)*: the file uploaded to each stored comment, downloaded once into `attachments/<comment id>/`, with the token sent only to Todoist's hosts. The behaviour is specified in [openspec/specs/attachment-downloads/](openspec/specs/attachment-downloads/); the planning is archived in [openspec/changes/archive/2026-10-20-add-attachment-downloads/](openspec/changes/archive/2026-10-20-add-attachment-downloads/).
5. **Derive a SQLite database** from the raw JSON, for queries and migration.
6. **Download Todoist's own backups** (`/api/v1/backups`) as an additional safety net. This is an idea, not yet evaluated.

The documented storage schema is part of each step and grows with the backup format.

## Development

- The dev container ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json)) provides Python 3.12 and uv, and runs `uv sync` when it is created.
- Planning uses [OpenSpec](https://github.com/Fission-AI/OpenSpec). Run `openspec list` to see changes in progress.

### Tests

The project is managed with [uv](https://docs.astral.sh/uv/). `uv sync` creates `.venv/` with the package in editable mode and the development dependencies pinned in [uv.lock](uv.lock):

```sh
uv sync
uv run pytest
```

The commands below are written without `uv run`; either prefix them with it or activate `.venv/` first. Without uv, `python -m pip install -e . --group dev` (pip 25.1 or newer) sets up the same environment, apart from the pinned versions.

The default run is fully offline: it uses the fixtures under [tests/fixtures/](tests/fixtures/) and never calls the real API. CI runs it on Linux, macOS and Windows.

### Contract tests against the real API

The contract tests detect when Todoist changes its API. They are deselected by default; select them with `pytest -m contract`. They use two accounts, each given by its own variable:

| Variable | Account | Used by |
|---|---|---|
| `TODOIST_CONTRACT_TOKEN` | the read account, for example your own | read-only tests only |
| `TODOIST_CONTRACT_WRITE_TOKEN` | the write account: a **dedicated test account, never a personal one** | read-only tests, and the write test |
| `TODOIST_CONTRACT_ALLOW_WRITES=1` | | unlocks the write test |

Each test skips when its variables are not set. The write test uses only `TODOIST_CONTRACT_WRITE_TOKEN`, never the read token. It needs both variables: before it writes anything, it compares the two accounts and refuses to run if the read token is missing, if both variables hold the same token, or if they hold tokens of the same Todoist user.

```sh
# Read-only: the shape of a full sync of each configured account, compared with tests/fixtures/shapes.json.
export TODOIST_CONTRACT_TOKEN=…          # set in your shell; never commit it
export TODOIST_CONTRACT_WRITE_TOKEN=…
pytest -m contract tests/contract/test_read.py

# Read-only: one page each of the four completed-tasks endpoints, compared with tests/fixtures/shapes.json.
pytest -m contract tests/contract/test_completed_read.py

# Read-only: the first page of archived projects and the /full of up to all of them, compared with tests/fixtures/shapes.json.
pytest -m contract tests/contract/test_archived_read.py

# Read-only: downloads one uploaded file of the synced comments into a temporary directory, checks its size, and
# deletes it again. Skips when the comments hold no uploaded file.
pytest -m contract tests/contract/test_attachments_read.py

# Write: creates, deletes, restores and archives temporary projects in the test account, then removes them.
# Its second test completes a recurring task twice and a task with a comment, and checks completed/.
TODOIST_CONTRACT_ALLOW_WRITES=1 pytest -m contract tests/contract/test_write.py

# Write: archives a temporary project with a sub-project, checks both snapshots under archived/ and that a
# forced full sync counts none of their objects as missing, then removes them.
TODOIST_CONTRACT_ALLOW_WRITES=1 pytest -m contract tests/contract/test_archived_write.py

# Write: uploads a small file to a comment of a temporary task, checks it under attachments/, then completes the
# task, archives the project and checks that the next run downloads nothing; removes the project and the upload.
TODOIST_CONTRACT_ALLOW_WRITES=1 pytest -m contract tests/contract/test_attachments_write.py
```

`test_completed_read.py` fails on keys, fields or types that `shapes.json` lacks, and also when `GET /tasks/completed` is gone or the activity log rejects the completion filter: either means that recurring completions can no longer be backed up. The completed-tasks write test reruns the backup for up to two minutes, until the new completion events appear in the activity log.

Findings, such as the shape of deletion payloads, rejected commands and the plan limits of each account, are written to `tests/contract/findings/`. That directory is not committed, and the API payloads in it are scrubbed. If the write test fails during cleanup, it names the projects to delete by hand.

### Recording fixtures

[scripts/record_fixtures.py](scripts/record_fixtures.py) records a full and an incremental sync from a real account and writes them to `tests/fixtures/recorded/<account>/`, together with one page of each completed-tasks endpoint: `by_completion_date.json` and `tasks_completed.json` for the latest month with completed tasks in the past year, `activities.json` with the latest completion events, `comments.json` with the comments of one completed task (not written if no completed task in that year has comments), `projects_archived.json` with the first page of archived projects, and `project_full.json` with the `/full` of one of them: of those on the page, the first with tasks and sections, or else the one with the most content (not written without an archived project). Recording is read-only. Nothing of the account is kept: strings become placeholders (except enum-like values such as colours), IDs become random pseudonyms that still match each other within a recording, and numbers become `0` (except plan limits). Then the script regenerates `tests/fixtures/shapes.json`, which the contract tests compare against. Its `endpoints` section describes the completed-tasks and archived-projects endpoints; as long as an endpoint has no recording, it is taken from the hand-written fixtures in [tests/fixtures/completed/](tests/fixtures/completed/) and [tests/fixtures/archived/](tests/fixtures/archived/). Its `file_attachments` section describes the `file_attachment` of uploaded files, from the recordings that hold one, or else from [tests/fixtures/attachments/uploads.json](tests/fixtures/attachments/uploads.json); file names and URLs are scrubbed like all other strings.

```sh
python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
python scripts/record_fixtures.py rescrub                  # apply the current scrubbing rules to the recordings
python scripts/record_fixtures.py shapes                   # regenerate shapes.json only
```

Review the recorded files for personal data before you commit them.

## License

[MIT](LICENSE)
