"""Records sync fixtures from a real Todoist account and generates `tests/fixtures/shapes.json`.

    python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
    python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
    python scripts/record_fixtures.py shapes                   # regenerates shapes.json only

Recording is read-only: it makes a full sync and one incremental sync and sends no commands. Both
responses are scrubbed before they are written to `tests/fixtures/recorded/<account>/`: every string
is replaced with a placeholder except IDs, keys and enum-like values, so that no personal data is
committed. Field names (object keys) are kept. Numbers and booleans are kept.

The contract tests import this module for `scrub`, `shape_of` and `compare_shapes`.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from todoist_takeout import resources, storage  # noqa: E402
from todoist_takeout.api import Client, UrllibTransport  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RECORDED = FIXTURES / "recorded"
SHAPES = FIXTURES / "shapes.json"
# Used for shapes.json as long as nothing is recorded.
HAND_WRITTEN = [FIXTURES / "sync" / "full_sync.json", FIXTURES / "sync" / "incremental_edit.json"]

TOKEN_VARIABLES = {"read": "TODOIST_CONTRACT_TOKEN", "write": "TODOIST_CONTRACT_WRITE_TOKEN"}

# --- Scrubbing ---------------------------------------------------------------

# Fields whose string values are kept when they look like an enum value (see _ENUM_LIKE).
ENUM_FIELDS = frozenset(
    {
        "color",
        "view_style",
        "view_type",
        "type",
        "role",
        "status",
        "lang",
        "unit",
        "layout",
        "grouped_by",
        "sorted_by",
        "sort_order",
        "filtered_by",
        "resource_type",
        "notification_type",
        "event_type",
        "object_type",
        "workspace_role",
        "access_level",
        "plan",
        "premium_status",
    }
)
# Fields that hold keys of collection objects (resources.KNOWN), kept like IDs.
KEY_FIELDS = frozenset(field for kind in resources.KNOWN.values() for field in kind.fields)

_ENUM_LIKE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_ID_LIKE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?\Z")
_EMAIL_LIKE = re.compile(r"[^@\s]+@[^@\s]+\Z")
_DATE_TEMPLATE = "2000-01-01T00:00:00.000000"

PLACEHOLDER = "<scrubbed>"
EMAIL_PLACEHOLDER = "user@example.invalid"
URL_PLACEHOLDER = "https://example.invalid/"


def _is_id_field(field: str | None) -> bool:
    if field is None:
        return False
    return field == "id" or field.endswith(("_id", "_ids")) or field in KEY_FIELDS


def scrub_string(value: str, field: str | None) -> str:
    if value == "":
        return value
    if _is_id_field(field) and _ID_LIKE.match(value):
        return value
    if field in ENUM_FIELDS and _ENUM_LIKE.match(value):
        return value
    if _DATE_LIKE.match(value):
        # Keep the format, replace the digits.
        return "".join(
            _DATE_TEMPLATE[i] if char.isdigit() and i < len(_DATE_TEMPLATE) and _DATE_TEMPLATE[i].isdigit()
            else ("0" if char.isdigit() else char)
            for i, char in enumerate(value)
        )
    if _EMAIL_LIKE.match(value):
        return EMAIL_PLACEHOLDER
    if value.startswith(("http://", "https://", "ws://", "wss://")):
        return URL_PLACEHOLDER
    return PLACEHOLDER


def scrub(value, field: str | None = None):
    """Return a copy of a parsed JSON value with personal strings replaced.

    `field` is the name of the object field that holds `value`; array elements inherit it, so the
    elements of `child_ids` count as IDs and the elements of `labels` are scrubbed.
    """
    if isinstance(value, dict):
        return {key: scrub(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, field) for item in value]
    if isinstance(value, str):
        return scrub_string(value, field)
    return value


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
    return kind.name


def shape_of(response: dict) -> dict:
    """Top-level keys with their kind, JSON types and, for objects, field names and field types."""
    keys = {}
    for key, value in response.items():
        entry = {"kind": describe_kind(key, value), "types": [json_type(value)]}
        if isinstance(value, list):
            objects = [obj for obj in value if isinstance(obj, dict)]
        elif isinstance(value, dict):
            entries = list(value.values())
            mapping = entry["kind"] == resources.MAPPING
            objects = [obj for obj in entries if isinstance(obj, dict)] if mapping else [value]
        else:
            objects = None
        if objects is not None:
            fields: dict[str, set[str]] = {}
            for obj in objects:
                for field, item in obj.items():
                    fields.setdefault(field, set()).add(json_type(item))
            entry["fields"] = {field: sorted(types) for field, types in sorted(fields.items())}
        keys[key] = entry
    return {"keys": dict(sorted(keys.items()))}


def merge_shapes(shapes: list[dict]) -> dict:
    keys: dict[str, dict] = {}
    for shape in shapes:
        for key, entry in shape["keys"].items():
            merged = keys.setdefault(key, {"kinds": set(), "types": set()})
            merged["kinds"].add(entry["kind"])
            merged["types"].update(entry["types"])
            if "fields" in entry:
                fields = merged.setdefault("fields", {})
                for field, types in entry["fields"].items():
                    fields.setdefault(field, set()).update(types)
    result = {}
    for key, merged in sorted(keys.items()):
        kinds = merged["kinds"] - {"unclassified"} or merged["kinds"]
        entry = {"kind": "|".join(sorted(kinds)), "types": sorted(merged["types"])}
        if "fields" in merged:
            entry["fields"] = {field: sorted(types) for field, types in sorted(merged["fields"].items())}
        result[key] = entry
    return {"keys": result}


def shape_sources() -> list[Path]:
    recorded = sorted(RECORDED.glob("*/*.json"))
    return recorded or HAND_WRITTEN


def generate_shapes(sources: list[Path] | None = None) -> dict:
    sources = shape_sources() if sources is None else sources
    shapes = merge_shapes([shape_of(json.loads(path.read_bytes())) for path in sources])
    return {"sources": [path.relative_to(FIXTURES).as_posix() for path in sources], **shapes}


def compare_shapes(expected: dict, actual: dict) -> tuple[list[str], list[str]]:
    """Return (added, removed): differences of `actual` from `expected`, one line each."""
    added, removed = [], []
    want, have = expected["keys"], actual["keys"]
    for key in sorted(have.keys() - want.keys()):
        added.append(f"key {key!r} ({have[key]['kind']})")
    for key in sorted(want.keys() - have.keys()):
        removed.append(f"key {key!r}")
    for key in sorted(have.keys() & want.keys()):
        old, new = want[key], have[key]
        if new["kind"] not in old["kind"].split("|"):
            added.append(f"kind {new['kind']!r} of {key!r} (expected {old['kind']!r})")
        for type_ in sorted(set(new["types"]) - set(old["types"])):
            added.append(f"type {type_} of {key!r}")
        old_fields, new_fields = old.get("fields", {}), new.get("fields", {})
        for field in sorted(new_fields.keys() - old_fields.keys()):
            added.append(f"field {key}.{field} ({', '.join(new_fields[field])})")
        for field in sorted(old_fields.keys() - new_fields.keys()):
            removed.append(f"field {key}.{field}")
        for field in sorted(new_fields.keys() & old_fields.keys()):
            for type_ in sorted(set(new_fields[field]) - set(old_fields[field])):
                added.append(f"type {type_} of {key}.{field}")
    return added, removed


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


def scrubbed_bytes(body: bytes, token: str) -> bytes:
    data = storage.encode_json(scrub(json.loads(body)))
    if token.encode() in data:
        raise SystemExit("error: the scrubbed response still contains the API token; nothing was written")
    return data


def record(account: str) -> list[Path]:
    token = token_for(account)
    client = Client(UrllibTransport(token))
    full = client.sync("*")
    incremental = client.sync(json.loads(full)["sync_token"])
    directory = RECORDED / account
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in [("full", full), ("incremental", incremental)]:
        path = directory / f"{name}.json"
        path.write_bytes(scrubbed_bytes(body, token))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record", help="record a full and an incremental sync, then regenerate shapes")
    record_parser.add_argument("--account", choices=sorted(TOKEN_VARIABLES), required=True)
    commands.add_parser("shapes", help="regenerate shapes.json from the recorded fixtures")
    args = parser.parse_args(argv)

    if args.command == "record":
        for path in record(args.account):
            print(f"wrote {path.relative_to(ROOT)}", file=sys.stderr)
        print("review the files for personal data before committing them", file=sys.stderr)
    print(f"wrote {write_shapes().relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
