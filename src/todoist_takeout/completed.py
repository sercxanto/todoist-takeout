"""Backing up completed tasks, completion events and the comments of completed tasks (design D2).

Each file under `completed/` is shaped like the API response it comes from (design D6). The
`completed` object of `state.json` records how far this part has come; each of its fields advances
only after the files it covers are written (design D3).
"""

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from todoist_takeout import storage, sync
from todoist_takeout.api import Client, cursor_pages, offset_pages
from todoist_takeout.errors import ApiError, ConfigError, PlanRestricted, StorageError

log = logging.getLogger(__name__)

COMPLETED_DIR = "completed"
COMMENTS_DIR = "comments"
ACTIVITY_DIR = "activity"
BACKFILL_DIR = "backfill"
JSON_SUFFIX = ".json"

BY_COMPLETION_DATE = "tasks/completed/by_completion_date"
COMMENTS = "comments"
ACTIVITIES = "activities"
TASKS_COMPLETED = "tasks/completed"
TASKS_PAGE_LIMIT = 200
COMMENTS_PAGE_LIMIT = 100
ACTIVITY_PAGE_LIMIT = 100
BACKFILL_PAGE_LIMIT = 200

# The values of `backfill_status` in state.json (design D3).
IN_PROGRESS = "in_progress"
COMPLETE = "complete"
UNAVAILABLE = "unavailable"
# The statuses with which `tasks/completed` tells that it is gone (design D10).
GONE_STATUSES = frozenset({404, 410})

# The activity log's filter for task completions; list-valued parameters are JSON text.
COMPLETION_EVENTS = '["item:completed"]'
# Each fetch of the activity log starts this long before the previous one (design D9).
ACTIVITY_OVERLAP = timedelta(days=1)

# A month closes once it was fetched at least this long after it ended (design D7).
GRACE_PERIOD = timedelta(days=7)
# The first month when the account's creation date is unknown: Todoist launched in 2007.
FALLBACK_FIRST_MONTH = "2007-01"

CURSOR = "next_cursor"


def run(target: Path, client: Client, state: dict, now: Callable[[], datetime]) -> None:
    """Back up completion events, completed tasks and their comments, unless the plan leaves them out.

    The activity log comes first because its history is limited, so that a run that fails later has
    still secured its events.
    """
    target = Path(target)
    limits = sync.current_plan_limits(target)
    if limits.get("completed_tasks") is False:
        return  # the plan warnings of the run name this part
    _start(target, state, now)
    _fetch_activity(target, client, state, now(), limits.get("activity_log_limit"))
    _fetch_months(target, client, state, now(), comments=limits.get("comments") is not False)
    _backfill(target, client, state)


def _start(target: Path, state: dict, now: Callable[[], datetime]) -> None:
    """Record T0 and the first month before the first request of this part. Later runs keep them."""
    progress = storage.completed_state(state)
    changes = {}
    if progress["first_month"] is None:
        changes["first_month"] = _first_month(target)
    if progress["t0_utc"] is None:
        changes["t0_utc"] = precise_timestamp(now())
    if changes:
        storage.update_completed_state(target, state, **changes)


def _first_month(target: Path) -> str:
    user = sync.stored_value(target, "user")
    try:
        return month_of(parse_time(user.get("joined_at") if isinstance(user, dict) else None))
    except ValueError:
        log.info(
            "the account's creation date is unknown; completed tasks are backed up from %s on", FALLBACK_FIRST_MONTH
        )
        return FALLBACK_FIRST_MONTH


# --- Completion events from the activity log (design D9) ------------------------------------


