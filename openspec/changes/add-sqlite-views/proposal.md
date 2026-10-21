# Proposal

> Status: the scope was settled with the user in an explore session (2026-10-21).

## Why

The SQLite database shows each object once, but three everyday questions still need hand-written SQL or several Datasette pages: what is still to do, what was done when, and how regularly recurring tasks get done. Facets cannot answer them, because the answers combine tables: open tasks need project, section and label names and must leave out archived projects; a complete record of what was done must merge `completions` with the completed tasks that no completion record covers; recurring tasks need counts and intervals per task.

## What Changes

- The database gets three SQL views over its tables, rebuilt with it in the same transaction:
  - `open_tasks`: the open tasks that Todoist still shows, without those of archived projects, with project, section and label names, and whether a task is overdue, ordered by due date and priority.
  - `done_log`: one row per completion: every row of `completions`, plus each completed task that no completion record covers, with the project name and whether the task still exists.
  - `recurring_stats`: one row per recurring task, with the number of its completions, the first and last one, the average interval between them, and the days since the last one.
- Values that depend on the current date, such as whether a task is overdue, are computed when the view is queried, so they stay current between rebuilds. They use the local time of the computer that runs the query.
- The Datasette metadata file describes the views and their columns and sets up facets for them.
- The schema version of the database goes up. As before, any older database the tool built is rebuilt with the new schema.

Out of scope: canned queries, further views (a project overview, completions per month, stale tasks) and charts.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `sqlite-view`: new requirements for the three views and for computing date-dependent values at query time; the Datasette metadata file requirement also covers the views.

## Impact

- `sqlite_view.py`: view definitions next to the table definitions, created after the tables in the rebuild transaction; the metadata file lists them. `_drop_schema` already drops views. `SCHEMA_VERSION` goes from 1 to 2.
- No new dependency; the views use SQLite's built-in date functions.
- Tests in `tests/test_sqlite_view.py` for each view, including the in-process Datasette test.
- Documentation: the "Viewing the backup" section of `README.md` lists the views.
