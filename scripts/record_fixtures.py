"""Records API fixtures from a real Todoist account and generates `tests/fixtures/shapes.json`.

    python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
    python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
    python scripts/record_fixtures.py rescrub                  # applies the current rules to the recordings
    python scripts/record_fixtures.py shapes                   # regenerates shapes.json only

Recording is read-only: it makes a full sync and one incremental sync, sends no commands, and
records one page of each completed-tasks endpoint (see record_completed), and one page of the
archived projects with the `/full` of one of them (see record_archived). All responses are
scrubbed together before they are written to `tests/fixtures/recorded/<account>/`, so that nothing
of the account is committed: strings become placeholders, IDs random pseudonyms and numbers zero
(see Scrubber). Field names, booleans and enum-like values stay.

shapes.json also describes the `file_attachment` of uploaded files (see attachment_shape_of), from
the recordings that hold one, or else from the hand-written `tests/fixtures/attachments/uploads.json`.

The contract tests import this module for `Scrubber`, `shape_of` and `compare_shapes`.
"""

import argparse
import json
import os
import random
import re
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from todoist_takeout import archived, attachments, completed, resources, storage  # noqa: E402
from todoist_takeout.api import Client, UrllibTransport  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RECORDED = FIXTURES / "recorded"
SHAPES = FIXTURES / "shapes.json"
# Used for shapes.json as long as nothing is recorded.
HAND_WRITTEN = [FIXTURES / "sync" / "full_sync.json", FIXTURES / "sync" / "incremental_edit.json"]

SYNC_FILES = ("full", "incremental")
# The GET endpoints of the completed-tasks backup: recording name → path, and the hand-written
# fixtures under tests/fixtures/completed/ that stand in for its shapes as long as nothing is recorded.
COMPLETED_ENDPOINTS = {
    "by_completion_date": (completed.BY_COMPLETION_DATE, ["by_completion_date_1", "by_completion_date_2"]),
    "tasks_completed": (completed.TASKS_COMPLETED, ["tasks_completed"]),
    "activities": (completed.ACTIVITIES, ["activities_1", "activities_2"]),
    "comments": (completed.COMMENTS, ["comments"]),
}
# The same for the archived-projects backup, with hand-written fixtures under tests/fixtures/archived/.
ARCHIVED_ENDPOINTS = {
    "projects_archived": (archived.ARCHIVED_PROJECTS, ["list_1", "list_2"]),
    "project_full": ("projects/{id}/full", ["full_parent", "full_child"]),
}
ENDPOINTS = {**COMPLETED_ENDPOINTS, **ARCHIVED_ENDPOINTS}
HAND_WRITTEN_DIRECTORIES = {
    **{name: FIXTURES / "completed" for name in COMPLETED_ENDPOINTS},
    **{name: FIXTURES / "archived" for name in ARCHIVED_ENDPOINTS},
}
RECORDING_FILES = SYNC_FILES + tuple(ENDPOINTS)
# Comments with an uploaded text file and image, with the fields the probe W9 saw (add-attachment-
# downloads, proposal). They stand in for the shape of uploaded files as long as no recording holds one.
UPLOADS_HAND_WRITTEN = FIXTURES / "attachments" / "uploads.json"
ATTACHMENT_FIELD = "file_attachment"
# Smaller than the tool's page sizes, to keep the recordings small; the shape is the same.
RECORD_PAGE_LIMIT = 50
# How many months before the current one record_completed searches for completed tasks.
SEARCH_MONTHS = 12
# How many archived projects record_archived tries for a `/full` with tasks and sections: the whole
# recorded page, because few archived projects have sections.
FULL_CANDIDATES = RECORD_PAGE_LIMIT
# The lists of `/full` whose entries the shapes describe; record_archived prefers a project with these.
FULL_LISTS = ("tasks", "sections", "subprojects", "collaborators", "collaborator_states")

TOKEN_VARIABLES = {"read": "TODOIST_CONTRACT_TOKEN", "write": "TODOIST_CONTRACT_WRITE_TOKEN"}

# --- Scrubbing ---------------------------------------------------------------

