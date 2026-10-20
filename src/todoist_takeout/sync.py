"""Running a backup (design D10) and applying sync responses to it (design D8).

Every object of a response is written as its own file under `sync/`, unchanged. Deleted objects
become tombstones. The only file ever removed under `sync/` is the tombstone of an object that the
sync returns as live again.
"""

import importlib.resources
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from todoist_takeout import archived, attachments, completed, resources, storage
from todoist_takeout.api import Client
from todoist_takeout.errors import ConfigError, RunError, StorageError
from todoist_takeout.lock import LOCK_FILE, TargetLock
from todoist_takeout.resources import META, SINGLE, Classifier, Kind

log = logging.getLogger(__name__)

SYNC_DIR = "sync"
JSON_SUFFIX = ".json"
DELETED_SUFFIX = ".deleted.json"
PENDING_DIR = "pending"
PENDING_FILE = "sync-response.json"
README_FILE = "README.md"
PLAN_LIMITS = "user_plan_limits"

# Resource directories whose objects can leave the sync by being completed or archived (design D9).
PROJECTS = "projects"
ITEMS = "items"
SECTIONS = "sections"
NOTES = "notes"

# A full sync is followed by one incremental sync, so a run makes at most this many requests.
MAX_ROUNDS = 2

# Flags in `user_plan_limits.current` that, when false, mean that the plan leaves a part of the
# account out of the backup (design D12), and the part each one names.
PLAN_FEATURES = {
    "comments": "task and project comments (including those of archived projects)",
    "completed_tasks": "completed tasks, completion events and their comments (including those of archived projects)",
}


@dataclass
class ApplyStats:
    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    missing: int = 0

    def add(self, other: "ApplyStats") -> None:
        self.written += other.written
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.missing += other.missing


# --- Run sequence ------------------------------------------------------------


def run_backup(target: Path, client: Client, *, now: Callable[[], datetime] | None = None) -> ApplyStats:
    """Bring the backup in `target` up to date with the account, and return what changed.

    Every step can be interrupted: the next run resumes from the last checkpoint and applies a
    downloaded response that was not checkpointed yet instead of requesting it again.
    """
    target = Path(target)
    now = now or _utc_now
    prepare_target(target)
    with TargetLock(target):
        state = storage.load_or_create_state(target)  # again, under the lock
        removed = storage.remove_temp_files(target)
        if removed:
            log.info("removed %d temporary files left by an interrupted run", removed)

        classifier = Classifier()
        stats = ApplyStats()
        pending = target / PENDING_DIR / PENDING_FILE
        body = storage.read_bytes(pending)
        if body is not None:
            log.info("applying the sync response that an interrupted run downloaded")
            _apply_pending(target, state, body, classifier, stats, now)

        for _ in range(MAX_ROUNDS):
            token = state.get("sync_token")
            if token:
                log.info("requesting an incremental sync")
            else:
                log.info("requesting a full sync of the account")
            body = client.sync(token or "*")
            storage.write_bytes_atomic(pending, body)
            if not _apply_pending(target, state, body, classifier, stats, now):
                break
            log.info("following the full sync with an incremental sync to catch up")

        completed.run(target, client, state, now)
        archived.run(target, client, state, now)
        attachments.run(target, client, now)
        _warn_about_plan_limits(target)
        state["last_successful_run_utc"] = _timestamp(now())
        storage.write_state(target, state)
    return stats


def prepare_target(target: Path) -> None:
    """Make sure `target` is a directory that holds a backup, creating it and `state.json` if needed.

    A directory that is rejected is left unchanged. Raises ConfigError.
    """
    if target.exists() and not target.is_dir():
        raise ConfigError(f"the target {target} is not a directory")
    try:
        target.mkdir(parents=True, exist_ok=True)
        names = [p.name for p in target.iterdir()]
    except OSError as exc:
        raise ConfigError(f"cannot use the target directory {target}: {exc.strerror or exc}") from exc
    if storage.STATE_FILE not in names and not all(_left_by_first_run(name) for name in names):
        raise ConfigError(
            f"the target directory {target} is not empty and is not a todoist-takeout backup "
            f"(it has no {storage.STATE_FILE}); choose a new or empty directory"
        )
    # Before any other file, so that the directory is recognized as a backup from now on.
    storage.load_or_create_state(target)


def _left_by_first_run(name: str) -> bool:
    # A first run killed while it created state.json, or just after it took the lock.
    return name == LOCK_FILE or (name.startswith(f".{storage.STATE_FILE}.") and name.endswith(".tmp"))


def _apply_pending(target: Path, state: dict, body: bytes, classifier: Classifier, stats: ApplyStats, now) -> bool:
    """Apply the response saved in `pending/`, checkpoint it, then remove it. Return whether it was a full sync."""
    pending = target / PENDING_DIR / PENDING_FILE
    response = _parse_response(pending, body)
    full = response.get("full_sync") is True
    applied = apply_response(target, response, classifier)
    stats.add(applied)
    log.info(
        "applied the %s: %d objects written, %d unchanged, %d marked deleted",
        "full sync" if full else "incremental sync",
        applied.written,
        applied.unchanged,
        applied.deleted,
    )
    storage.write_bytes_atomic(target / README_FILE, format_description())
    state["sync_token"] = response["sync_token"]
    if full:
        date = response.get("full_sync_date_utc")
        state["last_full_sync_utc"] = date if isinstance(date, str) else _timestamp(now())
    storage.write_state(target, state)
    storage.remove_file(pending)
    return full