def _fetch_activity(target: Path, client: Client, state: dict, start: datetime, log_limit) -> None:
    """Fetch the completion events since the previous fetch and merge them into their months' files."""
    progress = storage.completed_state(state)
    t0 = _checked_time(progress["t0_utc"], "t0_utc")
    previous = _checked_time(progress["activity_fetched_utc"], "activity_fetched_utc") or t0
    _warn_about_gap(previous, start, log_limit)
    date_from = (previous - ACTIVITY_OVERLAP).date().isoformat()
    params = {"object_event_types": COMPLETION_EVENTS, "date_from": date_from}
    try:
        pages = fetch_pages(client, ACTIVITIES, params, ACTIVITY_PAGE_LIMIT, "results")
    except PlanRestricted:
        # The checkpoint stays, so the gap warning appears once the log is available again.
        log.warning(
            "the Todoist plan does not allow fetching the activity log; completion events cannot be backed up "
            "on this plan"
        )
        return
    response = merge_pages(pages, "results", "event_date")
    by_month: dict[str, list] = {}
    for event in response["results"]:
        month = _event_month(event, t0, start)
        if month is not None:
            by_month.setdefault(month, []).append(event)
    new = 0
    for month, events in sorted(by_month.items()):
        path = target / COMPLETED_DIR / ACTIVITY_DIR / f"{month}{JSON_SUFFIX}"
        fetched = {**response, "results": events}
        stored = read_stored(path, "results")
        known = {_id_order(e.get("id")) for e in stored["results"] if isinstance(e, dict)} if stored else set()
        new += sum(1 for e in events if not isinstance(e, dict) or _id_order(e.get("id")) not in known)
        merged = merge_pages(
            [stored, fetched] if stored is not None else [fetched], "results", "event_date", by_id=True
        )
        storage.write_bytes_atomic(path, storage.encode_json(merged))
    storage.update_completed_state(target, state, activity_fetched_utc=precise_timestamp(start))
    log.info("completion events: fetched the activity log since %s; %d new events stored", date_from, new)


def _event_month(event, t0: datetime, start: datetime) -> str | None:
    """The month whose file an event goes into, or None for an event before T0."""
    date = event.get("event_date") if isinstance(event, dict) else None
    try:
        moment = parse_time(date)
    except ValueError:
        log.warning(
            "an event of the activity log has no valid event_date (%r); it is stored in the month of the fetch, %s",
            date,
            month_of(start),
        )
        return month_of(start)
    # Completions before T0 come from the one-time backfill.
    return month_of(moment) if moment >= t0 else None


def _warn_about_gap(previous: datetime, start: datetime, log_limit) -> None:
    """Warn if events may have dropped out of the activity log since `previous`.

    `log_limit` is the plan's `activity_log_limit` in days; a missing value or -1 means no limit.
    """
    if not isinstance(log_limit, (int, float)) or isinstance(log_limit, bool) or log_limit <= 0:
        return
    kept_since = start - timedelta(days=log_limit)
    if previous >= kept_since:
        return
    log.warning(
        "the Todoist plan keeps the activity log for only %s days, and it was not fetched for %d days; "
        "completion events from %s to %s may be missing from the backup",
        f"{log_limit:g}",
        (start - previous).days,
        _readable_time(previous),
        _readable_time(kept_since),
    )


# --- Completed tasks by month (design D5, D7) -----------------------------------------------