# Fields whose string values are kept when they look like an enum value (see _ENUM_LIKE). Fields
# that could tell something about the person, such as `lang` or `premium_status`, are not listed.
ENUM_FIELDS = frozenset(
    {
        "color",
        "view_style",
        "view_type",
        "type",
        "scope",
        "role",
        "status",
        "unit",
        "layout",
        "grouped_by",
        "sorted_by",
        "sort_order",
        "filtered_by",
        "resource_type",
        "upload_state",
        "notification_type",
        "event_type",
        "object_type",
        "workspace_role",
        "access_level",
        "plan",
        "template_type",
    }
)
# Numbers in this resource are plan values, not account data; design D12 reads them.
PLAN_LIMITS = "user_plan_limits"
# IDs of six or more characters must not occur anywhere in the output.
MIN_CHECKED_ID_LENGTH = 6

_ENUM_LIKE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_ID_LIKE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Object keys that look like IDs rather than field names: letters and digits with a digit or with
# both letter cases. Field names are snake_case. Keys that are known IDs are replaced in any case.
_ID_KEY = re.compile(r"(?=.*[0-9]|(?=.*[a-z])(?=.*[A-Z]))[A-Za-z0-9]{6,64}\Z")
# Resources whose object keys are all IDs, such as the task IDs in `day_orders`.
ID_KEYED = frozenset({"day_orders"})
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?\Z")
_EMAIL_LIKE = re.compile(r"[^@\s]+@[^@\s]+\Z")
_DATE_TEMPLATE = "2000-01-01T00:00:00.000000"

PLACEHOLDER = "<scrubbed>"
# Stands for all object keys that are IDs in shapes.json.
ID_FIELD = "<id>"
EMAIL_PLACEHOLDER = "user@example.invalid"
URL_PLACEHOLDER = "https://example.invalid/"


def is_id_field(field: str | None) -> bool:
    return field is not None and (field == "id" or field.endswith(("_id", "_ids", "_uid", "_uids")))


def scrub_text(value: str, field: str | None) -> str:
    """The placeholder for a string that is not an ID."""
    if value == "":
        return value
    if field in ENUM_FIELDS and _ENUM_LIKE.match(value):
        return value
    if _DATE_LIKE.match(value):
        # Keep the format, replace the digits.
        return "".join(
            _DATE_TEMPLATE[i]
            if char.isdigit() and i < len(_DATE_TEMPLATE) and _DATE_TEMPLATE[i].isdigit()
            else ("0" if char.isdigit() else char)
            for i, char in enumerate(value)
        )
    if _EMAIL_LIKE.match(value):
        return EMAIL_PLACEHOLDER
    if value.startswith(("http://", "https://", "ws://", "wss://")):
        return URL_PLACEHOLDER
    return PLACEHOLDER


class Scrubber:
    """Removes everything that belongs to the account from parsed JSON values.

    Strings become placeholders, except enum-like values of ENUM_FIELDS. IDs (values of `id`,
    `*_id`, `*_ids`, `*_uid` and `*_uids` fields, and object keys that look like IDs) become random
    pseudonyms with the same length and character classes. The same ID gets the same pseudonym for
    the lifetime of the scrubber, so use one scrubber for all responses of a recording. Numbers
    become 0 of the same type, except in `user_plan_limits`. Booleans, null and field names stay.
    """

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.SystemRandom()
        self._pseudonyms: dict[str, str] = {}
        self._used: set[str] = set()

    @property
    def originals(self) -> set[str]:
        return set(self._pseudonyms)

    def scrub(self, value, field: str | None = None, *, plan: bool = False):
        """Return a scrubbed copy of `value`, the value of the object field `field`.

        Array elements inherit the field, so the elements of `child_ids` count as IDs.
        """
        plan = plan or field == PLAN_LIMITS
        if isinstance(value, dict):
            return {self._key(key, field): self.scrub(item, key, plan=plan) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item, field, plan=plan) for item in value]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, str):
            if is_id_field(field) and _ID_LIKE.match(value):
                return self.pseudonym(value)
            return scrub_text(value, field)
        if isinstance(value, int):
            if is_id_field(field):
                return int(self.pseudonym(str(value)))
            return value if plan else 0
        if isinstance(value, float):
            return value if plan else 0.0
        return value

    def collect(self, value, field: str | None = None) -> None:
        """Register the IDs in the values of ID fields, so that object keys holding them are known.

        Call it for every response of a recording before scrubbing any of them.
        """
        if isinstance(value, dict):
            for key, item in value.items():
                self.collect(item, key)
        elif isinstance(value, list):
            for item in value:
                self.collect(item, field)
        elif is_id_field(field) and not isinstance(value, bool):
            if isinstance(value, int) or (isinstance(value, str) and _ID_LIKE.match(value)):
                self.pseudonym(str(value))

    def _key(self, key: str, parent: str | None) -> str:
        if key in self._pseudonyms or parent in ID_KEYED or _ID_KEY.match(key):
            return self.pseudonym(key)
        return key

    def pseudonym(self, original: str) -> str:
        if original not in self._pseudonyms:
            for _ in range(1000):
                candidate = "".join(self._replace(i, char) for i, char in enumerate(original))
                if candidate not in self._used and (candidate != original or len(original) < 2):
                    break
            else:
                raise ValueError(f"no free pseudonym for an ID of length {len(original)}")
            self._pseudonyms[original] = candidate
            self._used.add(candidate)
        return self._pseudonyms[original]

    def _replace(self, index: int, char: str) -> str:
        if char.isdigit():
            if index == 0 and char != "0":
                return self._rng.choice("123456789")  # no leading zero, so numeric IDs stay numbers
            return self._rng.choice(string.digits)
        if char in string.ascii_uppercase:
            return self._rng.choice(string.ascii_uppercase)
        if char in string.ascii_lowercase:
            return self._rng.choice(string.ascii_lowercase)
        return char


