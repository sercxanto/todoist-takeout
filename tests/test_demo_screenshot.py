import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def rows(database, query):
    with closing(sqlite3.connect(database)) as con:
        return con.execute(query).fetchall()


def test_the_demo_account_backs_up_into_a_database_with_every_view_filled(demo, tmp_path):
    demo.build_demo(tmp_path / "todoist", NOW)

    database = tmp_path / "todoist" / "todoist.db"
    assert (tmp_path / "todoist" / "json" / "state.json").is_file()
    [(open_tasks, overdue)] = rows(database, "SELECT count(*), sum(overdue) FROM open_tasks")
    assert open_tasks > 10
    assert overdue > 0
    assert {source for (source,) in rows(database, "SELECT DISTINCT source FROM done_log")} == {"backfill", "task"}
    assert all(count > 1 for (count,) in rows(database, "SELECT completions FROM recurring_stats"))


def test_due_dates_follow_the_day_of_the_run(demo, tmp_path):
    demo.build_demo(tmp_path / "todoist", NOW)

    # The demo's days count from the local date, like the overdue flag of open_tasks.
    today = NOW.astimezone().date()
    first, last = (today + timedelta(days=-2)).isoformat(), (today + timedelta(days=12)).isoformat()
    due = rows(tmp_path / "todoist" / "todoist.db", "SELECT min(due_date), max(due_date) FROM open_tasks")
    assert due == [(first, last)]
