# Design

## Context

See proposal.md for the motivation. The current state:

- `README.md` (263 lines) holds all user documentation. It links to `src/todoist_takeout/storage_format.md` and `CONTRIBUTING.md` with relative links, and to its own sections with anchors (`#viewing-the-backup`, `#backup-and-database-in-one-directory`).
- `pyproject.toml` sets `readme = "README.md"`, so the README becomes the long description on PyPI. PyPI renders it with `readme_renderer`: relative links and images do not resolve there, and headings get IDs prefixed with `user-content-`, without the script GitHub uses to map `#name` to them, so in-page anchors lead nowhere (checked with `readme_renderer` during implementation). It has no `[project.urls]` yet.
- `src/todoist_takeout/storage_format.md` is the only copy of the format description; `sync.py` writes it into every backup as `README.md`, and the `backup-storage` spec requires it to be kept in the repository.
- `sqlite_view.SOURCE_URL` is `https://github.com/sercxanto/todoist-takeout`, the repository the Datasette metadata already points to. The repository is private for now.
- `docs/` holds only `ideas.md`, a German list of development ideas.
- `tests/fake_api.py` serves the backup code offline. `tests/fixtures/` holds scrubbed recordings whose strings are placeholders, so they make poor screenshot material.

## Goals / Non-Goals

**Goals:**

- A README that a new user reads in a minute and that renders the same on GitHub and on PyPI.
- Every fact of the current README is still documented somewhere a user finds from the README.
- Broken links are found by the offline tests, not by readers.

**Non-Goals:**

- Rewriting the reference content. It moves, and is cut only where the split would repeat it.
- Pinning links to release tags, badges and the PyPI install line (release workflow).
- Making the screenshot part of the test suite or CI.

## Decisions

### D1: The README stays at the root, with this outline

`README.md` stays where it is, because GitHub shows it on the repository page and `pyproject.toml` names it. Outline, in this order:

1. Title and one sentence on what the tool does.
2. **Why**: the three current points (readable without Todoist, includes history, source for a migration).
3. **What you get**: one short list. It is complete (completed tasks, every completion of recurring tasks, archived projects, attachment files), plain documented JSON, incremental and resumable, browsable in Datasette with full-text search and ready-made views, no runtime dependencies, Windows, macOS and Linux, and the token goes only to Todoist.
4. The screenshot (D5).
5. **Quick start**: install straight from the repository with `uv tool install git+https://github.com/sercxanto/todoist-takeout` (pipx and a checkout as alternatives; settled during implementation, so that users need no clone), set `TODOIST_API_TOKEN`, run `todoist-takeout backup-and-sqlite ~/Backups/todoist`, open `uvx datasette ~/Backups/todoist`. `backup-and-sqlite` leads because it is what most users want; `backup` and `sqlite` are mentioned in one sentence with a link.
6. **Run it daily**: one sentence, linking to `docs/scheduling.md`.
7. **Documentation**: one line per page of D2 and the format description.
8. **Status** (the current status note, shortened), **Contributing**, **License**.

The current "Status" block moves from the top to item 8, so the README opens with what the tool does.

*Alternative:* move the README to `docs/` and point `pyproject.toml` there. Rejected: GitHub would show no README on the repository page.

### D2: Detailed pages under `docs/`, by task

| Page | Sections of the current README |
|---|---|
| `docs/backup.md` | Token (all of it, including the HTTPS and redirect rule), Running a backup, What the backup contains with its five bold paragraphs, the options table, the output, Exit status |
| `docs/viewing.md` | Viewing the backup (sqlite, tables, views, full-text search, exit status), Backup and database in one directory (layout, `--database`, failure behaviour, moving an existing backup) |
| `docs/scheduling.md` | Scheduling (token file, cron, launchd, Task Scheduler) |
| `docs/platform-notes.md` | Platform notes |

Each page starts with one sentence on what it covers and a link back to the README. Links between the pages and to `../src/todoist_takeout/storage_format.md` are relative, which GitHub resolves; the pages are not shipped to PyPI.

There is no `docs/README.md`. The documentation list in the README is the index, and a second README would be confused with the first.

*Alternatives:* one long `docs/user-guide.md`, rejected because a single long page is what the README is today, only moved; one page per command, rejected because `backup-and-sqlite` would then repeat the token, options and exit statuses of both other pages.

### D3: Absolute links in the README, relative ones elsewhere

Every link and image in the README is absolute:

- files: `https://github.com/sercxanto/todoist-takeout/blob/main/<path>`
- images: `https://github.com/sercxanto/todoist-takeout/raw/main/<path>`, which GitHub serves to anyone allowed to see the repository and PyPI can embed once the repository is public
- external sites as today

The README links to sections of other pages (for example `docs/backup.md#exit-status`) but never to its own sections. On PyPI the anchors of other pages are harmless, because the links go to GitHub.

*Alternatives:* relative links rewritten at build time with `hatch-fancy-pypi-readme`, rejected for now because it adds a build plugin to fix a problem that absolute links avoid; it becomes worth it with links pinned to release tags, which belong to the release workflow. Links to `raw.githubusercontent.com`, rejected because they do not work while the repository is private.

### D4: `[project.urls]`

`pyproject.toml` gets:

```toml
[project.urls]
Homepage = "https://github.com/sercxanto/todoist-takeout"
Documentation = "https://github.com/sercxanto/todoist-takeout#documentation"
Issues = "https://github.com/sercxanto/todoist-takeout/issues"
```

`Documentation` points to the README's documentation list on GitHub, where the anchor works. PyPI shows these links in its sidebar.

### D5: Screenshot from an invented demo backup

`scripts/demo_screenshot.py` does three steps:

1. Builds a demo backup in a temporary directory by running the real `backup-and-sqlite` against `tests/fake_api.FakeApi`, as the tests do. The demo account is a compact hand-written description in `scripts/demo/demo.json`: a few projects (for example "Home", "Work", "Garden"), sections, labels, open and recurring tasks, comments, completed tasks and the completions of the recurring tasks, with days relative to the day the script runs. The script expands it into the responses of the sync, `tasks/completed/by_completion_date` and the backfill endpoint `tasks/completed`, so the overdue flags and "days since" columns look plausible whenever it is rerun. All names are invented; none come from a real account. (Settled during implementation: a compact description instead of full API responses, because full responses repeat some 25 fields per task and cannot hold relative dates.)
2. `backup-and-sqlite` builds the database with it.
3. Starts Datasette (a development dependency already) on the database and takes a screenshot of the `open_tasks` view with [shot-scraper](https://shot-scraper.datasette.io), run through `uvx`, and writes `docs/images/datasette.png`.

The script is run by hand when the database or its views change visibly; CONTRIBUTING.md says how. It does not run in the tests: shot-scraper needs a browser that Playwright downloads (`uvx shot-scraper install`). The data step is still covered by a test (task 3.2), so that the demo responses do not drift from what the backup code accepts.

The README image has alt text that says what it shows, and is limited to about 1200 pixels width so that it stays small.

*Alternatives:* a hand-written backup directory, rejected because it would drift from the storage format the tool writes; the scrubbed test fixtures, rejected because their placeholder strings show nothing; shot-scraper as a development dependency, rejected because the browser download would then come with every `uv sync`; Datasette's own screenshots, rejected because they do not show this schema.

### D6: The link test

`tests/test_docs.py`, offline and with the standard library only:

- Finds the links and images of `README.md` and of every `docs/*.md` with one regular expression for inline Markdown links `[text](target)` and `![alt](target)`. The pages use no reference-style links or HTML links; the test fails on `<a href` or `](` targets it cannot classify, so that new forms are noticed.
- README: every target is `https://` or `mailto:`, none starts with `#`. Targets that start with `https://github.com/sercxanto/todoist-takeout/blob/main/` or `/raw/main/` must name a file that exists in the checkout, so they are checked without a network request. The prefix comes from `sqlite_view.SOURCE_URL`, so there is one place for the repository URL.
- `docs/*.md`: relative targets must name an existing file; an anchor in a relative target must match a heading of that file, using GitHub's rule for heading IDs (lower case, spaces to `-`, punctuation dropped).
- `pyproject.toml`: every `[project.urls]` value starts with `SOURCE_URL`, read with `tomllib`.

External sites are not checked; that would need the network.

### D7: Development notes in `dev/`

`docs/ideas.md` moves to `dev/ideas.md` with `git mv` and is translated into English, including the status names. Changes to entries:

- "Build a nice README": status "change created", with a link to this change.
- "Define the release workflow": also covers badges (CI status, PyPI version, Python versions), the install line `uv tool install todoist-takeout`, links pinned to release tags (with `hatch-fancy-pypi-readme`), and checking that the README and its images render on PyPI once the repository is public.

`docs/` is then for users only. `AGENTS.md` and `CONTRIBUTING.md` say so: user documentation goes into the README (short) or `docs/` (details), development notes into `dev/`, and the README uses absolute links only.

### D8: Checking the PyPI rendering

Before the change is done, the README is rendered as PyPI renders it, with `uvx --from 'readme-renderer[md]' python -m readme_renderer README.md -o <scratch>/readme.html` and `uv build` followed by `uvx twine check dist/*`. These are one-off checks; neither tool becomes a dependency.

### D9: Cross-platform

The change adds no production code. The link test handles paths with `pathlib`, reads files as UTF-8 and does not depend on line endings, so it behaves the same on Windows, macOS and Linux. The screenshot script is a development tool that runs where Datasette and shot-scraper run; it uses a temporary directory and starts Datasette on a free port.

Not checked: a manual CI run on Windows and macOS, as the CI minutes of the private repository are used up. The new test reads files only, so differences between operating systems are unlikely.

## Risks / Trade-offs

- [Links to `main` describe the latest code, not the installed version] → Acceptable before the first release; links pinned to tags are part of the release-workflow idea (D7).
- [While the repository is private, the absolute links work only for people with access] → The same is true of the repository itself. Nothing is published to PyPI before it is public.
- [The screenshot ages when the views change] → CONTRIBUTING.md names the script and when to run it; the demo data test keeps the data step working.
- [Content is lost in the move] → Task 2.5 compares the old and new text section by section.
- [Archived changes refer to README sections that no longer exist] → They are history and stay as they are.

## Open Questions

- Whether `done_log` makes a better screenshot than `open_tasks`. It can be decided when the screenshot is taken and changes no task.