def scrub(value, field: str | None = None):
    """Scrub one value with a scrubber of its own (see Scrubber)."""
    return Scrubber().scrub(value, field)


# --- Shapes ------------------------------------------------------------------


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
    if isinstance(value, list):
        return "array"
    return "object"


def describe_kind(key: str, value) -> str:
    """The kind `resources.py` assigns to a top-level key, or "unclassified" if only the fallback fits."""
    kind = resources.KNOWN.get(key)
    if kind is None or not resources.fits(kind, value):
        return "unclassified"
    if kind.name == resources.COLLECTION:
        return f"collection({','.join(kind.fields)})"
    if kind.name == resources.ONE_OF:
        return f"one_of({','.join(kind.fields)})"
    return kind.name


def shape_of(response: dict) -> dict:
    """Top-level keys with their kind, JSON types and, for objects, field names and field types.

    `full_sync` says whether the key occurred in a full sync, because some keys only occur in
    incremental ones.
    """
    keys = {}
    full = response.get("full_sync") is True
    for key, value in response.items():
        entry = {"kind": describe_kind(key, value), "types": [json_type(value)], "full_sync": full}
        if isinstance(value, list):
            objects = [obj for obj in value if isinstance(obj, dict)]
        elif isinstance(value, dict):
            objects = [value]
        else:
            objects = None
        if objects is not None:
            entry["fields"] = _fields(objects)
        keys[key] = entry
    return {"keys": dict(sorted(keys.items()))}


def endpoint_shape_of(response: dict) -> dict:
    """Top-level keys of a GET response with their kind, JSON types and the fields of their entries.

    The kinds follow the page merge of design D6: `list` for the list of entries, `map` for an
    object, `value` for anything else. A map keyed by IDs, such as `projects` of `tasks/completed`,
    is described by the fields of its values.
    """
    keys = {}
    for key, value in response.items():
        objects = None
        if isinstance(value, list):
            kind, objects = "list", [obj for obj in value if isinstance(obj, dict)]
        elif isinstance(value, dict):
            kind, objects = "map", [value]
            if value and all(_ID_KEY.match(k) and isinstance(v, dict) for k, v in value.items()):
                objects = list(value.values())
        else:
            kind = "value"
        entry = {"kind": kind, "types": [json_type(value)]}
        if objects is not None:
            entry["fields"] = _fields(objects)
        keys[key] = entry
    return {"keys": dict(sorted(keys.items()))}


def _fields(objects: list[dict]) -> dict:
    fields: dict[str, set[str]] = {}
    for obj in objects:
        for field, item in obj.items():
            # Object keys that are IDs, as in `day_orders`, differ per account and day.
            name = ID_FIELD if _ID_KEY.match(field) else field
            fields.setdefault(name, set()).add(json_type(item))
    return {field: sorted(types) for field, types in sorted(fields.items())}


def uploaded_attachments(value) -> list[dict]:
    """The `file_attachment` of each comment in a response that describes an uploaded file, by the
    rule of the download step (`attachments.uploaded_file`), so links and pending uploads are left out."""
    found = []
    if isinstance(value, dict):
        if attachments.uploaded_file(value) is not None:
            found.append(value[ATTACHMENT_FIELD])
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return found
    for item in values:
        found.extend(uploaded_attachments(item))
    return found


