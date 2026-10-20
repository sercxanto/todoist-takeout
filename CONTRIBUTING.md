# Contributing

How to set up a development environment, run the checks and the tests, and record fixtures, followed by the requirements the tool is built against and the key design decisions. How to install and use the tool is described in the [README](README.md).

- The dev container ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json)) provides Python 3.12 and uv, and runs `uv sync` when it is created.
- Planning uses [OpenSpec](https://github.com/Fission-AI/OpenSpec). Run `openspec list` to see changes in progress.

## Setup and tests

The project is managed with [uv](https://docs.astral.sh/uv/). `uv sync` creates `.venv/` with the package in editable mode and the development dependencies pinned in [uv.lock](uv.lock):

```sh
uv sync
uv run pytest
```

The commands below are written without `uv run`; either prefix them with it or activate `.venv/` first. Without uv, `python -m pip install -e . --group dev` (pip 25.1 or newer) sets up the same environment, apart from the pinned versions.

The default run is fully offline: it uses the fixtures under [tests/fixtures/](tests/fixtures/) and never calls the real API. CI runs it on Linux, macOS and Windows.

## Coverage

[pytest-cov](https://pytest-cov.readthedocs.io/) measures the line and branch coverage of `src/todoist_takeout/`, also in the tests that run the CLI in a child process. It is off by default; `--cov` turns it on and prints the files that are not fully covered, with their missing lines:

```sh
pytest --cov
pytest --cov --cov-report=html       # browsable report in htmlcov/
```

CI measures it in every job and shows the report in the job summary. A run with `--cov` fails when the total coverage is below 90 %. Coverage differs a little between operating systems, because some code runs on Windows only or everywhere but Windows; the margin to the current level leaves room for that.

## Linting and formatting

[Ruff](https://docs.astral.sh/ruff/) lints and formats the code; its settings are in [pyproject.toml](pyproject.toml), and uv.lock pins its version. CI fails when either check finds something:

```sh
ruff check .            # add --fix to apply the safe fixes
ruff format .           # or --check to only report
```

## Dependencies and CI

The workflows under [.github/workflows/](.github/workflows/) pin each action to a commit hash, with its version in a comment, because a tag can be moved to other code. [Dependabot](.github/dependabot.yml) proposes updates of the actions and of the development dependencies in uv.lock once a month, each group in one pull request, and only for releases that are at least a week old.

## Contract tests against the real API

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

## Recording fixtures

[scripts/record_fixtures.py](scripts/record_fixtures.py) records a full and an incremental sync from a real account and writes them to `tests/fixtures/recorded/<account>/`, together with one page of each completed-tasks endpoint: `by_completion_date.json` and `tasks_completed.json` for the latest month with completed tasks in the past year, `activities.json` with the latest completion events, `comments.json` with the comments of one completed task (not written if no completed task in that year has comments), `projects_archived.json` with the first page of archived projects, and `project_full.json` with the `/full` of one of them: of those on the page, the first with tasks and sections, or else the one with the most content (not written without an archived project). Recording is read-only. Nothing of the account is kept: strings become placeholders (except enum-like values such as colours), IDs become random pseudonyms that still match each other within a recording, and numbers become `0` (except plan limits). Then the script regenerates `tests/fixtures/shapes.json`, which the contract tests compare against. Its `endpoints` section describes the completed-tasks and archived-projects endpoints; as long as an endpoint has no recording, it is taken from the hand-written fixtures in [tests/fixtures/completed/](tests/fixtures/completed/) and [tests/fixtures/archived/](tests/fixtures/archived/). Its `file_attachments` section describes the `file_attachment` of uploaded files, from the recordings that hold one, or else from [tests/fixtures/attachments/uploads.json](tests/fixtures/attachments/uploads.json); file names and URLs are scrubbed like all other strings.

```sh
python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
python scripts/record_fixtures.py rescrub                  # apply the current scrubbing rules to the recordings
python scripts/record_fixtures.py shapes                   # regenerate shapes.json only
```

Review the recorded files for personal data before you commit them.

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

- **Raw payloads are the source of truth.** API responses are stored unchanged as JSON, so fields the tool does not know about are preserved. The `sqlite` command derives a SQLite database from them for browsing; it is rebuilt from the JSON on every run and is never a source of truth.
- **The backup holds the current state, with tombstones.** Objects deleted in Todoist stay in the backup and are marked as deleted. Older versions are not kept, because a file-based backup of the backup directory provides history.
- **Layout:** one file per active object at `sync/<type>/<id>.json`, one file per month for completed tasks at `completed/<YYYY-MM>.json`, with month boundaries in UTC, one directory per archived project at `archived/<project id>/`, and one directory per comment with an uploaded file at `attachments/<comment id>/`. The current month is included.
- **Completed tasks are frozen.** A month of completed tasks is fetched again until 7 days after it ended; changes made later to its tasks are not captured.
- **Archived projects are snapshots.** An archived project is fetched once, and again only when its listed payload changes, for example after it was unarchived and archived again. Nothing is removed from `sync/` once a snapshot holds it.
