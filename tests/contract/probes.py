"""Helpers for the API probes of add-completed-tasks-backup and add-archived-projects-backup.

The probes are a spike: they answer the open questions in those changes' proposals (probes R1-R7
and W1-W7) and write what they observe to `tests/contract/findings/`. Nothing here is used by the
tool. Whatever proves worth keeping becomes a regular contract test in the tasks of those changes.

The offline test `tests/test_contract_probes.py` checks these helpers.
"""

import json
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from todoist_takeout.api import Client, UrllibTransport
from todoist_takeout.errors import ApiError

COMPLETED_BY_DATE = "tasks/completed/by_completion_date"
COMPLETED_ALL = "tasks/completed"
ARCHIVED_PROJECTS = "projects/archived"
ARCHIVED_PROJECTS_COUNT = "projects/archived/count"
ACTIVITIES = "activities"
COMMENTS = "comments"

# Candidate ways to ask the activity log for task completions only. The SDKs send a list of
# `object:event` strings, but how a GET request encodes the list is not documented, and the API
# may use the legacy names `item` and `note`. `activity_filter` picks the first that works.
ACTIVITY_FILTERS = (
    {"object_event_types": '["item:completed"]'},
    {"object_event_types": "item:completed"},
    {"object_event_types": '["task:completed"]'},
    {"object_event_types": "task:completed"},
    {"object_type": "item", "event_type": "completed"},
)

PAGE_LIMIT = 200
# Guards against a cursor or offset that never ends.
MAX_PAGES = 100
# Fields that may identify a completed task or one completion of it.
TASK_ID_FIELDS = ("id", "task_id", "item_id", "v2_id", "v2_task_id")
# Response headers that would announce a deprecated endpoint.
NOTICE_HEADER_WORDS = ("deprecat", "sunset", "warning")


class ProbeError(Exception):
    """A probe could not continue, for example because a command it depends on was rejected."""


# --- Requests ----------------------------------------------------------------


class GetTransport:
    """Sends every request as GET, with the form as query string, and keeps the last headers.

    The tool's Client sends only POST requests. Wrapping its transport gives the probes the same
    retries, rate-limit waits and error classification without changing `api.py`. Error messages
    still name the method POST.
    """

    def __init__(self, inner):
        self._inner = inner
        self.last_headers: dict[str, str] = {}

    def send(self, method, path, form):
        query = urllib.parse.urlencode(form)
        status, headers, body = self._inner.send("GET", f"{path}?{query}" if query else path, {})
        self.last_headers = dict(headers.items()) if headers else {}
        return status, headers, body


class Api:
    """GET requests against the API v1, answered as parsed JSON."""

    def __init__(self, transport, **client_args):
        self.transport = GetTransport(transport)
        self.client = Client(self.transport, **client_args)

    @classmethod
    def for_token(cls, token: str) -> "Api":
        return cls(UrllibTransport(token))

    def get(self, path: str, params: dict | None = None):
        form = {key: str(value) for key, value in (params or {}).items() if value is not None}
        return json.loads(self.client.post(path, form))

    def notice_headers(self) -> dict[str, str]:
        """Headers of the last response that may announce a deprecation."""
        return {
            name: value
            for name, value in self.transport.last_headers.items()
            if any(word in name.lower() for word in NOTICE_HEADER_WORDS)
        }


@dataclass
class Walk:
    """All pages of a paginated response: items concatenated, `projects` and `sections` maps merged."""

    items: list = field(default_factory=list)
    pages: int = 0
    keys: list = field(default_factory=list)
    maps: dict = field(default_factory=dict)

    def add_page(self, page: dict, items_key: str) -> list:
        self.pages += 1
        self.keys = sorted(set(self.keys) | set(page))
        items = page.get(items_key) or []
        self.items.extend(items)
        for key in ("projects", "sections"):
            value = page.get(key)
            if isinstance(value, dict):
                self.maps.setdefault(key, {}).update(value)
            elif isinstance(value, list):
                self.maps.setdefault(key, {}).update((str(obj.get("id")), obj) for obj in value if isinstance(obj, dict))
        return items