def attachment_shape_of(uploads: list[dict]) -> dict:
    """The fields and types of the `file_attachment` objects of uploaded files, with their
    `resource_type` as the kind, so that compare_shapes reports a new field or a new type of upload."""
    shapes = []
    for upload in uploads:
        kind = upload.get("resource_type")
        entry = {
            "kind": kind if isinstance(kind, str) else "unclassified",
            "types": [json_type(upload)],
            "fields": _fields([upload]),
        }
        shapes.append({"keys": {ATTACHMENT_FIELD: entry}})
    return merge_shapes(shapes)


def merge_shapes(shapes: list[dict]) -> dict:
    keys: dict[str, dict] = {}
    for shape in shapes:
        for key, entry in shape["keys"].items():
            merged = keys.setdefault(key, {"kinds": set(), "types": set()})
            merged["kinds"].add(entry["kind"])
            if "full_sync" in entry:  # sync shapes only
                merged["full_sync"] = merged.get("full_sync", False) or entry["full_sync"]
            merged["types"].update(entry["types"])
            if "fields" in entry:
                fields = merged.setdefault("fields", {})
                for field, types in entry["fields"].items():
                    fields.setdefault(field, set()).update(types)
    result = {}
    for key, merged in sorted(keys.items()):
        kinds = merged["kinds"] - {"unclassified"} or merged["kinds"]
        entry = {"kind": "|".join(sorted(kinds)), "types": sorted(merged["types"])}
        if "full_sync" in merged:
            entry["full_sync"] = merged["full_sync"]
        if "fields" in merged:
            entry["fields"] = {field: sorted(types) for field, types in sorted(merged["fields"].items())}
        result[key] = entry
    return {"keys": result}


def shape_sources() -> list[Path]:
    recorded = sorted(path for name in SYNC_FILES for path in RECORDED.glob(f"*/{name}.json"))
    return recorded or HAND_WRITTEN


def endpoint_shape_sources() -> dict[str, list[Path]]:
    """The recordings of each GET endpoint, or its hand-written fixtures."""
    sources = {}
    for name, (_path, hand_written) in ENDPOINTS.items():
        recorded = sorted(RECORDED.glob(f"*/{name}.json"))
        sources[name] = recorded or [HAND_WRITTEN_DIRECTORIES[name] / f"{stem}.json" for stem in hand_written]
    return sources


def attachment_shape_sources() -> list[Path]:
    """The recordings that hold an uploaded file, or the hand-written uploads."""
    recorded = sorted(path for path in RECORDED.glob("*/*.json") if uploaded_attachments(json.loads(path.read_bytes())))
    return recorded or [UPLOADS_HAND_WRITTEN]


def generate_shapes(
    sources: list[Path] | None = None,
    endpoint_sources: dict[str, list[Path]] | None = None,
    attachment_sources: list[Path] | None = None,
) -> dict:
    sources = shape_sources() if sources is None else sources
    endpoint_sources = endpoint_shape_sources() if endpoint_sources is None else endpoint_sources
    attachment_sources = attachment_shape_sources() if attachment_sources is None else attachment_sources
    shapes = merge_shapes([shape_of(json.loads(path.read_bytes())) for path in sources])
    endpoints = {
        name: {
            "sources": [path.relative_to(FIXTURES).as_posix() for path in paths],
            **merge_shapes([endpoint_shape_of(json.loads(path.read_bytes())) for path in paths]),
        }
        for name, paths in endpoint_sources.items()
    }
    uploads = [upload for path in attachment_sources for upload in uploaded_attachments(json.loads(path.read_bytes()))]
    file_attachments = {
        "sources": [path.relative_to(FIXTURES).as_posix() for path in attachment_sources],
        **attachment_shape_of(uploads),
    }
    return {
        "sources": [path.relative_to(FIXTURES).as_posix() for path in sources],
        **shapes,
        "endpoints": endpoints,
        "file_attachments": file_attachments,
    }


