"""Records sync fixtures from a real Todoist account and generates `tests/fixtures/shapes.json`.

    python scripts/record_fixtures.py record --account read    # uses TODOIST_CONTRACT_TOKEN
    python scripts/record_fixtures.py record --account write   # uses TODOIST_CONTRACT_WRITE_TOKEN
    python scripts/record_fixtures.py rescrub                  # applies the current rules to the recordings
    python scripts/record_fixtures.py shapes                   # regenerates shapes.json only

Recording is read-only: it makes a full sync and one incremental sync and sends no commands. Both
responses are scrubbed before they are written to `tests/fixtures/recorded/<account>/`, so that
nothing of the account is committed: strings become placeholders, IDs random pseudonyms and numbers
zero (see Scrubber). Field names, booleans and enum-like values stay.

The contract tests import this module for `Scrubber`, `shape_of` and `compare_shapes`.
"""

import argparse
import json
import os
import random
import re
import string
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
# Object keys that are IDs, such as the task IDs in `day_orders`, rather than field names.
_ID_KEY = re.compile(r"(?=[A-Za-z]*[0-9])[A-Za-z0-9]{6,64}\Z")
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
            _DATE_TEMPLATE[i] if char.isdigit() and i < len(_DATE_TEMPLATE) and _DATE_TEMPLATE[i].isdigit()
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
            return {self._key(key): self.scrub(item, key, plan=plan) for key, item in value.items()}
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

    def _key(self, key: str) -> str:
        if key in self._pseudonyms or _ID_KEY.match(key):
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
            fields: dict[str, set[str]] = {}
            for obj in objects:
                for field, item in obj.items():
                    # Object keys that are IDs, as in `day_orders`, differ per account and day.
                    name = ID_FIELD if _ID_KEY.match(field) else field
                    fields.setdefault(name, set()).add(json_type(item))
            entry["fields"] = {field: sorted(types) for field, types in sorted(fields.items())}
        keys[key] = entry
    return {"keys": dict(sorted(keys.items()))}


def merge_shapes(shapes: list[dict]) -> dict:
    keys: dict[str, dict] = {}
    for shape in shapes:
        for key, entry in shape["keys"].items():
            merged = keys.setdefault(key, {"kinds": set(), "types": set(), "full_sync": False})
            merged["kinds"].add(entry["kind"])
            merged["full_sync"] = merged["full_sync"] or entry.get("full_sync", False)
            merged["types"].update(entry["types"])
            if "fields" in entry:
                fields = merged.setdefault("fields", {})
                for field, types in entry["fields"].items():
                    fields.setdefault(field, set()).update(types)
    result = {}
    for key, merged in sorted(keys.items()):
        kinds = merged["kinds"] - {"unclassified"} or merged["kinds"]
        entry = {"kind": "|".join(sorted(kinds)), "types": sorted(merged["types"]), "full_sync": merged["full_sync"]}
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
        if new["kind"] not in old["kind"].split("|"):
            added.append(f"kind {new['kind']!r} of {key!r} (expected {old['kind']!r})")
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
    results = [storage.encode_json(scrubber.scrub(json.loads(body))) for body in bodies]
    for data in results:
        if token and token.encode() in data:
            raise SystemExit("error: the scrubbed response still contains the API token; nothing was written")
        text = data.decode("utf-8")
        leaked = sorted(o for o in scrubber.originals if len(o) >= MIN_CHECKED_ID_LENGTH and o in text)
        if leaked:
            raise SystemExit(f"error: the scrubbed response still contains {len(leaked)} original IDs; nothing was written")
    return results


def record(account: str) -> list[Path]:
    token = token_for(account)
    client = Client(UrllibTransport(token))
    full = client.sync("*")
    incremental = client.sync(json.loads(full)["sync_token"])
    directory = RECORDED / account
    directory.mkdir(parents=True, exist_ok=True)
    return _write(directory, scrubbed_bytes([full, incremental], token))


RECORDING_FILES = ("full", "incremental")


def _write(directory: Path, results: list[bytes]) -> list[Path]:
    written = []
    for name, data in zip(RECORDING_FILES, results):
        path = directory / f"{name}.json"
        path.write_bytes(data)
        written.append(path)
    return written


def rescrub() -> list[Path]:
    """Apply the current scrubbing rules to every existing recording, keeping it consistent."""
    written = []
    for directory in sorted(path for path in RECORDED.iterdir() if path.is_dir()):
        paths = [directory / f"{name}.json" for name in RECORDING_FILES]
        written += _write(directory, scrubbed_bytes([path.read_bytes() for path in paths]))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record", help="record a full and an incremental sync, then regenerate shapes")
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
