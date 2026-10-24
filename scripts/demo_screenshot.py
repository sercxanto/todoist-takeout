"""Builds a demo backup from invented data and takes the README's screenshot of it in Datasette.

    uvx shot-scraper install                      # once: downloads the browser that shot-scraper drives
    uv run python scripts/demo_screenshot.py      # writes docs/images/datasette.png
    uv run python scripts/demo_screenshot.py --keep ~/todoist-demo   # also keeps the demo directory

The demo account is described in `scripts/demo/demo.json`, with days relative to the day of the run,
so that due dates, overdue flags and the days since a completion look current whenever the script
runs. It is expanded into API responses that the fake API of the tests serves to the real
`backup-and-sqlite` run, so the demo backup is in the format the tool writes. Nothing comes from a
real account.

The tests import this module for `build_demo`, which needs neither Datasette nor a browser.
"""

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_api import BY_COMPLETION_DATE, TASKS_COMPLETED, FakeApi  # noqa: E402

from todoist_takeout import backup_and_sqlite, completed  # noqa: E402
from todoist_takeout.api import Client  # noqa: E402

DEMO = Path(__file__).resolve().parent / "demo" / "demo.json"
SCREENSHOT = ROOT / "docs" / "images" / "datasette.png"
USER_ID = "40000001"
# Todoist's API counts priorities the other way round from its apps: 4 is p1.
PRIORITIES = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
# The page of the screenshot, and the columns it leaves out to fit the width.
PAGE = "todoist/open_tasks"
HIDDEN_COLUMNS = ("id", "due_string", "deadline_date", "added_at")
# The parts of the page the screenshot leaves out, so that it shows the title, the facets and the rows.
HIDDEN_PARTS = ("dl.column-descriptions", "p.export-links", "p.suggested-facets", ".advanced-export", "pre.wrapped-sql")
WIDTH = 1200
DATASETTE_START_TIMEOUT = 30


def build_demo(directory: Path, now: datetime | None = None) -> None:
    """Run `backup-and-sqlite` into `directory` against the fake API, serving the demo account."""
    now = now or datetime.now(UTC)
    account = Account(json.loads(DEMO.read_text(encoding="utf-8")), now)
    backup_and_sqlite.run(directory, Client(account.api(), sleep=lambda delay: None))


def demo_id(*parts: str) -> str:
    """A stable, invented ID in the style of Todoist's, for one object of the demo account."""
    return "6D" + hashlib.sha256("/".join(parts).encode()).hexdigest()[:14]


def timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Account:
    """The demo account of `demo.json`, as the responses of the API endpoints the backup calls."""

    def __init__(self, demo: dict, now: datetime):
        self.demo = demo
        self.now = now
        self.today = now.astimezone().date()
        self.joined = now - timedelta(days=demo["user"]["joined_ago"])
        self.projects = {p["key"]: demo_id("project", p["key"]) for p in demo["projects"]}
        self.sections = {s["key"]: demo_id("section", s["key"]) for s in demo["sections"]}
        self.tasks = {t["key"]: demo_id("task", t["key"]) for t in demo["tasks"]}

    def api(self) -> FakeApi:
        api = FakeApi({"*": self.full_sync(), "DEMO-1": self.unchanged("DEMO-1")}, files={})
        for month, items in sorted(self.by_month(self.completed_items(), "completed_at").items()):
            since, until = completed.month_window(month)
            api.cursor_pages(
                BY_COMPLETION_DATE, {"since": since, "until": until}, [{"items": items, "next_cursor": None}]
            )
        for month, entries in sorted(self.by_month(self.recurring_completions(), "completed_at").items()):
            since, _until = completed.month_window(month)
            page = {"items": entries, "projects": {}, "sections": {}}
            api.offset_pages(TASKS_COMPLETED, {"since": since}, [page], completed.BACKFILL_PAGE_LIMIT)
        return api

    # --- The sync -----------------------------------------------------------------------------

    def full_sync(self) -> bytes:
        demo = self.demo
        response = {
            "sync_token": "DEMO-1",
            "full_sync": True,
            "full_sync_date_utc": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "projects": [self.project(p, i) for i, p in enumerate(demo["projects"])],
            "sections": [self.section(s, i) for i, s in enumerate(demo["sections"])],
            "labels": [self.label(lb, i) for i, lb in enumerate(demo["labels"])],
            "items": [self.item(t, i) for i, t in enumerate(demo["tasks"])],
            "notes": [self.note(c, i) for i, c in enumerate(demo["comments"])],
            "project_notes": [],
            "user": self.user(),
            "user_plan_limits": {
                "current": {"plan_name": "pro", "comments": True, "completed_tasks": True, "activity_log_limit": -1}
            },
        }
        return json.dumps(response).encode()

    @staticmethod
    def unchanged(token: str) -> bytes:
        return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()

    def user(self) -> dict:
        user = self.demo["user"]
        return {
            "id": USER_ID,
            "email": user["email"],
            "full_name": user["full_name"],
            "inbox_project_id": self.projects["inbox"],
            "lang": "en",
            "is_premium": True,
            "joined_at": timestamp(self.joined),
        }

    def project(self, project: dict, order: int) -> dict:
        parent = project.get("parent")
        result = {
            "id": self.projects[project["key"]],
            "name": project["name"],
            "description": "",
            "color": project["color"],
            "parent_id": self.projects[parent] if parent else None,
            "child_order": order,
            "is_collapsed": False,
            "shared": False,
            "can_assign_tasks": False,
            "is_deleted": False,
            "is_archived": False,
            "is_favorite": project.get("favorite", False),
            "view_style": "list",
            "created_at": timestamp(self.joined),
            "updated_at": timestamp(self.joined),
        }
        if project.get("inbox"):
            result["inbox_project"] = True
        return result

    def section(self, section: dict, order: int) -> dict:
        return {
            "id": self.sections[section["key"]],
            "name": section["name"],
            "project_id": self.projects[section["project"]],
            "section_order": order,
            "is_collapsed": False,
            "user_id": USER_ID,
            "is_deleted": False,
            "is_archived": False,
            "archived_at": None,
            "added_at": timestamp(self.joined),
            "updated_at": timestamp(self.joined),
        }

    @staticmethod
    def label(label: dict, order: int) -> dict:
        return {
            "id": demo_id("label", label["name"]),
            "name": label["name"],
            "color": label["color"],
            "item_order": order,
            "is_deleted": False,
            "is_favorite": label.get("favorite", False),
        }

    def item(self, task: dict, order: int, *, completed_at: datetime | None = None) -> dict:
        added = self.joined + timedelta(days=order + 1)
        key = task.get("key") or task["content"]
        notes = sum(1 for c in self.demo["comments"] if c["task"] == task.get("key"))
        return {
            "id": self.tasks.get(key) or demo_id("task", key),
            "user_id": USER_ID,
            "project_id": self.projects[task["project"]],
            "section_id": self.sections[task["section"]] if task.get("section") else None,
            "parent_id": None,
            "added_by_uid": USER_ID,
            "assigned_by_uid": None,
            "responsible_uid": None,
            "labels": task.get("labels", []),
            "deadline": self.deadline(task),
            "duration": None,
            "checked": completed_at is not None,
            "is_deleted": False,
            "added_at": timestamp(added),
            "completed_at": timestamp(completed_at) if completed_at else None,
            "completed_by_uid": USER_ID if completed_at else None,
            "updated_at": timestamp(completed_at or added),
            "due": self.due(task),
            "priority": PRIORITIES[task["priority"]],
            "child_order": order,
            "content": task["content"],
            "description": task.get("description", ""),
            "note_count": notes,
            "day_order": -1,
            "is_collapsed": False,
        }

    def due(self, task: dict) -> dict | None:
        if "due_in" not in task:
            return None
        day = self.today + timedelta(days=task["due_in"])
        recurring = task.get("recurring")
        return {
            "date": day.isoformat(),
            "timezone": None,
            "string": recurring or self.day_string(day),
            "lang": "en",
            "is_recurring": recurring is not None,
        }

    def deadline(self, task: dict) -> dict | None:
        if "deadline_in" not in task:
            return None
        return {"date": (self.today + timedelta(days=task["deadline_in"])).isoformat(), "lang": "en"}

    @staticmethod
    def day_string(day: date) -> str:
        return f"{day:%b} {day.day}"

    def note(self, comment: dict, order: int) -> dict:
        return {
            "id": demo_id("note", str(order)),
            "posted_uid": USER_ID,
            "item_id": self.tasks[comment["task"]],
            "content": comment["content"],
            "file_attachment": None,
            "uids_to_notify": [],
            "is_deleted": False,
            "posted_at": timestamp(self.now - timedelta(days=2, hours=order)),
            "reactions": {},
        }

    # --- Completed tasks and completions --------------------------------------------------------

    def done_at(self, days_ago: int, order: int) -> datetime:
        """A time on the day `days_ago` days before today, spread over the day by `order`."""
        return self.now - timedelta(days=days_ago, hours=order % 9, minutes=17 * order % 60)

    def completed_items(self) -> list[dict]:
        """The tasks completed for good, as `tasks/completed/by_completion_date` lists them."""
        offset = len(self.demo["tasks"])
        return [
            self.item(task, offset + i, completed_at=self.done_at(task["done_ago"], i))
            for i, task in enumerate(self.demo["completed"])
        ]

    def recurring_completions(self) -> list[dict]:
        """Every completion of the recurring tasks, as `tasks/completed` lists them."""
        tasks = {t["key"]: (i, t) for i, t in enumerate(self.demo["tasks"])}
        entries = []
        for completions in self.demo["recurring_completions"]:
            order, task = tasks[completions["task"]]
            item = self.item(task, order)
            for n, days_ago in enumerate(completions["done_ago"]):
                done = timestamp(self.done_at(days_ago, order + n))
                entries.append(
                    {
                        "id": demo_id("completion", completions["task"], str(n)),
                        "task_id": item["id"],
                        "user_id": USER_ID,
                        "project_id": item["project_id"],
                        "section_id": item["section_id"],
                        "content": item["content"],
                        "completed_at": done,
                        "note_count": 0,
                        "meta_data": None,
                        "item_object": item,
                    }
                )
        return entries

    @staticmethod
    def by_month(entries: list[dict], field: str) -> dict[str, list[dict]]:
        months: dict[str, list[dict]] = {}
        for entry in entries:
            months.setdefault(entry[field][:7], []).append(entry)
        return months