def compare_shapes(expected: dict, actual: dict) -> tuple[list[str], list[str]]:
    """Return (added, removed): differences of `actual` from `expected`, one line each.

    Removals are only listed where they can mean something: a key that the recordings saw in a full
    sync missing from a full sync, and a field missing from a resource whose objects are present.
    An account without tasks cannot show the fields of tasks.
    """
    added, removed = [], []
    want, have = expected["keys"], actual["keys"]
    actual_full = any(entry.get("full_sync") for entry in have.values())
    for key in sorted(have.keys() - want.keys()):
        added.append(f"key {key!r} ({have[key]['kind']})")
    for key in sorted(want.keys() - have.keys()):
        if want[key].get("full_sync") or not actual_full:
            removed.append(f"key {key!r}")
    for key in sorted(have.keys() & want.keys()):
        old, new = want[key], have[key]
        for kind in sorted(set(new["kind"].split("|")) - set(old["kind"].split("|"))):
            added.append(f"kind {kind!r} of {key!r} (expected {old['kind']!r})")
        for type_ in sorted(set(new["types"]) - set(old["types"])):
            added.append(f"type {type_} of {key!r}")
        old_fields, new_fields = old.get("fields", {}), new.get("fields", {})
        for field in sorted(new_fields.keys() - old_fields.keys()):
            added.append(f"field {key}.{field} ({', '.join(new_fields[field])})")
        if new_fields:
            for field in sorted(old_fields.keys() - new_fields.keys()):
                removed.append(f"field {key}.{field}")
        for field in sorted(new_fields.keys() & old_fields.keys()):
            for type_ in sorted(set(new_fields[field]) - set(old_fields[field])):
                added.append(f"type {type_} of {key}.{field}")
    return added, removed


def compare_endpoint_bodies(expected: dict, bodies: dict[str, bytes]) -> dict[str, tuple[list[str], list[str]]]:
    """(added, removed) of each response in `bodies`, by recording name, against `expected`, the
    `endpoints` of shapes.json. A missing `next_cursor` is not listed as removed: the last page of a
    cursor walk has none, and one page may be the last."""
    result = {}
    for name, body in bodies.items():
        added, removed = compare_shapes(expected[name], endpoint_shape_of(json.loads(body)))
        result[name] = (added, [line for line in removed if line != f"key {completed.CURSOR!r}"])
    return result


def write_shapes() -> Path:
    SHAPES.write_bytes(storage.encode_json(generate_shapes()))
    return SHAPES


# --- Recording ---------------------------------------------------------------


def token_for(account: str) -> str:
    variable = TOKEN_VARIABLES[account]
    token = os.environ.get(variable, "").strip()
    if not token:
        raise SystemExit(f"error: set {variable} to the API token of the {account} account")
    return token


def scrubbed_bytes(bodies: list[bytes], token: str | None = None) -> list[bytes]:
    """Scrub the responses of one recording with one scrubber, and check the result."""
    scrubber = Scrubber()
    values = [json.loads(body) for body in bodies]
    for value in values:
        scrubber.collect(value)
    results = [storage.encode_json(scrubber.scrub(value)) for value in values]
    for data in results:
        if token and token.encode() in data:
            raise SystemExit("error: the scrubbed response still contains the API token; nothing was written")
        text = data.decode("utf-8")
        leaked = sorted(o for o in scrubber.originals if len(o) >= MIN_CHECKED_ID_LENGTH and o in text)
        if leaked:
            raise SystemExit(
                f"error: the scrubbed response still contains {len(leaked)} original IDs; nothing was written"
            )
    return results


def record(account: str, now: datetime | None = None) -> list[Path]:
    token = token_for(account)
    client = Client(UrllibTransport(token))
    full = client.sync("*")
    incremental = client.sync(json.loads(full)["sync_token"])
    bodies = {"full": full, "incremental": incremental}
    bodies.update(record_completed(client, now or datetime.now(timezone.utc)))
    bodies.update(record_archived(client))
    directory = RECORDED / account
    directory.mkdir(parents=True, exist_ok=True)
    return _write(directory, dict(zip(bodies, scrubbed_bytes(list(bodies.values()), token))))


