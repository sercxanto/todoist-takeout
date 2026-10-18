"""The kinds of resources a sync response contains, and how each one is keyed (design D6).

`storage_format.md` documents the same table for readers of a backup.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

COLLECTION = "collection"  # array of objects, one file per object, keyed by `fields`
MAPPING = "mapping"  # JSON object of objects, one file per entry, keyed by the entry's name
SINGLE = "single"  # one value, one file
META = "meta"  # response metadata, not stored as an object


@dataclass(frozen=True)
class Kind:
    name: str
    fields: tuple[str, ...] = ()


ID = Kind(COLLECTION, ("id",))

KNOWN: dict[str, Kind] = {
    **dict.fromkeys(
        [
            "items",
            "projects",
            "sections",
            "labels",
            "notes",
            "project_notes",
            "filters",
            "reminders",
            "reminders_location",
            "collaborators",
            "live_notifications",
            "workspace_filters",
        ],
        ID,
    ),
    "view_options": Kind(COLLECTION, ("view_type", "object_id")),
    "workspace_users": Kind(COLLECTION, ("workspace_id", "user_id")),
    "project_view_options_defaults": Kind(COLLECTION, ("project_id",)),
    # The API documentation spells this key both ways.
    "collaborator_states": Kind(COLLECTION, ("project_id", "user_id")),
    "collaborators_states": Kind(COLLECTION, ("project_id", "user_id")),
    "workspaces": Kind(MAPPING),
    **dict.fromkeys(
        [
            "user",
            "user_settings",
            "user_plan_limits",
            "stats",
            # The API documentation spells these keys both ways.
            "notification_settings",
            "settings_notifications",
            "live_notifications_last_read_id",
            "live_notifications_last_read",
            "day_orders",
            "locations",
            "role_actions",
        ],
        Kind(SINGLE),
    ),
    **dict.fromkeys(["sync_token", "full_sync", "full_sync_date_utc", "temp_id_mapping", "sync_status"], Kind(META)),
}


def key_component(value) -> str | None:
    """Return a key field's value as a string, or None if it cannot be part of a key."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def object_key(kind: Kind, obj) -> tuple[str, ...] | None:
    """Return the key of a collection object, or None if it lacks a key field."""
    if not isinstance(obj, dict):
        return None
    key = tuple(key_component(obj.get(field)) for field in kind.fields)
    return None if None in key else key


def fits(kind: Kind, value) -> bool:
    """Whether `value` has the shape that `kind` expects."""
    if kind.name == COLLECTION:
        return isinstance(value, list) and all(object_key(kind, obj) is not None for obj in value)
    if kind.name == MAPPING:
        return isinstance(value, dict) and all(isinstance(entry, dict) for entry in value.values())
    return True


def by_shape(value) -> Kind:
    """The fallback for unknown keys: an array of objects with `id` is a collection, anything else a single."""
    return ID if fits(ID, value) else Kind(SINGLE)


class Classifier:
    """Classifies the top-level keys of sync responses.

    Warns once per key and run when a key is unknown or its value does not have the expected shape.
    """

    def __init__(self):
        self._warned: set[str] = set()

    def classify(self, key: str, value) -> Kind:
        kind = KNOWN.get(key)
        if kind is not None and fits(kind, value):
            return kind
        fallback = by_shape(value)
        if key not in self._warned:
            self._warned.add(key)
            if kind is None:
                reason = "unknown resource"
            else:
                reason = f"resource with an unexpected shape (expected {_describe(kind)})"
            log.warning(
                "%s %r in the sync response; storing it unchanged as %s", reason, key, _describe(fallback)
            )
        return fallback


def _describe(kind: Kind) -> str:
    if kind.name == COLLECTION:
        return f"a collection keyed by {' + '.join(kind.fields)}"
    if kind.name == MAPPING:
        return "a mapping of objects"
    return "a single value"
