"""Building a SQLite database from a backup, for browsing it, for example in Datasette (design D1).

The command only reads the backup (design D2). It collects every stored copy of each project,
section, label, task and comment, keeps the current copy of each, and leaves out the objects that
count as deleted (design D4). Completions are taken from the completion records as they are.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from todoist_takeout import archived, attachments, completed, storage, sync
from todoist_takeout.errors import ConfigError, ReadOnlyTarget
from todoist_takeout.lock import TargetLock

log = logging.getLogger(__name__)

# The resource directories of `sync/` that the database shows.
ITEMS = "items"
PROJECTS = "projects"
SECTIONS = "sections"
LABELS = "labels"
NOTES = "notes"
PROJECT_NOTES = "project_notes"

# The `event_type` of the activity log's completion events.
COMPLETED_EVENT = "item:completed"
# The `source` of a completion.
ACTIVITY = "activity"
BACKFILL = "backfill"

_MONTH_FILE = re.compile(r"\d{4}-\d{2}\.json")


def run(backup: Path, database: Path) -> "Content":
    """Read the backup in `backup` for the database at `database`."""
    backup = Path(backup)
    check_backup(backup)
    log.info("reading the backup in %s", backup)
    with _locked(backup):
        return read_backup(backup)


def check_backup(backup: Path) -> dict:
    """Return the state of the backup in `backup`, without writing to it.

    Raises ConfigError if `backup` is not a backup in a format version the tool supports.
    """
    if not backup.is_dir():
        problem = "does not exist" if not backup.exists() else "is not a directory"
        raise ConfigError(f"the backup directory {backup} {problem}")
    state = storage.read_state(backup)
    if state is None:
        raise ConfigError(f"{backup} is not a todoist-takeout backup (it has no {storage.STATE_FILE})")
    return state


@contextmanager
def _locked(backup: Path) -> Iterator[None]:
    """Hold the backup's lock, or none if the backup is on a read-only file system (design D2)."""
    lock = TargetLock(backup)
    try:
        lock.acquire()
    except ReadOnlyTarget as exc:
        # No backup run can be writing to a backup whose lock file cannot be opened for writing.
        log.warning("%s; the backup is read without the lock", exc)
        yield
        return
    try:
        yield
    finally:
        lock.release()


# --- Reading the backup (design D4) -------------------------------------------------------------


@dataclass(frozen=True)
class Completion:
    """One completion of a task, from the activity log or the backfill."""

    id: str
    task_id: str | None
    content: str | None
    project_id: str | None
    completed_at: str | None
    source: str


@dataclass
class Content:
    """The current copy of each object that is not deleted, and the completions, each by ID in ID order."""

    projects: dict[str, dict]
    sections: dict[str, dict]
    labels: dict[str, dict]
    tasks: dict[str, dict]
    comments: dict[str, dict]
    completions: dict[str, Completion]


# A missing or invalid time, which is older than any valid one.
_NO_TIME = (False, datetime.min.replace(tzinfo=UTC))
# The tie-breaker of the copies that are not in a snapshot.
_NO_SNAPSHOT = (_NO_TIME, "")


class _Copies:
    """The copies of one type of object, keeping the current one per ID.

    A copy with a higher key wins: the newest `updated_at` if the type is `timed`, then the rank of
    its place, then its snapshot's tie-breaker. On equal keys, the copy offered first stays.
    """

    def __init__(self, *, timed: bool):
        self.timed = timed
        self._best: dict[str, tuple[tuple, dict]] = {}

    def offer(self, obj, rank: int, snapshot: tuple = _NO_SNAPSHOT) -> None:
        object_id = completed._id_text(obj.get("id")) if isinstance(obj, dict) else None
        if object_id is None:
            return
        key = (_time_key(obj.get("updated_at")) if self.timed else _NO_TIME, rank, snapshot)
        current = self._best.get(object_id)
        if current is None or key > current[0]:
            self._best[object_id] = (key, obj)

    def current(self, deleted: set[str]) -> dict[str, dict]:
        """The current copies of the objects whose IDs are not in `deleted`."""
        return {object_id: obj for object_id, (_key, obj) in sorted(self._best.items()) if object_id not in deleted}


def _time_key(value) -> tuple:
    # Compared as instants, not as text.
    try:
        return (True, completed.parse_time(value))
    except ValueError:
        return _NO_TIME


