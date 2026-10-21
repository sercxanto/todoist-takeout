# Design

## Context

See proposal.md for the motivation and `specs/sqlite-view/spec.md` for the requirements. The code it builds on is `sqlite_view.py`:

- `TABLES` is a tuple of `Table` definitions (name, description, `Column`s with descriptions, a row builder, facets, sort, label column, search columns). The schema, the inserts, the FTS tables and the metadata file are all generated from it.
- `_rebuild` runs one transaction: `_drop_schema` (which already drops views, before tables), then create and fill every table, then `_create_search`, then `PRAGMA application_id` and `user_version` (`SCHEMA_VERSION = 1`).
- `metadata()` writes one entry per table under `databases.<stem>.tables`.
- Times in the tables are text as the backup holds them: timestamps in UTC (`2026-10-20T09:00:00Z`, possibly with fractional seconds); `due_date` as a date (`2026-10-20`), a floating date-time (`2026-10-20T09:00:00`) or a UTC date-time (`2026-10-20T09:00:00Z`), all three in the recorded fixtures.
- `completions` has no index on `task_id`; `task_labels` has its primary key `(task_id, label_name)`.
- The archived design of the database (2026-10-20-add-sqlite-view, D3) rejected views over raw JSON: big IDs would be corrupted and the copy rules would need window functions in every view. It also noted that Datasette cannot link a view's foreign keys.

## Goals / Non-Goals

**Goals:**

- The views are readable on their own: names in place of IDs, so the missing foreign-key links in Datasette do not matter.
- The views also work outside Datasette, with `sqlite3` or any other SQLite client.
- One place that defines a view's SQL, its column descriptions and its facets.

**Non-Goals:**

- Fast queries on very large accounts beyond what an index on `completions.task_id` gives.
- Time zones other than the local time of the computer that runs the query; Todoist's own time zone setting in `sync/user.json` is not used.

## Decisions

### D1: SQL views over the built tables

The views are `CREATE VIEW` statements over `tasks`, `projects`, `sections`, `task_labels` and `completions`. The objections of the archived D3 do not apply: the views read the cleaned tables, whose IDs are already text and whose copies are already chosen. Datasette cannot link foreign keys in a view, so the views carry the names themselves (D3 to D5).

*Alternative:* tables filled in Python, like the existing ones. Rejected: "overdue" and "days since the last completion" depend on the time of the query, which a table built at rebuild time cannot follow (spec, Values computed at query time), and the rest is plain joins and grouping.

*Alternative:* canned queries in the metadata file. Rejected, as in the archived design: Datasette 1.0 moves them to `datasette.yaml`, and they would work only in Datasette.

### D2: View definitions in code

A `View` dataclass next to `Table`: name, description, `Column`s (name, type, description, no `references`), the `SELECT` statement, and facets. A tuple `VIEWS` holds `open_tasks`, `done_log` and `recurring_stats`, in that order, because `recurring_stats` reads `done_log`.

- `_rebuild` creates the views after the tables and the index of D6, before `_create_search`, in the same transaction, so a failed rebuild leaves the previous views as well.
- `metadata()` adds one entry per view under the same `tables` key, which is where Datasette reads the options of views too: `description`, `columns` and `facets`. No `sort`, `sort_desc` or `label_column` (D7); no `fts_table`.
- The summary line counts the rows of the tables only, as now; a view stores no rows.
- `SCHEMA_VERSION` goes to 2.

### D3: `open_tasks`

```sql
SELECT t.id, t.content, p.name AS project_name, s.name AS section_name,
       (SELECT group_concat(label_name, ', ')
          FROM (SELECT label_name FROM task_labels WHERE task_id = t.id ORDER BY label_name)) AS labels,
       t.priority, t.due_date, t.due_string, t.is_recurring, t.deadline_date,
       <overdue, D4> AS overdue,
       t.added_at
  FROM tasks t
  LEFT JOIN projects p ON p.id = t.project_id
  LEFT JOIN sections s ON s.id = t.section_id
 WHERE t.status = 'open' AND coalesce(p.is_archived, 0) = 0
 ORDER BY t.due_date IS NULL, substr(t.due_date, 1, 10), t.priority, t.due_date, t.id
```

