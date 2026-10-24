"""Backing up archived projects, which the sync leaves out (design D1, D2).

Each archived project gets a snapshot in `archived/<project id>/`, whose files are shaped like the
API responses they come from. `project.json` is written after every other file of the snapshot, so
a snapshot with a `project.json` is complete for the parts the plan allowed (design D3).
`state.json` holds nothing for this part.
"""

import json
import logging
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from todoist_takeout import completed, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.errors import ApiError, PlanRestricted, StorageError

log = logging.getLogger(__name__)

ARCHIVED_DIR = "archived"
COMMENTS_DIR = "comments"
PROJECT_FILE = "project.json"
FULL_FILE = "full.json"
COMPLETED_FILE = "completed.json"
PROJECT_COMMENTS_FILE = "project_comments.json"

ARCHIVED_PROJECTS = "projects/archived"
LIST_PAGE_LIMIT = 200
# Completed tasks are fetched in windows of this many calendar months, at most 92 days (design D7).
WINDOW_MONTHS = 3
# Whose comments the plan warning names.
OWNER = "archived projects"


def run(target: Path, client: Client, state: dict, now: Callable[[], datetime]) -> None:
    """Store a snapshot of each archived project that is new or changed, and fill in the parts that an
    earlier plan left out of the stored ones."""
    target = Path(target)
    limits = sync.current_plan_limits(target)
    _Backup(
        target,
        client,
        state,
        now(),
        completed_tasks=limits.get("completed_tasks") is not False,
        comments=limits.get("comments") is not False,
    ).run()


def full_path(project_id: str) -> str:
    return f"projects/{urllib.parse.quote(project_id, safe='')}/full"


def snapshot_dir(target: Path, project_id: str) -> Path:
    return Path(target) / ARCHIVED_DIR / storage.encode_key(project_id)


