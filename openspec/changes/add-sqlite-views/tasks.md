# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation session.

- One session with `/opsx:apply add-sqlite-views`, medium effort. Commit after each completed group, once its tests pass.
- No task needs a token or the real API. Task 4.1 needs the user to look at Datasette in a browser with a real backup.
- The change runs SQLite's local-time functions, so it needs a manual CI run on Windows and macOS before it is merged.

## 1. View framework

- [x] 1.1 Add the `View` dataclass and an empty `VIEWS` tuple to `sqlite_view.py`, create the views and the index `completions_task_id` in `_rebuild` after the tables and before the search tables, add the views to `metadata()` with description, columns and facets, and raise `SCHEMA_VERSION` to 2 (design D2, D6). Verify that the existing tests of `tests/test_sqlite_view.py` pass, adjusted only where they assert the schema version, and that a test finds the index in `sqlite_master`
- [x] 1.2 Add a test that compares the columns of every view (`PRAGMA table_info`) with its `View` definition, and one that checks that a failed rebuild keeps the previous views (design D2, D9). Verify that both tests pass once the first view exists (task 2.1)

## 2. Views

- [x] 2.1 Add `open_tasks` with the overdue expression (design D3, D4). Verify with tests for every scenario of the spec requirements "Open tasks view", "Overdue tasks" and "Values computed at query time", using dates far in the past and future and, for "A day passes", Python's local date
- [x] 2.2 Add `done_log` (design D5). Verify with tests for every scenario of the spec requirement "Completion log view", including "Task completed again", the order across both sources with differing fractional seconds, and `completed_local` against Python's `astimezone()`
- [x] 2.3 Add `recurring_stats` over `done_log` (design D8). Verify with tests for every scenario of the spec requirement "Recurring tasks view"
- [x] 2.4 Extend the in-process Datasette test to open each view and check its description, column descriptions, facets and the order of its first rows (spec "Views in Datasette", design D7). Verify that the test passes against the pinned Datasette 0.65

## 3. Documentation

- [x] 3.1 Add the views to the "Viewing the backup" section of `README.md`: a short table of the three views, and that overdue and the days since the last completion use the local time of the computer running the query and stay current between rebuilds (design D10). Verify that the section names every view and column group the spec requires and that the example summary line is unchanged

## 4. Integration

- [x] 4.1 Build a database from a real backup and open it with `uvx datasette`, in 0.65.5 and in the current 1.0 alpha; check that the three views load in well under a second, show their descriptions and facets, and keep their order (design D7, D8 risks). Verify by the user looking at the pages in a browser; note the result in design.md's Risks section
- [x] 4.2 Run `uv run ruff format .`, `uv run ruff check .` and `uv run pytest`, and fix what they report. Verify that all three finish without findings or failures

## Workflow follow-up

- Start a manual CI run (Windows, macOS, Linux) before merging.
- Archive the change with `/opsx:archive add-sqlite-views` once it is merged.