- `LEFT JOIN` keeps a task whose project or section is not in the database (deleted); `coalesce` keeps it in the view, as nothing says its project is archived.
- Labels are sorted inside a subquery, because `group_concat(... ORDER BY ...)` needs SQLite 3.44, newer than the SQLite of some Python 3.11 builds.
- The order compares the day first, so that the priority decides between tasks of the same day whether they have a time or not; `p1` sorts before `p2` as text. `t.id` makes the order deterministic.
- Tasks of archived sections in active projects stay in the view; the spec only leaves out archived projects.

### D4: Overdue and local time

```sql
CASE
  WHEN t.due_date IS NULL         THEN 0
  WHEN length(t.due_date) = 10    THEN t.due_date < date('now', 'localtime')
  WHEN t.due_date LIKE '%Z'       THEN julianday(t.due_date) < julianday('now')
  ELSE                                 julianday(t.due_date) < julianday('now', 'localtime')
END
```

- A floating date or date-time is a local time by Todoist's definition, so it is compared with the local clock; a UTC time with the UTC clock. SQLite parses the `Z` suffix and fractional seconds.
- `'now'` and `'localtime'` are evaluated per query, which gives the spec's query-time behaviour without a rebuild. `localtime` uses the operating system's time zone through the C library on Windows, macOS and Linux alike.
- A task due today without a time is not overdue until tomorrow; this matches Todoist, which shows such a task under "Today", not under "Overdue".

### D5: `done_log`

```sql
SELECT c.task_id, c.content, c.project_id, p.name AS project_name, c.completed_at,
       datetime(c.completed_at, 'localtime') AS completed_local, c.source,
       EXISTS (SELECT 1 FROM tasks WHERE id = c.task_id) AS task_exists
  FROM completions c LEFT JOIN projects p ON p.id = c.project_id
UNION ALL
SELECT t.id, t.content, t.project_id, p.name, t.completed_at,
       datetime(t.completed_at, 'localtime'), 'task', 1
  FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
 WHERE t.status = 'completed'
   AND NOT EXISTS (SELECT 1 FROM completions WHERE task_id = t.id)
ORDER BY julianday(completed_at) DESC, task_id
```

- **Coverage by task ID only**, as settled with the user: any completion record of a task covers its row in `tasks`. Matching on the completion time as well would need a tolerance, because the activity log's `event_date` and the task's `completed_at` need not be equal. The cost is the spec's "Task completed again" scenario: a task completed, uncompleted and completed again, of which only the first completion is recorded, shows only that one. That happens only with a gap in the activity log.
- Content and project of a recorded completion are the ones at the time of completion, as `completions` holds them; the project name is the project's current name.
- The order uses `julianday` because the timestamps of the two sources can differ in their fractional seconds, which would break a text comparison. `completed_local` is `YYYY-MM-DD HH:MM:SS` in local time.

### D6: Index on `completions.task_id`

`CREATE INDEX completions_task_id ON completions (task_id)`, created after the table is filled. Without it, the `NOT EXISTS` of D5 scans `completions` once per completed task, which on an account with tens of thousands of completions takes seconds per page in Datasette. `_drop_schema` already drops indexes.

### D7: Order of the rows in Datasette

Each view has its order in its own `ORDER BY`. The metadata sets no sort option for views: Datasette pages through a view without a primary key by offset over `SELECT * FROM view`, which keeps the view's order, and a user can still sort by a column in the page. SQLite keeps the order of a view's `ORDER BY` for a query without its own `ORDER BY`; the tests check the order through Datasette as well as directly.