def read_backup(backup: Path) -> Content:
    """Collect the copies from every place of design D4, and keep the current copy of each object.

    Names that start with `.` and the `pending/` directory are not read.
    """
    backup = Path(backup)
    tasks = _Copies(timed=True)
    projects = _Copies(timed=True)
    sections = _Copies(timed=True)
    comments = _Copies(timed=False)
    labels = _Copies(timed=False)

    # Ranks, higher first on ties: tasks archived/completed.json 3, archived/full.json 2, completed/ 1,
    # sync/ 0; projects archived/project.json 2, archived/full.json 1, sync/ 0; sections archived/ 1,
    # sync/ 0; comments archived/comments/ 4, archived/project_comments.json 3, completed/comments/ 2,
    # sync/notes/ 1, sync/project_notes/ 0.
    deleted = {}
    for resource, copies, rank in [
        (ITEMS, tasks, 0),
        (PROJECTS, projects, 0),
        (SECTIONS, sections, 0),
        (LABELS, labels, 0),
        (NOTES, comments, 1),
        (PROJECT_NOTES, comments, 0),
    ]:
        live, deleted[resource] = _read_sync(backup, resource)
        for obj in live:
            copies.offer(obj, rank)

    completed_dir = backup / completed.COMPLETED_DIR
    for path in attachments._json_files(completed_dir):
        if _MONTH_FILE.fullmatch(path.name):
            for task in _entries(path, "items"):
                tasks.offer(task, 1)
    for path in attachments._json_files(completed_dir / completed.COMMENTS_DIR):
        for comment in _entries(path, "results"):
            comments.offer(comment, 2)

    for directory in attachments._subdirectories(backup / archived.ARCHIVED_DIR):
        if directory.name.startswith("."):
            continue
        project = archived._read_object(directory / archived.PROJECT_FILE)
        full = archived._read_object(directory / archived.FULL_FILE)
        # Between two snapshots, the one whose project.json is newer wins, then the one whose name sorts last.
        snapshot = (_time_key(project.get("updated_at") if project is not None else None), directory.name)
        if project is not None:
            projects.offer(project, 2, snapshot)
        for sub in archived._list_of(full, "subprojects"):
            projects.offer(sub, 1, snapshot)
        for section in archived._list_of(full, "sections"):
            sections.offer(section, 1, snapshot)
        for task in archived._list_of(full, "tasks"):
            tasks.offer(task, 2, snapshot)
        for task in _entries(directory / archived.COMPLETED_FILE, "items"):
            tasks.offer(task, 3, snapshot)
        for path in attachments._json_files(directory / archived.COMMENTS_DIR):
            for comment in _entries(path, "results"):
                comments.offer(comment, 4, snapshot)
        for comment in _entries(directory / archived.PROJECT_COMMENTS_FILE, "results"):
            comments.offer(comment, 3, snapshot)

    return Content(
        projects=projects.current(deleted[PROJECTS]),
        sections=sections.current(deleted[SECTIONS]),
        labels=labels.current(deleted[LABELS]),
        tasks=tasks.current(deleted[ITEMS]),
        comments=comments.current(deleted[NOTES] | deleted[PROJECT_NOTES]),
        completions=_read_completions(backup),
    )


def _read_sync(backup: Path, resource: str) -> tuple[list, set[str]]:
    """The live objects stored in `sync/<resource>/`, and the IDs of the objects that count as deleted.

    A deletion payload is never a copy: neither a tombstone nor a `<key>.json` that holds one.
    """
    directory = backup / sync.SYNC_DIR / storage.encode_key(resource)
    live, deleted = [], set()
    for path in attachments._json_files(directory):
        if path.name.endswith(sync.DELETED_SUFFIX):
            object_id = _key_id(path.name[: -len(sync.DELETED_SUFFIX)])
        else:
            value = attachments._read_json(path)
            if not sync._is_deleted(value):
                live.append(value)
                continue
            object_id = _key_id(path.name[: -len(sync.JSON_SUFFIX)])
        if object_id is not None:
            deleted.add(object_id)
    return live, deleted


def _key_id(stem: str) -> str | None:
    """The ID that a file name of `sync/` encodes, or None for a name the tool does not write."""
    try:
        key = storage.decode_key(stem)
    except ValueError:
        return None
    return key[0] if len(key) == 1 else None


def _entries(path: Path, list_key: str) -> list:
    """The entries of a response-shaped file, or none if it does not exist."""
    stored = completed.read_stored(path, list_key)
    return stored[list_key] if stored is not None else []


def _read_completions(backup: Path) -> dict[str, Completion]:
    """The completions of `completed/activity/` and `completed/backfill/`, deleted tasks included."""
    found: dict[str, Completion] = {}
    completed_dir = backup / completed.COMPLETED_DIR
    for path in attachments._json_files(completed_dir / completed.ACTIVITY_DIR):
        for event in _entries(path, "results"):
            if not isinstance(event, dict) or event.get("event_type") != COMPLETED_EVENT:
                continue
            extra = event.get("extra_data")
            extra = extra if isinstance(extra, dict) else {}
            _add(
                found,
                event.get("id"),
                task_id=completed._id_text(event.get("object_id")),
                content=_text(extra.get("content")),
                project_id=completed._id_text(event.get("parent_project_id")),
                completed_at=_text(event.get("event_date")),
                source=ACTIVITY,
            )
    for path in attachments._json_files(completed_dir / completed.BACKFILL_DIR):
        for entry in _entries(path, "items"):
            if not isinstance(entry, dict):
                continue
            task = entry.get("item_object")
            content = entry.get("content")
            if content is None and isinstance(task, dict):
                content = task.get("content")
            _add(
                found,
                entry.get("id"),
                task_id=completed._id_text(entry.get("task_id")),
                content=_text(content),
                project_id=completed._id_text(entry.get("project_id")),
                completed_at=_text(entry.get("completed_at")),
                source=BACKFILL,
            )
    return dict(sorted(found.items()))


def _add(found: dict[str, Completion], raw_id, **fields) -> None:
    # An ID that Python's json parsed as an integer, however large, is written in decimal, exactly.
    completion_id = completed._id_text(raw_id)
    if completion_id is not None:
        found.setdefault(completion_id, Completion(id=completion_id, **fields))


def _text(value) -> str | None:
    return value if isinstance(value, str) else None