def record_completed(client: Client, now: datetime) -> dict[str, bytes]:
    """One page of each completed-tasks endpoint, with the parameters the tool sends. Read-only.

    `by_completion_date` and `tasks/completed` are recorded for the latest month before the current
    one that has completed tasks, searching back SEARCH_MONTHS months (the month before the current
    one if none has). `comments` are those of the first task with `note_count` > 0 seen in these
    months; without such a task, they are not recorded. `activities` is the latest page of
    completion events.
    """
    bodies: dict[str, bytes] = {}
    chosen = task_id = None
    month = completed.month_of(now)
    for _ in range(SEARCH_MONTHS):
        month = _previous_month(month)
        since, until = completed.month_window(month)
        params = {"since": since, "until": until, "limit": str(RECORD_PAGE_LIMIT)}
        body = client.get(completed.BY_COMPLETION_DATE, params)
        items = json.loads(body).get("items") or []
        if chosen is None and (items or "by_completion_date" not in bodies):
            bodies["by_completion_date"] = body
            if items:
                chosen = month
        if task_id is None:
            task_id = next((task["id"] for task in items if _has_comments(task)), None)
        if chosen is not None and task_id is not None:
            break
    since, until = completed.month_window(chosen or _previous_month(completed.month_of(now)))
    bodies["tasks_completed"] = client.get(
        completed.TASKS_COMPLETED,
        {"since": since, "until": until, "annotate_items": "true", "limit": str(RECORD_PAGE_LIMIT), "offset": "0"},
    )
    bodies["activities"] = client.get(
        completed.ACTIVITIES, {"object_event_types": completed.COMPLETION_EVENTS, "limit": str(RECORD_PAGE_LIMIT)}
    )
    if task_id is not None:
        bodies["comments"] = client.get(completed.COMMENTS, {"task_id": str(task_id), "limit": str(RECORD_PAGE_LIMIT)})
    else:
        print(
            f"no completed task with comments in the last {SEARCH_MONTHS} months; comments not recorded",
            file=sys.stderr,
        )
    return bodies


def record_archived(client: Client) -> dict[str, bytes]:
    """The first page of archived projects, and the `/full` of one project on it. Read-only.

    So that the shapes describe the fields of tasks and sections, the `/full` of up to
    FULL_CANDIDATES listed projects is requested, stopping at the first with tasks and sections.
    Without one, the project whose `/full` has the most non-empty lists is recorded, the earliest
    on a tie. Without an archived project, `/full` is not recorded.
    """
    listed = client.get(archived.ARCHIVED_PROJECTS, {"limit": str(RECORD_PAGE_LIMIT)})
    bodies = {"projects_archived": listed}
    projects = json.loads(listed).get("results") or []
    project_ids = [str(p["id"]) for p in projects if isinstance(p, dict) and "id" in p][:FULL_CANDIDATES]
    best, best_score = None, -1
    for project_id in project_ids:
        body = client.get(archived.full_path(project_id), {})
        full = json.loads(body)
        score = sum(1 for key in FULL_LISTS if isinstance(full.get(key), list) and full[key])
        if score > best_score:
            best, best_score = body, score
        if full.get("tasks") and full.get("sections"):
            break
    if best is not None:
        bodies["project_full"] = best
    else:
        print("no archived project; projects/{id}/full not recorded", file=sys.stderr)
    return bodies


def _previous_month(month: str) -> str:
    return completed.month_of(completed.month_start(month) - timedelta(days=1))


def _has_comments(task) -> bool:
    if not isinstance(task, dict) or "id" not in task:
        return False
    count = task.get("note_count")
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def _write(directory: Path, results: dict[str, bytes]) -> list[Path]:
    """Write the files of one recording, and remove those of an earlier one that this one lacks."""
    written = []
    for name in RECORDING_FILES:
        path = directory / f"{name}.json"
        if name in results:
            path.write_bytes(results[name])
            written.append(path)
        else:
            path.unlink(missing_ok=True)  # its pseudonyms would not match the new recording's
    return written


def rescrub() -> list[Path]:
    """Apply the current scrubbing rules to every existing recording, keeping it consistent."""
    written = []
    for directory in sorted(path for path in RECORDED.iterdir() if path.is_dir()):
        paths = {name: directory / f"{name}.json" for name in RECORDING_FILES}
        bodies = {name: path.read_bytes() for name, path in paths.items() if path.exists()}
        written += _write(directory, dict(zip(bodies, scrubbed_bytes(list(bodies.values())))))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser(
        "record",
        help="record a full and an incremental sync, the completed-tasks and the archived-projects endpoints, "
        "then regenerate shapes",
    )
    record_parser.add_argument("--account", choices=sorted(TOKEN_VARIABLES), required=True)
    commands.add_parser("rescrub", help="apply the current scrubbing rules to the recordings, then regenerate shapes")
    commands.add_parser("shapes", help="regenerate shapes.json from the recorded fixtures")
    args = parser.parse_args(argv)

    if args.command in ("record", "rescrub"):
        for path in record(args.account) if args.command == "record" else rescrub():
            print(f"wrote {path.relative_to(ROOT)}", file=sys.stderr)
        print("review the files for personal data before committing them", file=sys.stderr)
    print(f"wrote {write_shapes().relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