### D8: `recurring_stats`

```sql
SELECT t.id, t.content, t.status, p.name AS project_name, t.due_string,
       count(d.task_id) AS completions,
       datetime(min(julianday(d.completed_at)), 'localtime') AS first_completed_local,
       datetime(max(julianday(d.completed_at)), 'localtime') AS last_completed_local,
       CASE WHEN count(d.task_id) > 1
            THEN round((max(julianday(d.completed_at)) - min(julianday(d.completed_at))) / (count(d.task_id) - 1), 1)
       END AS avg_days_between,
       CAST(julianday('now') - max(julianday(d.completed_at)) AS INTEGER) AS days_since_last
  FROM tasks t
  LEFT JOIN projects p ON p.id = t.project_id
  LEFT JOIN done_log d ON d.task_id = t.id
 WHERE t.is_recurring = 1
 GROUP BY t.id
 ORDER BY max(julianday(d.completed_at)) IS NULL, max(julianday(d.completed_at)) DESC, t.id
```

- It reads `done_log`, not `completions`, so a recurring task completed for good without a record still counts its last completion.
- The average interval is the span divided by the number of gaps, the same as the mean of the intervals, and needs two completions. `days_since_last` counts whole days, rounded down.
- Recurring tasks of archived projects stay in: their history is still history. The status column shows a recurring task that was completed for good.
- Facets: `project_name` and `status`. For `open_tasks`: `project_name`, `priority`, `overdue`, `is_recurring`. For `done_log`: `source`, `project_name`, `task_exists`.

### D9: Tests

In `tests/test_sqlite_view.py`, against small hand-made backups as the existing tests do:

- One test per scenario of the spec. Dates far in the past and future (2000-01-01, 2999-12-31) keep the overdue tests independent of the day they run. SQLite's `'now'` cannot be replaced from Python, so "A day passes" is tested by its two halves: `open_tasks` is a view in `sqlite_master`, not a table, and a task due yesterday is overdue while one due today is not, both dates taken from Python's local date when the test runs.
- The columns of each view, read with `PRAGMA table_info(<view>)`, match its `View` definition, so the descriptions in the metadata file cannot drift from the SQL.
- `completed_local` matches Python's `datetime.astimezone()` of the same UTC time, which uses the same operating-system time zone, so the test passes in any time zone.
- The in-process Datasette test opens each view and checks its description and facets.

### D10: Documentation

- `README.md`, "Viewing the backup": a short table of the three views after the table of tables, and one sentence that overdue and the days since the last completion use the local time of the computer that runs the query and stay current between rebuilds.
- The column descriptions in the metadata file document the views, as they do the tables.

### D11: Cross-platform

Nothing new on the file side: the views are created in the same in-place transaction as the tables. The date functions are SQLite's own; `localtime` reads the operating system's time zone on Windows, macOS and Linux. The CI matrix runs the view tests on all three, which needs a manual CI run before the change is merged.

## Risks / Trade-offs

- [Datasette 0.65 or 1.0 ignores facets or column descriptions for views] -> The in-process test against 0.65 checks them; 1.0 is checked once by hand, as in the archived design.
- [The order of a view is not guaranteed by SQL] -> SQLite keeps it for a plain `SELECT` over the view, which is what Datasette runs; a user who sorts by a column gets that order instead.
- [`recurring_stats` joins the `done_log` view] -> SQLite materialises it and builds an automatic index for the join; checked by hand on a real backup, with the index of D6.
- [Local time of a server] -> If Datasette runs on a computer in another time zone, overdue and local times follow that computer. The README says so.
- [A task completed again with only the first completion recorded] -> Shows only the first completion (D5). Accepted with the user; the spec states it as a scenario.

## Migration Plan

None. The next run of the `sqlite` command rebuilds any older database with schema version 2. A database of version 2 opened by an older version of the tool is rebuilt with version 1 and loses the views, as before.