# --- The screenshot ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + DATASETTE_START_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            sys.exit(f"Datasette stopped with status {server.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    sys.exit(f"Datasette did not answer {url} within {DATASETTE_START_TIMEOUT} seconds")


def screenshot(directory: Path, output: Path) -> None:
    """Serve `directory` with Datasette and save a screenshot of the open tasks to `output`."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "datasette", "serve", str(directory), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(base, server)
        columns = "&".join(f"_nocol={name}" for name in HIDDEN_COLUMNS)
        hide = f"document.querySelectorAll({', '.join(HIDDEN_PARTS)!r}).forEach(e => e.remove())"
        output.parent.mkdir(parents=True, exist_ok=True)
        # The selectors crop the image to the area from the page header to the end of the rows.
        command = ["uvx", "shot-scraper", f"{base}/{PAGE}?{columns}", "-o", str(output), "--width", str(WIDTH)]
        command += ["--javascript", hide, "-s", ".page-header", "-s", ".table-wrapper", "--padding", "16"]
        subprocess.run(command, check=True)
    finally:
        server.terminate()
        server.wait()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=SCREENSHOT, help=f"the image to write (default {SCREENSHOT})")
    parser.add_argument("--keep", type=Path, help="build the demo in this new directory and keep it")
    args = parser.parse_args(argv)
    if args.keep:
        build_demo(args.keep)
        screenshot(args.keep, args.output)
        return
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "todoist"
        build_demo(directory)
        screenshot(directory, args.output)


if __name__ == "__main__":
    main()
