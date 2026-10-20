"""Building a SQLite database from a backup, for browsing it, for example in Datasette (design D1).

The command only reads the backup (design D2). It collects every stored copy of each project,
section, label, task and comment, keeps the current copy of each, and leaves out the objects that
count as deleted (design D4). Completions are taken from the completion records as they are. The
database is then rebuilt in one transaction in the existing file (design D5, D6).
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from todoist_takeout import archived, attachments, completed, storage, sync
from todoist_takeout.errors import ConfigError, ReadOnlyTarget, StorageError
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


def run(backup: Path, database: Path) -> "Built":
    """Build the database at `database` from the backup in `backup` (design D2)."""
    backup, database = Path(backup), Path(database)
    check_backup(backup)
    check_database(database)
    log.info("reading the backup in %s", backup)
    with _locked(backup):
        content = read_backup(backup)
    log.info("building %s", database)
    return build(database, content)


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
    # The stored file of each comment that has one, as a path relative to the backup, with `/`.
    attachment_paths: dict[str, str]


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

    current_comments = comments.current(deleted[NOTES] | deleted[PROJECT_NOTES])
    return Content(
        projects=projects.current(deleted[PROJECTS]),
        sections=sections.current(deleted[SECTIONS]),
        labels=labels.current(deleted[LABELS]),
        tasks=tasks.current(deleted[ITEMS]),
        comments=current_comments,
        completions=_read_completions(backup),
        attachment_paths=_attachment_paths(backup, current_comments),
    )


def _attachment_paths(backup: Path, comments: dict[str, dict]) -> dict[str, str]:
    """The stored file of each comment with an uploaded file, where `attachments/<id>/` holds one (design D5)."""
    root = backup / attachments.ATTACHMENTS_DIR
    # Compared with the listed names, so that a directory whose name differs only in letter case,
    # which a case-insensitive file system would open as well, is not taken for the comment's own.
    names = set(attachments._listdir(root))
    paths = {}
    for comment_id, comment in comments.items():
        try:
            stem = storage.encode_key(comment_id)
        except StorageError:
            continue  # an ID too long for a directory name, whose file cannot be stored
        if stem not in names or attachments.uploaded_file(comment) is None:
            continue
        files = sorted(
            name
            for name in attachments._listdir(root / stem)
            if not storage.is_temp_name(name) and (root / stem / name).is_file()
        )
        if files:
            paths[comment_id] = f"{attachments.ATTACHMENTS_DIR}/{stem}/{files[0]}"
    return paths


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


# --- The database target (design D7) ------------------------------------------------------------

# `PRAGMA application_id` of the tool's databases: the ASCII bytes `TdTk`.
APPLICATION_ID = 0x5464546B
# `PRAGMA user_version`: the version of the schema below.
SCHEMA_VERSION = 1
# How long a rebuild waits, in seconds, for another program to release the database (design D6).
BUSY_TIMEOUT = 10.0

_SQLITE_HEADER = b"SQLite format 3\x00"
_HEADER_SIZE = 100
_APPLICATION_ID_OFFSET = 68


def check_database(database: Path) -> bool:
    """Return whether `database` is a new database: a missing or empty file.

    Raises ConfigError for a directory, a file that is not a SQLite database, and a SQLite database
    that the tool did not build. Only the header is read, without opening the file with SQLite,
    which could roll back a hot journal and so change it.
    """
    if database.is_dir():
        raise ConfigError(f"the database path {database} is a directory")
    try:
        with open(database, "rb") as f:
            header = f.read(_HEADER_SIZE)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ConfigError(f"cannot read the database file {database}: {exc.strerror or exc}") from exc
    if not header:
        return True
    if len(header) < _HEADER_SIZE or not header.startswith(_SQLITE_HEADER):
        raise ConfigError(f"{database} is not a SQLite database; it was left unchanged. Choose another database path")
    application_id = int.from_bytes(header[_APPLICATION_ID_OFFSET : _APPLICATION_ID_OFFSET + 4], "big")
    if application_id != APPLICATION_ID:
        raise ConfigError(
            f"{database} is a SQLite database that todoist-takeout did not build; it was left unchanged. "
            "Choose another database path"
        )
    return False


# --- The schema (design D5) ---------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # TEXT or INTEGER
    description: str
    references: str | None = None  # the table whose `id` the column refers to


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]
    rows: Callable[["Content"], Iterable[tuple]]  # the rows, in column order and in order of the key
    primary_key: tuple[str, ...] = ("id",)
    search: tuple[str, ...] = ()  # the columns of the full-text search table `<name>_fts` (design D10)

    def create_statement(self) -> str:
        columns = [
            f"{c.name} {c.type}"
            + (" NOT NULL" if c.name in self.primary_key else "")
            + (f" REFERENCES {c.references} (id)" if c.references else "")
            for c in self.columns
        ]
        return f"CREATE TABLE {self.name} ({', '.join(columns)}, PRIMARY KEY ({', '.join(self.primary_key)}))"

    @property
    def search_table(self) -> str:
        return f"{self.name}_fts"


OPEN = "open"
COMPLETED = "completed"
# The API's `priority` 4 is the highest, which Todoist shows as p1.
_PRIORITIES = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}


def _project_rows(content: Content) -> Iterator[tuple]:
    for project_id, project in content.projects.items():
        yield (
            project_id,
            _text(project.get("name")),
            _text(project.get("description")),
            completed._id_text(project.get("parent_id")),
            _flag(project.get("is_archived")),
            _flag(project.get("is_favorite")),
            _text(project.get("color")),
            _text(project.get("created_at")),
            _text(project.get("updated_at")),
        )


def _section_rows(content: Content) -> Iterator[tuple]:
    for section_id, section in content.sections.items():
        yield (
            section_id,
            _text(section.get("name")),
            completed._id_text(section.get("project_id")),
            _flag(section.get("is_archived")),
            _text(section.get("added_at")),
            _text(section.get("archived_at")),
        )


def _label_rows(content: Content) -> Iterator[tuple]:
    for label_id, label in content.labels.items():
        yield (label_id, _text(label.get("name")), _text(label.get("color")), _flag(label.get("is_favorite")))


def _task_rows(content: Content) -> Iterator[tuple]:
    for task_id, task in content.tasks.items():
        due = _object(task.get("due"))
        priority = task.get("priority")
        yield (
            task_id,
            _text(task.get("content")),
            _text(task.get("description")),
            completed._id_text(task.get("project_id")),
            completed._id_text(task.get("section_id")),
            completed._id_text(task.get("parent_id")),
            COMPLETED if task.get("checked") is True else OPEN,
            _PRIORITIES.get(priority) if completed._is_int(priority) else None,
            _text(due.get("date")),
            _text(due.get("string")),
            _flag(due.get("is_recurring")),
            _text(_object(task.get("deadline")).get("date")),
            _text(task.get("added_at")),
            _text(task.get("completed_at")),
            _text(task.get("updated_at")),
        )


def _task_label_rows(content: Content) -> Iterator[tuple]:
    # A label name links to the stored label of that name, the first by ID if several have it.
    label_ids: dict[str, str] = {}
    for label_id, label in content.labels.items():
        name = label.get("name")
        if isinstance(name, str):
            label_ids.setdefault(name, label_id)
    for task_id, task in content.tasks.items():
        names = task.get("labels")
        if not isinstance(names, list):
            continue
        for name in dict.fromkeys(name for name in names if isinstance(name, str) and name):
            yield (task_id, name, label_ids.get(name))


def _comment_rows(content: Content) -> Iterator[tuple]:
    for comment_id, comment in content.comments.items():
        attachment = _object(comment.get("file_attachment"))
        task_id = comment.get("item_id")
        if task_id is None:
            task_id = comment.get("task_id")
        is_link = attachment.get("resource_type") in attachments.LINK_TYPES
        yield (
            comment_id,
            completed._id_text(task_id),
            completed._id_text(comment.get("project_id")),
            _text(comment.get("content")),
            _text(comment.get("posted_at")),
            _text(attachment.get("file_name")),
            content.attachment_paths.get(comment_id),
            _text(attachment.get("url")) if is_link else None,
        )


def _completion_rows(content: Content) -> Iterator[tuple]:
    for c in content.completions.values():
        yield (c.id, c.task_id, c.content, c.project_id, c.completed_at, c.source)


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _flag(value) -> int:
    return 1 if value is True else 0


TABLES = (
    Table(
        "projects",
        "Every project that is not deleted, archived projects included, each from its copy with the newest "
        "updated_at; on a tie, the copy in the snapshot of an archived project.",
        (
            Column("id", "TEXT", "The project's ID."),
            Column("name", "TEXT", "The project's name."),
            Column("description", "TEXT", "The project's description."),
            Column("parent_id", "TEXT", "The parent project of a sub-project.", "projects"),
            Column("is_archived", "INTEGER", "1 if the project is archived, else 0."),
            Column("is_favorite", "INTEGER", "1 if the project is a favorite, else 0."),
            Column("color", "TEXT", "The name of the project's color, such as grey."),
            Column("created_at", "TEXT", "When the project was created, in UTC."),
            Column("updated_at", "TEXT", "When the project last changed, in UTC."),
        ),
        _project_rows,
    ),
    Table(
        "sections",
        "Every section that is not deleted, of active and of archived projects.",
        (
            Column("id", "TEXT", "The section's ID."),
            Column("name", "TEXT", "The section's name."),
            Column("project_id", "TEXT", "The project of the section.", "projects"),
            Column("is_archived", "INTEGER", "1 if the section is archived, else 0."),
            Column("added_at", "TEXT", "When the section was created, in UTC."),
            Column("archived_at", "TEXT", "When the section was archived, in UTC."),
        ),
        _section_rows,
    ),
    Table(
        "labels",
        "The personal labels that are not deleted. Shared labels are only in task_labels.",
        (
            Column("id", "TEXT", "The label's ID."),
            Column("name", "TEXT", "The label's name."),
            Column("color", "TEXT", "The name of the label's color."),
            Column("is_favorite", "INTEGER", "1 if the label is a favorite, else 0."),
        ),
        _label_rows,
    ),
    Table(
        "tasks",
        "Every task that is not deleted, open and completed, each from its current copy in the backup.",
        (
            Column("id", "TEXT", "The task's ID."),
            Column("content", "TEXT", "The task's name."),
            Column("description", "TEXT", "The task's description."),
            Column("project_id", "TEXT", "The project of the task.", "projects"),
            Column("section_id", "TEXT", "The section of the task, if any.", "sections"),
            Column("parent_id", "TEXT", "The parent task of a sub-task.", "tasks"),
            Column("status", "TEXT", "open or completed."),
            Column("priority", "TEXT", "The priority as Todoist shows it, from p1 (highest) to p4."),
            Column("due_date", "TEXT", "The due date, or date and time, of the next occurrence."),
            Column("due_string", "TEXT", "The due date as it was entered, such as every monday."),
            Column("is_recurring", "INTEGER", "1 if the task recurs, else 0."),
            Column("deadline_date", "TEXT", "The deadline."),
            Column("added_at", "TEXT", "When the task was created, in UTC."),
            Column("completed_at", "TEXT", "When the task was completed, in UTC."),
            Column("updated_at", "TEXT", "When the task last changed, in UTC."),
        ),
        _task_rows,
        search=("content", "description"),
    ),
    Table(
        "task_labels",
        "One row per label of a task, also for a shared label that is not in labels.",
        (
            Column("task_id", "TEXT", "The task.", "tasks"),
            Column("label_name", "TEXT", "The label's name, as the task holds it."),
            Column("label_id", "TEXT", "The personal label of that name, if there is one.", "labels"),
        ),
        _task_label_rows,
        primary_key=("task_id", "label_name"),
    ),
    Table(
        "comments",
        "Every comment that is not deleted, of tasks and of projects.",
        (
            Column("id", "TEXT", "The comment's ID."),
            Column("task_id", "TEXT", "The task of a task comment.", "tasks"),
            Column("project_id", "TEXT", "The project of a project comment.", "projects"),
            Column("content", "TEXT", "The comment's text."),
            Column("posted_at", "TEXT", "When the comment was posted, in UTC."),
            Column("attachment_name", "TEXT", "The name of the attached file."),
            Column(
                "attachment_path",
                "TEXT",
                "Where the attached file is stored, relative to the backup directory; none if it is not stored.",
            ),
            Column("attachment_url", "TEXT", "The URL of an attached link."),
        ),
        _comment_rows,
        search=("content",),
    ),
    Table(
        "completions",
        "One row per completion of a task, every completion of a recurring task included, also of tasks deleted since.",
        (
            Column("id", "TEXT", "The ID of the completion record."),
            Column("task_id", "TEXT", "The completed task.", "tasks"),
            Column("content", "TEXT", "The task's name when it was completed."),
            Column("project_id", "TEXT", "The task's project when it was completed.", "projects"),
            Column("completed_at", "TEXT", "When the task was completed, in UTC."),
            Column(
                "source",
                "TEXT",
                "activity for the activity log, backfill for the completions from before the first backup.",
            ),
        ),
        _completion_rows,
    ),
)


# --- The rebuild (design D6, D10) ---------------------------------------------------------------


@dataclass(frozen=True)
class Built:
    """The result of a rebuild: the number of rows per table, and whether full-text search is set up."""

    rows: dict[str, int]
    search: bool


def build(database: Path, content: Content) -> Built:
    """Replace the whole content of `database` with `content` in one transaction (design D6).

    Raises ConfigError if `database` may not be used (design D7), and StorageError if the rebuild
    failed, which leaves the previous content.
    """
    new = check_database(database)  # again: the file may have changed while the backup was read
    try:
        storage._ensure_dir(database.parent)
    except OSError as exc:
        raise StorageError(f"cannot create the directory of the database {database}: {exc.strerror or exc}") from exc
    try:
        con = sqlite3.connect(database, isolation_level=None, timeout=BUSY_TIMEOUT)
    except sqlite3.Error as exc:
        raise StorageError(f"cannot open the database {database}: {exc}") from exc
    try:
        try:
            return _rebuild(con, content, new)
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise StorageError(f"cannot build the database {database}, which keeps its previous content: {exc}") from exc
    finally:
        con.close()


def _rebuild(con: sqlite3.Connection, content: Content, new: bool) -> Built:
    if new:
        # On a new database, auto_vacuum takes effect only as the first statement of a deferred
        # transaction: in BEGIN IMMEDIATE it is ignored, and outside a transaction it writes a
        # header without the application ID, which a failed run would leave behind.
        con.execute("BEGIN")
        con.execute("PRAGMA auto_vacuum = FULL")
    else:
        con.execute("BEGIN IMMEDIATE")
    _drop_schema(con)
    rows = {}
    for table in TABLES:
        con.execute(table.create_statement())
        rows[table.name] = _insert(con, table, table.rows(content))
    search = _create_search(con)
    con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.execute("COMMIT")
    return Built(rows, search)


def _drop_schema(con: sqlite3.Connection) -> None:
    """Drop every trigger, view, table and index, of this or any older schema.

    Virtual tables go before the other tables, as they drop their shadow tables with them.
    """
    order = {"trigger": 0, "view": 1, "table": 3, "index": 4}
    entries = sorted(
        (2 if kind == "table" and (sql or "").upper().startswith("CREATE VIRTUAL") else order[kind], kind, name)
        for kind, name, sql in con.execute("SELECT type, name, sql FROM sqlite_master")
        if kind in order and not name.startswith("sqlite_")
    )
    for _rank, kind, name in entries:
        con.execute(f"DROP {kind.upper()} IF EXISTS {_quote(name)}")


def _insert(con: sqlite3.Connection, table: Table, rows: Iterable[tuple]) -> int:
    rows = list(rows)
    con.executemany(f"INSERT INTO {table.name} VALUES ({', '.join('?' * len(table.columns))})", rows)
    return len(rows)


def _create_search(con: sqlite3.Connection) -> bool:
    """Create and fill the full-text search tables, or none if SQLite lacks FTS5 (design D10)."""
    for table in TABLES:
        if not table.search:
            continue
        columns = ", ".join(table.search)
        try:
            con.execute(
                f"CREATE VIRTUAL TABLE {table.search_table} USING fts5({columns}, content='{table.name}', "
                "content_rowid='rowid')"
            )
        except sqlite3.OperationalError as exc:
            if not str(exc).startswith("no such module"):
                raise
            log.warning("the database is built without full-text search, which this Python's SQLite lacks (%s)", exc)
            return False
        con.execute(f"INSERT INTO {table.search_table} (rowid, {columns}) SELECT rowid, {columns} FROM {table.name}")
    return True


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
