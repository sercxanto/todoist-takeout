# Proposal

> Status: the scope was settled with the user in conversation (2026-10-23).

## Why

At 263 lines, `README.md` is almost entirely reference material: options, exit statuses, every table and view of the database, three scheduler examples, the move into the `backup-and-sqlite` layout and platform notes. What the tool offers, and the one command most users need, are buried. The README is also the long description on PyPI once the package is published, where its relative links and in-page anchors do not work.

## What Changes

- `README.md` stays at the repository root and becomes a short entry point (about 60 to 80 lines): what the tool is, why to use it, what it backs up, a screenshot of the backup in Datasette, a quick start with `backup-and-sqlite`, a pointer to scheduling, a list of links to the detailed documentation, status, contributing and license.
- The detailed user documentation moves, with its content unchanged apart from cuts where a split repeats something, into new pages under `docs/`:
  - `docs/backup.md`: the token, the `backup` command, what the backup contains, large first runs, the Free plan, Todoist's own backups, options, output and exit status.
  - `docs/viewing.md`: the `sqlite` and `backup-and-sqlite` commands, the directory layout, moving an existing backup, the tables and views.
  - `docs/scheduling.md`: cron, launchd and Task Scheduler.
  - `docs/platform-notes.md`: macOS certificates, local disks, Windows long paths.
- The format description stays in `src/todoist_takeout/storage_format.md`, the copy that is written into every backup; the README and `docs/` link to it.
- Links in the README are absolute GitHub URLs and the README has no in-page anchors, so that it works on GitHub and on PyPI alike. Links between the pages under `docs/` stay relative.
- A screenshot of Datasette, made from an invented demo backup and never from real account data, under `docs/images/`, with a script that recreates it.
- `pyproject.toml` gets `[project.urls]` (homepage, documentation, issues), which PyPI shows next to the description.
- An offline test checks the links of the README and of `docs/`.
- `docs/ideas.md` moves to `dev/ideas.md` and is translated into English. The README idea points to this change; badges, the PyPI install line and links to release tags are added to the release-workflow idea.
- `CONTRIBUTING.md` and `AGENTS.md` describe where user documentation and development notes go and the link rule for the README.

Out of scope: badges, installation from PyPI, links to release tags instead of `main`, and any change to the tool's behaviour. The first three belong to the release workflow.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. The change is documentation only; `.openspec.yaml` sets `skip_specs: true`. The `backup-storage` requirement that the format description is kept in the repository is unaffected, because `storage_format.md` stays where it is.

## Impact

- Documentation: `README.md` rewritten; new `docs/backup.md`, `docs/viewing.md`, `docs/scheduling.md`, `docs/platform-notes.md` and `docs/images/`; `docs/ideas.md` moved to `dev/ideas.md`; `CONTRIBUTING.md` and `AGENTS.md` updated.
- `pyproject.toml`: `[project.urls]`; no new runtime dependency.
- New script under `scripts/` for the demo backup and the screenshot; new test `tests/test_docs.py`.
- No production code changes.
