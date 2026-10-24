# Contributing

How to install the unreleased code, set up a development environment, run the checks and the tests, record fixtures and cut a release, followed by the requirements the tool is built against and the key design decisions. How to install and use the tool is described in the [README](README.md) and the pages under [docs/](docs/).

- The dev container ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json)) provides Python 3.12, uv and the OpenSpec CLI, and runs `uv sync` when it is created.
- Planning uses [OpenSpec](https://github.com/Fission-AI/OpenSpec). Run `openspec list` to see changes in progress.
- Ideas that are not planned yet are collected in [dev/ideas.md](dev/ideas.md).

## Installing the unreleased code

The README installs the latest release from PyPI. To use what is on `main`, or on a branch, install it from the repository, or from a checkout:

```sh
uv tool install git+https://github.com/sercxanto/todoist-takeout             # main
uv tool install git+https://github.com/sercxanto/todoist-takeout@<branch>
uv tool install .                   # the checkout in the current directory
```

`pipx install` takes the same arguments. `todoist-takeout --version` then shows the version of the last release, since the version changes only in a release commit (see [Releasing](#releasing)).

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

## Linting, formatting and type checking

[Ruff](https://docs.astral.sh/ruff/) lints and formats the code, and [ty](https://docs.astral.sh/ty/) checks the type annotations of `src/` for all platforms, so that the code for Windows or macOS only is checked on Linux too. Their settings are in [pyproject.toml](pyproject.toml), and uv.lock pins their versions; ty is still in beta, so an update may report new findings. CI fails when any check finds something, a warning of ty included:

```sh
ruff check .            # add --fix to apply the safe fixes
ruff format .           # or --check to only report
ty check
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

[scripts/record_fixtures.py](scripts/record_fixtures.py) records a full and an incremental sync from a real account and writes them to `tests/fixtures/recorded/<account>/`, together with one page of each completed-tasks endpoint: `by_completion_date.json` and `tasks_completed.json` for the latest month with completed tasks in the past year, `activities.json` with the latest completion events, `comments.json` with the comments of one completed task (not written if no completed task in that year has comments), `projects_archived.json` with the first page of archived projects, and `project_full.json` with the `/full` of one of them: of those on the page, the first with tasks and sections, or else the one with the most content (not written without an archived project). Recording is read-only. Nothing of the account is kept: strings become placeholders (except enum-like values such as colours), IDs become random pseudonyms that still match each other within a recording, numbers become `0` and booleans `false` (except plan limits and `full_sync`). Then the script regenerates `tests/fixtures/shapes.json`, which the contract tests compare against. Its `endpoints` section describes the completed-tasks and archived-projects endpoints; as long as an endpoint has no recording, it is taken from the hand-written fixtures in [tests/fixtures/completed/](tests/fixtures/completed/) and [tests/fixtures/archived/](tests/fixtures/archived/). Its `file_attachments` section describes the `file_attachment` of uploaded files, from the recordings that hold one, or else from [tests/fixtures/attachments/uploads.json](tests/fixtures/attachments/uploads.json); file names and URLs are scrubbed like all other strings.

```sh
python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
python scripts/record_fixtures.py rescrub                  # apply the current scrubbing rules to the recordings
python scripts/record_fixtures.py shapes                   # regenerate shapes.json only
```

Review the recorded files for personal data before you commit them.

## Documentation

The [README](README.md) is the entry point for users and stays short; details go into a page under [docs/](docs/), linked from the README's documentation list. The README is also the project description on PyPI, where relative links and links to its own sections do not work, so it links only to absolute URLs, files of this repository as `https://github.com/sercxanto/todoist-takeout/blob/main/<path>` and images as `.../raw/main/<path>`. When a release is built, [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) moves these links from `main` to the release's tag in the PyPI description (configured in pyproject.toml), so the description of each version keeps showing the documentation of that version; [tests/test_release.py](tests/test_release.py) checks the result. The pages under `docs/` link to each other and to the repository with relative links. [tests/test_docs.py](tests/test_docs.py) checks both, and that every linked file and heading exists.

## The screenshot in the README

[docs/images/datasette.png](docs/images/datasette.png) shows the `open_tasks` view of an invented demo account. [scripts/demo_screenshot.py](scripts/demo_screenshot.py) recreates it: it expands [scripts/demo/demo.json](scripts/demo/demo.json) into API responses, runs `backup-and-sqlite` against the fake API of the tests into a temporary directory, serves the result with Datasette and takes the screenshot with [shot-scraper](https://shot-scraper.datasette.io). Due dates are relative to the day of the run. Run it again when the database or its views change in a way the screenshot shows:

```sh
uvx shot-scraper install                    # once: downloads the browser shot-scraper drives
uv run python scripts/demo_screenshot.py    # writes docs/images/datasette.png
```

On Linux, the browser needs system libraries that a minimal system such as the dev container lacks; `sudo uvx --from playwright playwright install-deps chromium-headless-shell` installs them with apt. In the dev container that command fails, because apt rejects the unsigned Yarn repository of the base image; install the libraries directly instead, with `sudo apt-get install libatk1.0-0 libatspi2.0-0 libasound2 libdbus-1-3 libgbm1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2` after an `apt-get update` that leaves out `/etc/apt/sources.list.d/yarn.list`. `--keep DIR` builds the demo in `DIR` and keeps it, for a look with `uvx datasette DIR`. Never take the screenshot from a real account. A test runs the data step of the script, so that the demo account keeps working with the backup code.

## Releasing

Versions follow [Semantic Versioning](https://semver.org). The version is `__version__` in [`src/todoist_takeout/__init__.py`](src/todoist_takeout/__init__.py), and it changes only in a release commit; between releases, `main` carries the version of the last release. While the version is 0.x, a change that breaks the command line or what users rely on in the backup raises the minor version, and everything else the patch version. The format version in a backup's `state.json` is independent of it.

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): `## [Unreleased]` first, then `## [X.Y.Z] - YYYY-MM-DD` for each release, newest first, with entries under `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed` and `### Security`. Link references end the file: `[Unreleased]` points to `https://github.com/sercxanto/todoist-takeout/compare/vX.Y.Z...HEAD` with the newest release (before the first release, to `https://github.com/sercxanto/todoist-takeout/commits/main`), and each version to `https://github.com/sercxanto/todoist-takeout/releases/tag/vX.Y.Z`. Every change that users can notice adds an entry under `Unreleased`, written for users: what they notice, not how it was built. [scripts/release.py](scripts/release.py) parses the file, and [tests/test_release.py](tests/test_release.py) checks its structure.

Pushing a tag `vX.Y.Z` starts [.github/workflows/release.yml](.github/workflows/release.yml). It checks that the tagged commit is on `main` and that the tag, `__version__` and the changelog agree, runs the jobs of the tests workflow on the tagged commit, builds the sdist and the wheel once, and runs the wheel's `--version` on Linux, macOS and Windows. Then it publishes the same files to TestPyPI, waits for the maintainer to approve the deployment to PyPI, publishes them to PyPI, and creates a GitHub Release with the changelog section as its notes and both files attached. PyPI and TestPyPI trust the workflow through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no token is stored anywhere, and PyPI shows an attestation of where the files were built. This relies on settings outside the repository: a trusted publisher on PyPI and one on TestPyPI for the workflow `release.yml`, with the environment `pypi` and `testpypi`, and the GitHub environments of those names, both limited to tags `v*`, where `pypi` has the maintainer as required reviewer.

To cut release X.Y.Z:

1. In CHANGELOG.md, move the entries under `Unreleased` into a new section `## [X.Y.Z] - <today>` below it, point the link reference `[Unreleased]` to `compare/vX.Y.Z...HEAD`, and add `[X.Y.Z]` pointing to `releases/tag/vX.Y.Z`. Set `__version__` to `"X.Y.Z"`.
2. Dry run:

   ```sh
   python scripts/release.py check vX.Y.Z    # the tag, the version and the changelog agree
   uv build --clear
   uvx twine check --strict dist/*
   uv run hatch-fancy-pypi-readme            # optional: the PyPI description, with the version shown as 42.0
   ```

3. Commit as "Release X.Y.Z", push to `main` and wait until CI is green.
4. Tag and push the tag:

   ```sh
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin vX.Y.Z
   ```

5. In the run of the release workflow (*Actions → release*), once "publish to TestPyPI" has finished, look at the version's page on TestPyPI, the description and its links in particular. Then approve the deployment to `pypi` (*Review deployments*). Nothing on PyPI can be uploaded again, so this is the last point to stop.
6. Check the version's page on PyPI and the GitHub Release.

A version number is never reused, and a tag that was published is never moved. A bad release is yanked on PyPI and followed by a patch release. What to do after a failed run depends on how far it got:

- Failed before "publish to TestPyPI", in the check, the tests, the build or the smoke test: fix the cause in a new commit, delete the tag and create it again on that commit (only repository admins can); the version stays the same.
- Failed or rejected from "publish to TestPyPI" on, a rejected approval included: the version is used up, because TestPyPI, like PyPI, accepts each file name only once, and a re-tag would leave the old files there, so the review would no longer show what PyPI gets. Release the fix as the next patch version, with the changelog section renamed to it; the skipped version never appears on PyPI, a gap that Semantic Versioning allows.
- Failed after "publish to PyPI", in the GitHub Release: re-run the failed job; it uses the same files.

If GitHub Actions cannot publish at all, publish from a workstation: create an API token on PyPI that is limited to the project, build from a clean checkout of the tag with `uv build --clear`, upload with `uv publish --token <token>`, and delete the token right after. Such a release has no attestation. No token is kept between releases.

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