def walk_cursor(api: Api, path: str, params: dict, *, limit: int = PAGE_LIMIT, items_key: str = "items",
                max_pages: int = MAX_PAGES) -> Walk:
    """Follow `next_cursor` until it is empty."""
    walk, cursor = Walk(), None
    while True:
        page = api.get(path, {**params, "limit": limit, "cursor": cursor})
        walk.add_page(page, items_key)
        cursor = page.get("next_cursor")
        if not cursor or walk.pages >= max_pages:
            return walk


def walk_offset(api: Api, path: str, params: dict, *, limit: int = PAGE_LIMIT, max_pages: int = MAX_PAGES) -> Walk:
    """Page by `offset` until a page is shorter than `limit`."""
    walk, offset = Walk(), 0
    while True:
        items = walk.add_page(api.get(path, {**params, "limit": limit, "offset": offset}), "items")
        offset += len(items)
        if len(items) < limit or walk.pages >= max_pages:
            return walk


def by_completion_date(api: Api, since: str, until: str, *, limit: int = PAGE_LIMIT, max_pages: int = MAX_PAGES,
                       **filters) -> Walk:
    return walk_cursor(api, COMPLETED_BY_DATE, {"since": since, "until": until, **filters}, limit=limit,
                       max_pages=max_pages)


def completed_all(api: Api, *, limit: int = PAGE_LIMIT, max_pages: int = MAX_PAGES, **params) -> Walk:
    return walk_offset(api, COMPLETED_ALL, params, limit=limit, max_pages=max_pages)


def activities(api: Api, filter_params: dict, *, limit: int = 100, max_pages: int = MAX_PAGES, **params) -> Walk:
    return walk_cursor(api, ACTIVITIES, {**filter_params, **params}, limit=limit, items_key="results",
                       max_pages=max_pages)


def comments(api: Api, **params) -> Walk:
    return walk_cursor(api, COMMENTS, params, limit=100, items_key="results")


def event_type(event) -> str:
    return f"{event.get('object_type')}:{event.get('event_type')}" if isinstance(event, dict) else "?"


def activity_filter(api: Api, **params) -> tuple[dict | None, dict]:
    """Find the parameters that make the activity log return task completions only.

    Return the first candidate whose answer holds completion events and nothing else, together with
    a report of every candidate tried. Without any completion in the range, no candidate is chosen.
    """
    report, chosen = {}, None
    for candidate in ACTIVITY_FILTERS:
        name = urllib.parse.urlencode(candidate)
        if chosen is not None:
            report[name] = "not tried"
            continue
        answer = attempt(lambda: api.get(ACTIVITIES, {**candidate, **params, "limit": 50}))
        if not answer["ok"]:
            report[name] = answer["error"]
            continue
        events = answer["value"].get("results") or []
        types = sorted({event_type(e) for e in events})
        report[name] = {"events": len(events), "types": types}
        if events and all(t.endswith(":completed") for t in types):
            chosen = candidate
    return chosen, report


def api_error(exc: ApiError) -> dict:
    return {"http_status": exc.http_status, "error": exc.error, "error_tag": exc.error_tag, "error_code": exc.error_code}


def attempt(fetch: Callable[[], object]) -> dict:
    """Run one request; describe the answer as `{"ok": True, "value": ...}` or the API's error."""
    try:
        return {"ok": True, "value": fetch()}
    except ApiError as exc:
        return {"ok": False, "error": api_error(exc)}


def poll(fetch: Callable[[], object], done: Callable[[object], bool], *, timeout: float = 60.0,
         interval: float = 3.0, sleep=time.sleep, clock=time.monotonic) -> tuple[object, float, bool]:
    """Fetch until `done(value)` or the timeout. Return the last value, the seconds taken, and done."""
    start = clock()
    while True:
        value = fetch()
        elapsed = clock() - start
        finished = done(value)
        if finished or elapsed >= timeout:
            return value, round(elapsed, 1), finished
        sleep(interval)


# --- Time --------------------------------------------------------------------


def utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(moment: datetime) -> str:
    """The UTC date, as the activity log's `date_from` and `date_to` take it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def month_start(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(start: datetime, months: int) -> datetime:
    years, month = divmod(start.month - 1 + months, 12)
    return start.replace(year=start.year + years, month=month + 1)


def month_window(start: datetime) -> tuple[str, str]:
    """`since` and `until` of a UTC month, with `until` one second before the next month."""
    return utc(start), utc(add_months(start, 1) - timedelta(seconds=1))


def months_between(first: datetime, second: datetime) -> int:
    return (second.year - first.year) * 12 + second.month - first.month


# --- Analysis ----------------------------------------------------------------


def json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def field_types(objects) -> dict[str, list[str]]:
    types: dict[str, set[str]] = {}
    for obj in objects:
        if isinstance(obj, dict):
            for key, value in obj.items():
                types.setdefault(key, set()).add(json_type(value))
    return {key: sorted(value) for key, value in sorted(types.items())}


def id_field_stats(objects) -> dict[str, dict]:
    """For every field whose name ends in `id`: in how many objects it is set, and how many distinct values it has."""
    values: dict[str, list] = {}
    for obj in objects:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.endswith("id") and isinstance(value, (str, int)) and not isinstance(value, bool):
                    values.setdefault(key, []).append(str(value))
    return {key: {"present": len(found), "distinct": len(set(found))} for key, found in sorted(values.items())}


def task_ids(obj) -> set[str]:
    if not isinstance(obj, dict):
        return set()
    return {str(obj[name]) for name in TASK_ID_FIELDS if obj.get(name) is not None}


def entries_for(objects, task_id) -> list:
    return [obj for obj in objects if str(task_id) in task_ids(obj)]


def overlap(first, second) -> dict[str, int]:
    """How many values of each task-ID field of `first` occur in each task-ID field of `second`."""
    result = {}
    for name_a in TASK_ID_FIELDS:
        values_a = {str(o[name_a]) for o in first if isinstance(o, dict) and o.get(name_a) is not None}
        if not values_a:
            continue
        for name_b in TASK_ID_FIELDS:
            values_b = {str(o[name_b]) for o in second if isinstance(o, dict) and o.get(name_b) is not None}
            if values_b:
                result[f"{name_a}~{name_b}"] = len(values_a & values_b)
    return result


def differing_fields(objects) -> list[str]:
    """Fields whose values are not the same in all objects; a missing field counts as different."""
    objects = [obj for obj in objects if isinstance(obj, dict)]
    names = set().union(*objects) if objects else set()
    missing = object()
    return sorted(name for name in names if len({json.dumps(o.get(name, missing), default=repr, sort_keys=True)
                                                    for o in objects}) > 1)


def field_diff(first, second) -> dict | None:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    return {
        "only_in_first": sorted(set(first) - set(second)),
        "only_in_second": sorted(set(second) - set(first)),
        "different_values": sorted(k for k in set(first) & set(second) if first[k] != second[k]),
    }


def describe(obj, *fields: str) -> dict:
    """Whether an object is present, with the given fields' values (booleans and nulls only, never data)."""
    if not isinstance(obj, dict):
        return {"present": False}
    result = {"present": True}
    for name in fields:
        value = obj.get(name)
        result[name] = value if value is None or isinstance(value, bool) else f"<{json_type(value)}>"
    return result


def find(objects, object_id):
    for obj in objects or []:
        if isinstance(obj, dict) and str(obj.get("id")) == str(object_id):
            return obj
    return None


# --- Running probes ------------------------------------------------------------


class Sections:
    """Runs each probe so that one failing probe does not stop the others.

    An API error is an answer and is recorded as such. Any other exception is recorded as a crash,
    and `crashed` lists the probes it happened in, so that the test can fail at the end.
    """

    def __init__(self):
        self.crashed: list[str] = []

    @contextmanager
    def run(self, findings: dict, name: str):
        entry = findings.setdefault(name, {})
        try:
            yield entry
        except ApiError as exc:
            entry["api_error"] = api_error(exc)
        except Exception as exc:  # noqa: BLE001 - recorded, and the test fails at the end
            entry["crashed"] = f"{type(exc).__name__}: {exc}"
            self.crashed.append(name)


def require(value, what: str):
    if not value:
        raise ProbeError(f"cannot continue: {what}")
    return value