def _fetch_months(target: Path, client: Client, state: dict, moment: datetime, *, comments: bool) -> None:
    """Fetch every month that is not closed, through the current one, and close those that may close."""
    progress = storage.completed_state(state)
    closed = _checked_month(progress["tasks_closed_through"], "tasks_closed_through")
    month = next_month(closed) if closed else _checked_month(progress["first_month"], "first_month")
    current = month_of(moment)
    comment_store = _CommentStore(target / COMPLETED_DIR / COMMENTS_DIR, client, enabled=comments)
    fetched = []
    closing = True
    # The progress is logged also when the run fails, for example on the rate limit (design D13).
    try:
        while month <= current:
            since, until = month_window(month)
            params = {"since": since, "until": until}
            try:
                pages = fetch_pages(client, BY_COMPLETION_DATE, params, TASKS_PAGE_LIMIT, "items")
            except PlanRestricted:
                log.warning(
                    "the Todoist plan does not allow fetching completed tasks; they cannot be backed up on this plan"
                )
                return
            response = merge_pages(pages, "items", "completed_at")
            path = target / COMPLETED_DIR / f"{month}{JSON_SUFFIX}"
            # The comments come first: a crash before the month file makes the next run fetch them again.
            comment_store.update(read_stored(path, "items"), response["items"])
            storage.write_bytes_atomic(path, storage.encode_json(response))
            fetched.append(month)
            # Months close in order, so the checkpoint never skips an open month.
            closing = closing and moment >= month_start(next_month(month)) + GRACE_PERIOD
            if closing:
                storage.update_completed_state(target, state, tasks_closed_through=month)
            month = next_month(month)
    finally:
        if fetched:
            log.info(
                "completed tasks: fetched %d months (%s to %s), closed through %s; %d comment files written",
                len(fetched),
                fetched[0],
                fetched[-1],
                storage.completed_state(state)["tasks_closed_through"] or "none",
                comment_store.written,
            )


class _CommentStore:
    """Fetches the comments of tasks and stores them in `directory`, one file per task (design D8)."""

    def __init__(
        self,
        directory: Path,
        client: Client,
        *,
        enabled: bool,
        owner: str = "completed tasks",
        empty_when_gone: bool = False,
    ):
        self.directory = directory
        self.client = client
        self.enabled = enabled
        self.owner = owner  # whose comments these are, for the plan warning
        # Whether a task whose comments are not found gets a file with an empty list, so that a
        # missing file means "not fetched yet" (archived design D6).
        self.empty_when_gone = empty_when_gone
        self.written = 0

    def update(self, stored: dict | None, tasks: list, *, fetch_missing: bool = False) -> None:
        """Fetch the comments of each task whose `note_count` is positive and differs from the stored one.

        With `fetch_missing`, a task whose comment file is missing is fetched even if its `note_count`
        is unchanged.
        """
        if not self.enabled:
            return
        stored_counts = {}
        for task in stored["items"] if stored else []:
            task_id = _task_id(task)
            if task_id is not None:
                stored_counts[task_id] = task.get("note_count")
        for task in tasks:
            task_id = _task_id(task)
            count = task.get("note_count") if isinstance(task, dict) else None
            if task_id is None or not _is_int(count) or count <= 0:
                continue
            if task_id in stored_counts and stored_counts[task_id] == count:
                if not fetch_missing or self.path(task_id).exists():
                    continue
            self._fetch(task_id)
            if not self.enabled:
                return

    def missing(self, task) -> bool:
        """Whether a task has comments (`note_count` positive) and no comment file."""
        task_id = _task_id(task)
        count = task.get("note_count") if isinstance(task, dict) else None
        return task_id is not None and _is_int(count) and count > 0 and not self.path(task_id).exists()

    def _fetch(self, task_id: str) -> None:
        try:
            pages = fetch_pages(self.client, COMMENTS, {"task_id": task_id}, COMMENTS_PAGE_LIMIT, "results")
        except PlanRestricted:
            log.warning(
                "the Todoist plan does not allow fetching comments; the comments of %s cannot be backed up on "
                "this plan",
                self.owner,
            )
            self.enabled = False
            return
        except ApiError as exc:
            if exc.http_status != 404:
                raise
            log.debug("the comments of a task were not found (HTTP 404); the task may have been deleted")
            if not self.empty_when_gone:
                return
            pages = []
        path = self.path(task_id)
        stored = read_stored(path, "results")
        if stored is not None:
            _check_comments(path, stored, task_id)
        merged = merge_pages([stored, *pages] if stored is not None else pages, "results", "posted_at", by_id=True)
        if storage.write_bytes_atomic(path, storage.encode_json(merged)):
            self.written += 1

    def path(self, task_id: str) -> Path:
        return self.directory / f"{storage.encode_key(task_id)}{JSON_SUFFIX}"