class _Backup:
    """One run of this part. The flags of the parts turn off for the rest of the run when the API
    rejects a request as restricted by the plan (design D5)."""

    def __init__(
        self, target: Path, client: Client, state: dict, moment: datetime, *, completed_tasks: bool, comments: bool
    ):
        self.target = target
        self.client = client
        self.state = state
        self.moment = moment
        self.completed_tasks = completed_tasks
        self.comments = comments
        # For the INFO line of the run (design D13).
        self.new = 0
        self.again = 0
        self.filled = 0
        self.comment_files = 0

    def run(self) -> None:
        try:
            queue = self._list()
        except PlanRestricted:
            log.warning(
                "the Todoist plan does not allow listing archived projects; they cannot be backed up on this plan"
            )
            return
        listed = len(queue)
        handled = {project_id for project_id, _ in queue}
        # The progress is logged also when the run fails, for example on the rate limit (design D13).
        try:
            for project_id, project in queue:  # grows while it is walked (design D8)
                full = self._back_up(project_id, project)
                for sub_id, sub in _archived_subprojects(full):
                    if sub_id not in handled:
                        handled.add(sub_id)
                        queue.append((sub_id, sub))
        except PlanRestricted:
            log.warning(
                "the Todoist plan does not allow fetching archived projects; they cannot be backed up on this plan"
            )
        finally:
            log.info(
                "archived projects: %d listed, %d snapshots fetched (%d new, %d again), %d parts filled in; "
                "%d comment files written",
                listed,
                self.new + self.again,
                self.new,
                self.again,
                self.filled,
                self.comment_files,
            )

    def _list(self) -> list[tuple[str, dict]]:
        """The listed archived projects with their IDs, in list order, each once."""
        listed: dict[str, dict] = {}
        for page in completed.fetch_pages(self.client, ARCHIVED_PROJECTS, {}, LIST_PAGE_LIMIT, "results"):
            for project in page["results"]:
                project_id = _object_id(project)
                if project_id is None:
                    raise ApiError(f"GET /{ARCHIVED_PROJECTS} returned a project without an id", http_status=200)
                listed.setdefault(project_id, project)
        return list(listed.items())

    def _back_up(self, project_id: str, project: dict) -> dict | None:
        """Bring the snapshot of a project up to date (design D4). Return its `/full` response, if any."""
        directory = snapshot_dir(self.target, project_id)
        stored = storage.read_bytes(directory / PROJECT_FILE)
        if stored is None or stored != storage.encode_json(project):
            full = self._fetch(project_id, project, directory)
            if full is not None:
                if stored is None:
                    self.new += 1
                else:
                    self.again += 1
            return full
        self._fill(project_id, project, directory)
        return _read_object(directory / FULL_FILE)

    # --- Fetching a snapshot (design D5) ---------------------------------------------------------

    def _fetch(self, project_id: str, project: dict, directory: Path) -> dict | None:
        """Fetch the snapshot of a project and write it in the order of design D3.

        Return the `/full` response, or None if the project is gone.
        """
        _check_directory(directory, project_id)
        full = self._full(project_id)
        if full is None:
            return None
        items = self._completed_items(project_id, project)
        if self.comments:
            stored_tasks = _stored_tasks(directory)
            fetched_tasks = [*_list_of(full, "tasks"), *_list_of(items, "items")]
            store = self._comment_store(directory)
            store.update({"items": stored_tasks} if stored_tasks else None, fetched_tasks, fetch_missing=True)
            self._count_comments(store)
            self._project_comments(project_id, directory)
        if items is not None:
            storage.write_bytes_atomic(directory / COMPLETED_FILE, storage.encode_json(items))
        storage.write_bytes_atomic(directory / FULL_FILE, storage.encode_json(full))
        # The checkpoint: it must not reach the disk before the files written above.
        storage.write_bytes_atomic(directory / PROJECT_FILE, storage.encode_json(project), full_sync=True)
        return full

    # --- Filling in missing parts (design D6) --------------------------------------------------

    def _fill(self, project_id: str, project: dict, directory: Path) -> None:
        """Add the parts that an earlier plan left out of a stored snapshot. The files tell which are missing."""
        if self.completed_tasks and not (directory / COMPLETED_FILE).exists():
            items = self._completed_items(project_id, project)
            if items is not None:
                # Its tasks' comments are left to the comments part below, which finds their files missing.
                storage.write_bytes_atomic(directory / COMPLETED_FILE, storage.encode_json(items))
                self.filled += 1
        if not self.comments:
            return
        # The comments part is complete when the project's comments and a file for each task with
        # comments are stored, since parts can be added to a snapshot while comments are not allowed.
        store = self._comment_store(directory)
        tasks = _stored_tasks(directory)
        if (directory / PROJECT_COMMENTS_FILE).exists() and not any(store.missing(task) for task in tasks):
            return
        # Compared with themselves, only the tasks whose comment file is missing are fetched.
        store.update({"items": tasks}, tasks, fetch_missing=True)
        self._count_comments(store)
        # Written last, so that a run ending early fetches the project's comments again.
        if self._project_comments(project_id, directory):
            self.filled += 1

    def _full(self, project_id: str) -> dict | None:
        path = full_path(project_id)
        try:
            body = self.client.get(path, {})
        except ApiError as exc:
            if exc.http_status != 404:
                raise
            log.debug("an archived project was not found (HTTP 404); it may have been deleted since it was listed")
            return None
        try:
            full = json.loads(body)
        except ValueError as exc:
            raise ApiError(f"GET /{path} returned invalid JSON: {exc}", http_status=200) from exc
        if not isinstance(full, dict):
            raise ApiError(f"GET /{path} returned a value that is not a JSON object", http_status=200)
        return full

    def _completed_items(self, project_id: str, project: dict) -> dict | None:
        """The project's completed tasks of every window, merged; None if the plan leaves them out."""
        if not self.completed_tasks:
            return None
        pages = []
        try:
            for since, until in self._windows(project):
                params = {"since": since, "until": until, "project_id": project_id}
                pages.extend(
                    completed.fetch_pages(
                        self.client, completed.BY_COMPLETION_DATE, params, completed.TASKS_PAGE_LIMIT, "items"
                    )
                )
        except PlanRestricted:
            log.warning(
                "the Todoist plan does not allow fetching completed tasks; the completed tasks of %s cannot "
                "be backed up on this plan",
                OWNER,
            )
            self.completed_tasks = False
            return None
        return completed.merge_pages(pages, "items", "completed_at")

    def _comment_store(self, directory: Path) -> "completed._CommentStore":
        return completed._CommentStore(
            directory / COMMENTS_DIR, self.client, enabled=True, owner=OWNER, empty_when_gone=True
        )

    def _count_comments(self, store: "completed._CommentStore") -> None:
        """Take over the plan flag and the count of written files from a comment store."""
        self.comments = store.enabled
        self.comment_files += store.written

    def _project_comments(self, project_id: str, directory: Path) -> bool:
        """Fetch the project's comments and merge them into its file. Return whether the file is stored."""
        if not self.comments:
            return False
        try:
            pages = completed.fetch_pages(
                self.client, completed.COMMENTS, {"project_id": project_id}, completed.COMMENTS_PAGE_LIMIT, "results"
            )
        except PlanRestricted:
            log.warning(
                "the Todoist plan does not allow fetching comments; the comments of %s cannot be backed up "
                "on this plan",
                OWNER,
            )
            self.comments = False
            return False
        path = directory / PROJECT_COMMENTS_FILE
        stored = completed.read_stored(path, "results")
        pages = [stored, *pages] if stored is not None else pages
        merged = completed.merge_pages(pages, "results", "posted_at", by_id=True)
        if storage.write_bytes_atomic(path, storage.encode_json(merged)):
            self.comment_files += 1
        return True

    # --- Windows over the project's lifetime (design D7) --------------------------------------

    def _windows(self, project: dict) -> Iterator[tuple[str, str]]:
        """Three-month windows from the project's first month until one contains its last month."""
        month = self._first_month(project)
        end = self._last_month(project)
        while True:
            last = month
            for _ in range(WINDOW_MONTHS - 1):
                last = completed.next_month(last)
            yield completed.month_window(month)[0], completed.month_window(last)[1]
            if last >= end:
                return
            month = completed.next_month(last)

    def _last_month(self, project: dict) -> str:
        """The month of the project's `updated_at`, which archiving moves forward, or the current month
        if that is earlier or `updated_at` is unknown. No task of an archived project can be completed."""
        current = completed.month_of(self.moment)
        try:
            updated = completed.month_of(completed.parse_time(project.get("updated_at")))
        except ValueError:
            return current
        return min(updated, current)

    def _first_month(self, project: dict) -> str:
        """The month of the project's `created_at`, else the account's first month of completed tasks."""
        try:
            return completed.month_of(completed.parse_time(project.get("created_at")))
        except ValueError:
            pass
        first = completed._checked_month(storage.completed_state(self.state)["first_month"], "first_month")
        return first or completed._first_month(self.target)


