# todoist-takeout

A command-line tool that keeps a complete, long-term readable local backup of a [Todoist](https://www.todoist.com) account.

> **Status:** early. Backing up active data (projects, sections, labels, tasks, comments and the other resources of the sync API) works. Completed tasks and archived projects are not backed up yet; see the [Roadmap](#roadmap).

## Why

Todoist is a cloud service, so your data is only reachable through it. If the service shuts down, or you lose access to your account for any other reason, the data is gone. todoist-takeout keeps a local copy that:

- stays readable for years, without Todoist and without this tool,
- includes your history, meaning completed tasks and not only the ones still open,
- can serve as the source for migrating to another service.

## Installation

todoist-takeout needs Python 3.11 or newer and has no other runtime dependencies. It is not on PyPI yet, so install it from a checkout of this repository:

```sh
pipx install .        # or: python -m pip install .
todoist-takeout --version
```

## Usage

### Token

The tool authenticates with your personal API token, which you find in Todoist under *Settings → Integrations → Developer*. It reads the token only from the environment variable `TODOIST_API_TOKEN`; there is no command-line option for it, and the tool never writes it to its output or into the backup.

```sh
export TODOIST_API_TOKEN=0123456789abcdef0123456789abcdef01234567
```

### Running a backup

```sh
todoist-takeout backup ~/Backups/todoist
# the same:
python -m todoist_takeout backup ~/Backups/todoist
```

The target directory is created if it does not exist. The first run downloads all active data; later runs download only what changed since the previous run. An interrupted run is simply started again: it resumes from the last checkpoint and does not download data it already stored. The tool refuses to write into a directory that is not empty and is not a todoist-takeout backup. Only one run at a time can use a target directory.

The backup contains a `README.md` that describes its format; the same description is in [src/todoist_takeout/storage_format.md](src/todoist_takeout/storage_format.md).

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
- **Windows long paths:** file names in the backup stay short, but a deeply nested target directory can exceed Windows' 260-character path limit. Use a short target path, or enable long path support (`LongPathsEnabled` in the registry or group policy).

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
- **Layout:** one file per active object at `sync/<type>/<id>.json`, and one file per month for completed tasks at `completed/<YYYY-MM>.json`, with month boundaries in UTC. The current month is included.
- **Completed tasks are frozen.** Changes made later to completed or archived tasks are not captured.

## Roadmap

1. **Sync active data** *(implemented)*: a full sync, then incremental syncs, with checkpointing and tombstones. The format is described in [storage_format.md](src/todoist_takeout/storage_format.md). The behaviour is specified in [openspec/specs/](openspec/specs/); the planning is archived in [openspec/changes/archive/2026-10-18-add-active-data-sync/](openspec/changes/archive/2026-10-18-add-active-data-sync/).
2. **Back up completed tasks:** month files for completed tasks, written incrementally, with every completion of recurring tasks and the comments of completed tasks. The API questions are answered; specs and design are next. See [openspec/changes/add-completed-tasks-backup/](openspec/changes/add-completed-tasks-backup/).
3. **Back up archived projects** with their sections, tasks and comments, which the sync endpoint does not return. The API questions are answered; specs and design are next. See [openspec/changes/add-archived-projects-backup/](openspec/changes/add-archived-projects-backup/).
4. **Download attachment files.**
5. **Derive a SQLite database** from the raw JSON, for queries and migration.
6. **Download Todoist's own backups** (`/api/v1/backups`) as an additional safety net. This is an idea, not yet evaluated.

The documented storage schema is part of each step and grows with the backup format.

## Development

- The dev container ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json)) provides Python 3.12.
- Planning uses [OpenSpec](https://github.com/Fission-AI/OpenSpec). Run `openspec list` to see changes in progress.

### Tests

```sh
python -m pip install -e '.[test]'
pytest
```

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

# Write: creates, deletes, restores and archives temporary projects in the test account, then removes them.
TODOIST_CONTRACT_ALLOW_WRITES=1 pytest -m contract tests/contract/test_write.py
```

Findings, such as the shape of deletion payloads, rejected commands and the plan limits of each account, are written to `tests/contract/findings/`. That directory is not committed, and the API payloads in it are scrubbed. If the write test fails during cleanup, it names the projects to delete by hand.

### API probes for completed tasks and archived projects

`tests/contract/test_probe_read.py` and `tests/contract/test_probe_write.py` are a temporary spike. They answer the open questions of [add-completed-tasks-backup](openspec/changes/add-completed-tasks-backup/proposal.md) and [add-archived-projects-backup](openspec/changes/add-archived-projects-backup/proposal.md), whose proposals record the results (probes R1-R7 and W1-W7). They use the same variables and the same write guard as the contract tests above, and record what they observe in `tests/contract/findings/probe-read-<account>.json` and `probe-write.json`. They fail only when a probe crashes, not because of what the API answers.

```sh
# Read-only probes R1-R7, for each configured account. Several dozen requests on large accounts.
pytest -m contract tests/contract/test_probe_read.py

# Write probes W1-W7, against the write account only: complete, uncomplete and archive in temporary projects.
TODOIST_CONTRACT_ALLOW_WRITES=1 pytest -m contract tests/contract/test_probe_write.py
```

### Recording fixtures

[scripts/record_fixtures.py](scripts/record_fixtures.py) records a full and an incremental sync from a real account and writes them to `tests/fixtures/recorded/<account>/`. Recording is read-only. Nothing of the account is kept: strings become placeholders (except enum-like values such as colours), IDs become random pseudonyms that still match each other within a recording, and numbers become `0` (except plan limits). Then the script regenerates `tests/fixtures/shapes.json`, which the contract tests compare against.

```sh
python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
python scripts/record_fixtures.py rescrub                  # apply the current scrubbing rules to the recordings
python scripts/record_fixtures.py shapes                   # regenerate shapes.json only
```

Review the recorded files for personal data before you commit them.