def _check_comments(path: Path, stored: dict, task_id: str) -> None:
    """Fail instead of merging comments into a file that holds those of another task.

    On a case-insensitive file system, task IDs that differ only in letter case share a file.
    """
    for comment in stored["results"]:
        if not isinstance(comment, dict) or "item_id" not in comment:
            continue
        if _id_text(comment["item_id"]) != task_id:
            raise StorageError(
                f"{path} holds comments of the task {comment['item_id']!r}, not of the task {task_id!r} whose "
                "comments are to be stored there; on a case-insensitive file system two IDs that differ only "
                "in letter case share a file. The file was not changed"
            )


def _task_id(task) -> str | None:
    return _id_text(task.get("id")) if isinstance(task, dict) else None


def _id_text(value) -> str | None:
    if isinstance(value, str) and value:
        return value
    if _is_int(value):
        return str(value)
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- One-time backfill of the completions before T0 (design D10) -----------------------------


def _backfill(target: Path, client: Client, state: dict) -> None:
    """Store the completions before T0 month by month, resuming at the first month not stored."""
    progress = storage.completed_state(state)
    status = progress["backfill_status"]
    if status in (COMPLETE, UNAVAILABLE):
        return
    if status != IN_PROGRESS:
        raise ConfigError(f"{storage.STATE_FILE} is not a valid state file: backfill_status {status!r} is unknown")
    t0 = _checked_time(progress["t0_utc"], "t0_utc")
    done = _checked_month(progress["backfill_through"], "backfill_through")
    month = next_month(done) if done else _checked_month(progress["first_month"], "first_month")
    last = month_of(t0)
    stored = []
    # The progress is logged also when the run fails, for example on the rate limit (design D13).
    try:
        while month <= last:
            since, until = month_window(month)
            if month == last:
                until = precise_timestamp(t0)
            params = {"since": since, "until": until, "annotate_items": "true"}
            try:
                pages = list(offset_pages(client, TASKS_COMPLETED, params, BACKFILL_PAGE_LIMIT))
            except PlanRestricted:
                log.warning(
                    "the Todoist plan does not allow fetching the completions before %s; they are tried again on "
                    "the next run",
                    _readable_time(t0),
                )
                return
            except ApiError as exc:
                if exc.http_status not in GONE_STATUSES:
                    raise
                storage.update_completed_state(target, state, backfill_status=UNAVAILABLE)
                log.warning(
                    "GET /%s is no longer available (HTTP %d); completions of recurring tasks before %s cannot be "
                    "backed up. The backfill months already stored are kept",
                    TASKS_COMPLETED,
                    exc.http_status,
                    _readable_time(t0),
                )
                return
            # The boundary is exact whatever the precision of the endpoint's own `until`.
            pages = [{**page, "items": [e for e in page["items"] if _before(e, t0)]} for page in pages]
            response = merge_pages(pages, "items", "completed_at")
            path = target / COMPLETED_DIR / BACKFILL_DIR / f"{month}{JSON_SUFFIX}"
            storage.write_bytes_atomic(path, storage.encode_json(response))
            stored.append(month)
            storage.update_completed_state(target, state, backfill_through=month)
            month = next_month(month)
        storage.update_completed_state(target, state, backfill_status=COMPLETE)
    finally:
        if stored:
            outcome = {
                COMPLETE: "the backfill is complete",
                UNAVAILABLE: "the backfill stopped",
            }.get(storage.completed_state(state)["backfill_status"], f"the next run continues with {month}")
            log.info(
                "earlier completions: stored %d backfill months (%s to %s); %s",
                len(stored),
                stored[0],
                stored[-1],
                outcome,
            )


def _before(entry, t0: datetime) -> bool:
    """Whether a backfill entry was completed before T0. One without a valid `completed_at` is kept."""
    try:
        return parse_time(entry.get("completed_at") if isinstance(entry, dict) else None) < t0
    except ValueError:
        return True


# --- Pages and files (design D6) ------------------------------------------------------------