def _check_directory(directory: Path, project_id: str) -> None:
    """Fail instead of writing into the snapshot of another project (design D12).

    On a case-insensitive file system, project IDs that differ only in letter case share a directory.
    """
    for name, owner in ((PROJECT_FILE, lambda value: value), (FULL_FILE, lambda value: value.get("project"))):
        path = directory / name
        value = _read_object(path)
        stored_id = _object_id(owner(value)) if value is not None else None
        if stored_id is not None and stored_id != project_id:
            raise StorageError(
                f"{path} belongs to the project {stored_id!r}, not to the project {project_id!r} whose snapshot "
                "is to be stored there; on a case-insensitive file system two IDs that differ only in letter "
                "case share a directory. The file was not changed"
            )


def _read_object(path: Path) -> dict | None:
    """Read a stored JSON object, or return None if the file does not exist."""
    data = storage.read_bytes(path)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StorageError(f"{path} is not a valid JSON file: {exc}") from exc
    if not isinstance(value, dict):
        raise StorageError(f"{path} is not a JSON object")
    return value


def _archived_subprojects(full: dict | None) -> list[tuple[str, dict]]:
    """The entries of `subprojects` that are archived, with their IDs. The others are in the sync (design D8)."""
    found = []
    for sub in _list_of(full, "subprojects"):
        sub_id = _object_id(sub)
        if sub_id is not None and sub.get("is_archived") is True:
            found.append((sub_id, sub))
    return found


def _object_id(value) -> str | None:
    return completed._id_text(value.get("id")) if isinstance(value, dict) else None


def _stored_tasks(directory: Path) -> list:
    """The open and completed tasks stored in a snapshot."""
    return [
        *_list_of(_read_object(directory / FULL_FILE), "tasks"),
        *_list_of(completed.read_stored(directory / COMPLETED_FILE, "items"), "items"),
    ]


def _list_of(response: dict | None, key: str) -> list:
    """The list in `key` of a response, or an empty list if it has none."""
    tasks = response.get(key) if response is not None else None
    return tasks if isinstance(tasks, list) else []