def _parse_response(pending: Path, body: bytes) -> dict:
    try:
        response = json.loads(body)
    except ValueError as exc:
        problem = f"is not valid JSON ({exc})"
    else:
        if isinstance(response, dict) and isinstance(response.get("sync_token"), str):
            return response
        problem = "has no sync_token"
    # Nothing was taken from it, so the next run can simply request it again.
    storage.remove_file(pending)
    raise RunError(f"the sync response {problem}; the backup was not changed")


def format_description() -> bytes:
    """The format description that every backup holds as README.md, with LF line endings."""
    text = importlib.resources.files("todoist_takeout").joinpath("storage_format.md").read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").encode("utf-8")


def stored_value(target: Path, resource: str):
    """Return the stored value of a single-value resource, such as `user`, or None if there is none."""
    path = Path(target) / SYNC_DIR / f"{storage.encode_key(resource)}{JSON_SUFFIX}"
    data = storage.read_bytes(path)
    return None if data is None else _parse_stored(path, data)


def current_plan_limits(target: Path) -> dict:
    """The stored `user_plan_limits.current`, or an empty dict if the backup has none."""
    limits = stored_value(target, PLAN_LIMITS)
    current = limits.get("current") if isinstance(limits, dict) else None
    return current if isinstance(current, dict) else {}


def _warn_about_plan_limits(target: Path) -> None:
    current = current_plan_limits(target)
    if not current:
        return
    plan = current.get("plan_name")
    plan = f"Todoist plan {plan!r}" if isinstance(plan, str) else "Todoist plan"
    for flag, part in PLAN_FEATURES.items():
        if current.get(flag) is False:
            log.warning("the %s does not include %s; %s cannot be backed up on this plan", plan, flag, part)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Applying a response -----------------------------------------------------


def apply_response(target: Path, response: dict, classifier: Classifier) -> ApplyStats:
    """Store every resource of a parsed sync response under `target/sync/`."""
    sync_dir = Path(target) / SYNC_DIR
    full = response.get("full_sync") is True
    stats = ApplyStats()
    stored_live, left_out = _stored_live_keys(sync_dir) if full else ({}, {})

    for key, value in response.items():
        kind = classifier.classify(key, value)
        if kind.name == META:
            continue
        name = storage.encode_key(key)
        if not full and _is_empty(value):
            continue  # an empty or missing resource in an incremental sync means "no change"
        if kind.name == SINGLE:
            _count(stats, storage.write_bytes_atomic(sync_dir / f"{name}{JSON_SUFFIX}", storage.encode_json(value)))
            continue

        directory = sync_dir / name
        seen = set()
        for obj in value:
            object_key = resources.object_key(kind, obj)
            stem = storage.encode_key(object_key)
            seen.add(stem)
            _apply_object(directory, stem, object_key, kind, obj, stats)
        if full and name in stored_live:
            stored_live[name] -= seen
            left_out[name] -= seen

    if full:
        stats.missing = sum(len(stems) for stems in stored_live.values())
        if stats.missing:
            log.warning(
                "%d stored objects were not in the full sync; they may have been deleted in Todoist "
                "and are kept unchanged",
                stats.missing,
            )
        expected = sum(len(stems) for stems in left_out.values())
        if expected:
            log.info(
                "%d stored objects that were completed or archived were not in the full sync, as expected",
                expected,
            )
    return stats


def _apply_object(directory: Path, stem: str, key: tuple[str, ...], kind: Kind, obj, stats: ApplyStats) -> None:
    path = directory / f"{stem}{JSON_SUFFIX}"
    data = storage.encode_json(obj)
    live = not _is_deleted(obj)
    stored = storage.read_bytes(path)
    if stored == data:
        # A run interrupted between writing a restored object and removing its tombstone
        # left the tombstone behind; remove it now.
        _count(stats, live and _restore(directory, stem, key, kind))
        return
    stored_value = None
    if stored is not None:
        stored_value = _parse_stored(path, stored)
        _check_key(path, stored_value, key, kind)

    if live:
        written = storage.write_bytes_atomic(path, data)
        _count(stats, _restore(directory, stem, key, kind) or written)
        return

    # A deletion payload. Keep the last live content: Todoist's deletion payloads have every field
    # but emptied values, so they never replace a live object (design D8).
    if stored_value is None or _is_deleted(stored_value):
        written = storage.write_bytes_atomic(path, data)
    else:
        tombstone = directory / f"{stem}{DELETED_SUFFIX}"
        existing = storage.read_bytes(tombstone)
        if existing is not None and existing != data:
            _check_key(tombstone, _parse_stored(tombstone, existing), key, kind)
        written = storage.write_bytes_atomic(tombstone, data)
    if written:
        stats.deleted += 1
    else:
        stats.unchanged += 1