def fetch_pages(client: Client, path: str, params: dict, limit: int, list_key: str) -> list[dict]:
    """Return every page of a cursor-paginated endpoint, each checked to hold a list in `list_key`."""
    pages = []
    for page in cursor_pages(client, path, params, limit):
        if not isinstance(page.get(list_key), list):
            raise ApiError(f"GET /{path} returned a page without a list in {list_key!r}", http_status=200)
        pages.append(page)
    return pages


def merge_pages(pages: Iterable[dict], list_key: str, time_field: str, *, by_id: bool = False) -> dict:
    """Merge response pages into one object shaped like a single response.

    The lists in `list_key` are concatenated, object-valued keys are merged with a later page winning
    per key, any other key takes the value of the last page that has it, and the cursor is dropped.
    With `by_id`, an entry replaces an earlier one with the same `id`. The entries are sorted by
    `time_field`, then by `id`, so that the result does not depend on the order of the pages.
    """
    merged: dict = {}
    entries: list = []
    for page in pages:
        for key, value in page.items():
            if key == CURSOR:
                continue
            if key == list_key:
                entries.extend(value)
                merged.setdefault(key, None)  # keeps the key's position
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    if by_id:
        entries = _latest_by_id(entries)
    merged[list_key] = sorted(entries, key=lambda entry: _entry_order(entry, time_field))
    return merged


def _latest_by_id(entries: list) -> list:
    latest = {}
    without_id = []
    for entry in entries:
        key = _id_order(entry.get("id")) if isinstance(entry, dict) else None
        if key is None:
            without_id.append(entry)
        else:
            latest[key] = entry
    return [*latest.values(), *without_id]


def _entry_order(entry, time_field: str) -> tuple:
    # The API writes every time as YYYY-MM-DDTHH:MM:SS.ffffffZ, so string order is chronological.
    # An entry without the time field sorts first, one without an ID first among equal times.
    time = entry.get(time_field) if isinstance(entry, dict) else None
    ident = _id_order(entry.get("id")) if isinstance(entry, dict) else None
    return (isinstance(time, str), time if isinstance(time, str) else "", ident or (-1, 0))


def _id_order(value) -> tuple | None:
    # Integer IDs compare as integers, string IDs as strings, and never with each other.
    if _is_int(value):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return None


def read_stored(path: Path, list_key: str) -> dict | None:
    """Read a stored file of `completed/`, or return None if it does not exist."""
    data = storage.read_bytes(path)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StorageError(f"{path} is not a valid JSON file: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get(list_key), list):
        raise StorageError(f"{path} is not a JSON object with a list in {list_key!r}")
    return value


# --- Months and times (design D5) -----------------------------------------------------------


def parse_time(value) -> datetime:
    """Parse an API timestamp, such as `2026-10-18T10:00:05.123456Z`, as a UTC time. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp: {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def precise_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _readable_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def month_of(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m")


def month_start(month: str) -> datetime:
    return datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)


def next_month(month: str) -> str:
    start = month_start(month)
    return f"{start.year + 1:04d}-01" if start.month == 12 else f"{start.year:04d}-{start.month + 1:02d}"


def month_window(month: str) -> tuple[str, str]:
    """The `since` and `until` of a request for the whole month; both are inclusive to the microsecond."""
    last_day = month_start(next_month(month)) - timedelta(days=1)
    return f"{month}-01T00:00:00.000000Z", f"{last_day:%Y-%m-%d}T23:59:59.999999Z"


def _checked_month(value, field: str) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and month_start(value).strftime("%Y-%m") == value:
            return value
    except ValueError:
        pass
    raise ConfigError(f"{storage.STATE_FILE} is not a valid state file: {field} {value!r} is not a month (YYYY-MM)")


def _checked_time(value, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError:
        raise ConfigError(
            f"{storage.STATE_FILE} is not a valid state file: {field} {value!r} is not a UTC timestamp"
        ) from None