def _restore(directory: Path, stem: str, key: tuple[str, ...], kind: Kind) -> bool:
    """Remove the tombstone of an object that the sync returned as live. Return whether one existed.

    Call it only after `<key>.json` holds the live payload, so that a crash never leaves the
    object without its tombstone and without its new content.
    """
    tombstone = directory / f"{stem}{DELETED_SUFFIX}"
    existing = storage.read_bytes(tombstone)
    if existing is None:
        return False
    _check_key(tombstone, _parse_stored(tombstone, existing), key, kind)
    return storage.remove_file(tombstone)


def _check_key(path: Path, stored_value, key: tuple[str, ...], kind: Kind) -> None:
    """Fail instead of replacing a file that holds a different object (design D7).

    On a case-insensitive file system, keys that differ only in letter case share a file.
    """
    stored_key = resources.object_key(kind, stored_value)
    if stored_key != key:
        raise StorageError(
            f"{path} holds the object with key {_show(stored_key)}, which is not the key "
            f"{_show(key)} of the object to store there; on a case-insensitive file system two keys "
            "that differ only in letter case share a file. The file was not changed"
        )


def _show(key) -> str:
    if key is None:
        return "(none)"
    return "+".join(repr(part) for part in key)


def _parse_stored(path: Path, data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StorageError(f"{path} is not a valid JSON file: {exc}") from exc


# The fields of a stored object that decide whether a full sync leaves it out (design D9).
_LEFT_OUT_FIELDS = ("id", "project_id", "item_id", "checked", "is_archived", "is_deleted")


def _stored_live_keys(sync_dir: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return the stems of the stored objects that are not marked deleted, per resource directory.

    The first dict holds the objects a full sync is expected to contain. The second holds those it
    leaves out because they were completed or archived (design D9): projects and sections with
    `is_archived: true`, items with `checked: true`, objects whose `project_id` names an archived
    project, and notes of the items left out.
    """
    stored = _stored_live_objects(sync_dir)
    archived = {_id(v, "id") for v in stored.get(PROJECTS, {}).values() if _flag(v, "is_archived")} - {None}
    left_out_items = {_id(v, "id") for v in stored.get(ITEMS, {}).values() if _left_out(ITEMS, v, archived, set())} - {
        None
    }

    live, left_out = {}, {}
    for resource, values in stored.items():
        live[resource], left_out[resource] = set(), set()
        for stem, value in values.items():
            excluded = _left_out(resource, value, archived, left_out_items)
            (left_out if excluded else live)[resource].add(stem)
    return live, left_out


def _stored_live_objects(sync_dir: Path) -> dict[str, dict[str, dict]]:
    """The stored objects that are not marked deleted, per resource directory and stem, each with
    only the fields of `_LEFT_OUT_FIELDS`, so that a large backup is not held in memory."""
    stored: dict[str, dict[str, dict]] = {}
    if not sync_dir.is_dir():
        return {}
    for directory in sorted(p for p in sync_dir.iterdir() if p.is_dir()):
        names = set(_listdir(directory))
        values = {}
        for name in names:
            if not name.endswith(JSON_SUFFIX) or name.endswith(DELETED_SUFFIX):
                continue
            stem = name[: -len(JSON_SUFFIX)]
            if f"{stem}{DELETED_SUFFIX}" in names:
                continue
            value = _parse_stored(directory / name, storage.read_bytes(directory / name) or b"null")
            if _is_deleted(value):
                continue
            values[stem] = {k: value[k] for k in _LEFT_OUT_FIELDS if k in value} if isinstance(value, dict) else {}
        stored[directory.name] = values
    return stored


def _left_out(resource: str, value, archived: set[str], left_out_items: set[str]) -> bool:
    """Whether a stored object leaves the sync because it was completed or archived (design D9)."""
    if resource in (PROJECTS, SECTIONS) and _flag(value, "is_archived"):
        return True
    if resource == ITEMS and _flag(value, "checked"):
        return True
    if resource == NOTES and _id(value, "item_id") in left_out_items:
        return True
    return _id(value, "project_id") in archived


def _flag(value, name: str) -> bool:
    return isinstance(value, dict) and value.get(name) is True


def _id(value, name: str) -> str | None:
    """An ID field of a stored object as a string, so that numbers and strings compare alike."""
    found = value.get(name) if isinstance(value, dict) else None
    return str(found) if isinstance(found, (str, int)) and not isinstance(found, bool) else None


def _listdir(directory: Path) -> list[str]:
    try:
        return [p.name for p in directory.iterdir()]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StorageError(f"cannot list {directory}: {exc.strerror or exc}") from exc


def _is_deleted(value) -> bool:
    return isinstance(value, dict) and value.get("is_deleted") is True


def _is_empty(value) -> bool:
    return isinstance(value, (list, dict)) and not value


def _count(stats: ApplyStats, written: bool) -> None:
    if written:
        stats.written += 1
    else:
        stats.unchanged += 1
