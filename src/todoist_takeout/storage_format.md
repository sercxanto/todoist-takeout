# todoist-takeout backup format

Format version: 1

This directory is a backup of a [Todoist](https://www.todoist.com) account, written by
todoist-takeout. Every file is plain JSON or
Markdown, so you can read the backup without the tool. This file describes the format and is
kept up to date by every run.

## Directory layout

| Path | Content |
|---|---|
| `README.md` | This description. |
| `state.json` | The checkpoint and run metadata. See [State file](#state-file). |
| `sync/` | The account's active data, as returned by the Todoist sync API. See [Active data](#active-data). |
| `pending/` | A downloaded response that has not been applied yet. Normally empty. See [Pending response](#pending-response). |
| `.lock` | Lock file that keeps two runs from working on the same backup. See [Lock file](#lock-file). |

## Active data

`sync/` holds the account's active data, as returned by the Todoist sync endpoint
(`/api/v1/sync`). Each top-level key of a sync response is a resource, for example `items`
(tasks), `projects`, `notes` (task comments) or `project_notes` (project comments). A resource is
stored according to its shape:

| Shape | Stored at | Example |
|---|---|---|
| Collection: an array of objects | `sync/<resource>/<key>.json`, one file per object | `sync/items/6X7rM8997g3RQmvh.json` |
| Mapping: a JSON object whose values are objects | `sync/<resource>/<name>.json`, one file per entry, named after the entry | `sync/workspaces/28173.json` |
| Single value | `sync/<resource>.json` | `sync/user.json` |

The key of a collection object is its `id`, except in these collections, where it is built from
several fields in this order (see [File names](#file-names)):

| Resource | Key fields |
|---|---|
| `view_options` | `view_type`, `object_id` |
| `workspace_users` | `workspace_id`, `user_id` |
| `collaborator_states`, `collaborators_states` | `project_id`, `user_id` |
| `project_view_options_defaults` | `project_id` |

For example, the view option with `view_type` `project` and `object_id` `6X7fphhgwcXVGccJ` is
stored at `sync/view_options/project~6X7fphhgwcXVGccJ.json`. Integer key fields are written in
decimal.

Resources stored as mappings: `workspaces`.

Resources stored as single values: `user`, `user_settings`, `user_plan_limits`, `stats`,
`notification_settings`, `settings_notifications`, `live_notifications_last_read_id`,
`live_notifications_last_read`, `day_orders`, `locations`, `role_actions`.

Not stored under `sync/`: `sync_token`, `full_sync` and `full_sync_date_utc`, which go into
`state.json`, and `temp_id_mapping` and `sync_status`, which only matter for write requests.

A resource the tool does not know, or a known resource whose value has an unexpected shape, is
stored by its shape: an array whose objects all have an `id` as a collection keyed by `id`,
anything else as a single value. The tool prints a warning naming the resource.

**Updates.** The first sync of a backup is a full sync, which returns every object. Later syncs are
incremental and return only what changed since the previous one; the files of all other objects
stay as they are. An empty array or object in an incremental sync means that nothing changed. A
single value is replaced as a whole. If the server sends a full sync again, objects missing from it
are kept unchanged, and the tool warns how many there were; they may have been deleted in Todoist.

**Deleted objects.** When an object is deleted in Todoist, the sync returns it with
`"is_deleted": true`, sometimes with all its fields and sometimes with only a few. The object's
file is never removed:

- If the backup has no file for the object yet, the deletion payload is stored as `<key>.json`.
- If the deletion payload contains every top-level field of the stored object, it replaces
  `<key>.json`.
- Otherwise `<key>.json` keeps the last full content of the object, and the deletion payload is
  written next to it as a tombstone, `sync/<resource>/<key>.deleted.json`, for example
  `sync/projects/6X7fphhgwcXVGccJ.deleted.json`.

An object counts as deleted if its `<key>.json` has `"is_deleted": true` or a `<key>.deleted.json`
exists.

**Restored objects.** When the sync returns an object without `"is_deleted": true` that the
backup stores as deleted, the returned payload is written to `<key>.json`, and then its
`<key>.deleted.json` is removed, so that the object counts as live again. This is the only case in
which the tool removes a file under `sync/`.

Comments keep their attachment metadata (`file_attachment`) exactly as the API returned it. The
attached files themselves are not downloaded.

## JSON encoding

Every `.json` file the tool writes is encoded the same way on every operating system:

- UTF-8 without a byte order mark;
- indented with two spaces;
- non-ASCII characters written as they are, not as `\u` escapes. The only exception is a lone
  UTF-16 surrogate, which has no UTF-8 form and is written as a `\uXXXX` escape;
- LF line endings and a final newline;
- object keys in the order the API returned them.

Each object file holds exactly the JSON value the API returned for that object, with every field
and nothing added. Only whitespace and the spelling of numbers can differ from the API's text.

## File names

An object's file name is built from its key: usually its `id`, sometimes several fields (the
`sync/` section names them). Each key component is encoded on its own:

- the characters `A`-`Z`, `a`-`z`, `0`-`9`, `_` and `-` are kept;
- every other character is written as its UTF-8 bytes, each as `%XX` with uppercase hex digits.
  For example, `Größe` becomes `Gr%C3%B6%C3%9Fe` and `a.b` becomes `a%2Eb`;
- a component that equals a reserved Windows device name (`CON`, `PRN`, `AUX`, `NUL`,
  `COM0`-`COM9`, `LPT0`-`LPT9`, in any letter case) has its first character encoded, so `CON`
  becomes `%43ON` and `nul` becomes `%6Eul`.

Encoded components are joined with `~`, in the order the `sync/` section documents. To decode a
name, drop the `.json` or `.deleted.json` suffix, split the rest at `~`, and percent-decode each
part as UTF-8.

Encoded names contain no `.`, so the suffix is always unambiguous. They are at most 200
characters long; the tool refuses to store a key whose encoded name would be longer.

On a case-insensitive file system (the default on Windows and macOS), two keys that differ only in
letter case would share a file. The tool checks the key stored in a file before it replaces it, and
fails instead of overwriting one object with another.

## State file

`state.json` is the checkpoint. The tool writes it after all other files of a run, so it never
points past data that is not on disk.

```json
{
  "format_version": 1,
  "sync_token": "…",
  "last_full_sync_utc": "2026-10-18T10:00:00Z",
  "last_successful_run_utc": "2026-10-18T10:00:05Z",
  "tool_version": "0.1.0"
}
```

| Field | Meaning |
|---|---|
| `format_version` | The version of this format. A tool that supports only older versions refuses to touch the backup. |
| `sync_token` | The sync token of the last applied response, or `null` before the first one is applied. |
| `last_full_sync_utc` | The `full_sync_date_utc` of the last full sync, or `null`. |
| `last_successful_run_utc` | When the last run completed, or `null`. |
| `tool_version` | The version of todoist-takeout that last wrote the file. |

Times are UTC in ISO 8601 format. The first run creates `state.json` without a sync token before
it writes anything else, which marks the directory as a todoist-takeout backup. Fields that a later
version of the tool adds are kept when an older version rewrites the file.

## Pending response

A sync response is saved to `pending/sync-response.json` as soon as it has been downloaded,
exactly as received (after transfer decoding, otherwise unchanged), and before anything is taken
from it. It is deleted once all its objects are written and `state.json` holds its sync token. If a
run is interrupted in between, the next run applies the saved response instead of downloading it
again.

## Lock file

A run holds an exclusive operating-system lock on `.lock` while it works, so that a second run on
the same backup stops instead of interfering. The lock ends with the process, even after a crash.
The file itself is empty and is never deleted. Locks may not work on network file systems; keep the
backup on a local disk, or make sure runs do not overlap.

## Temporary files

Every file is replaced atomically: the new content is written to a temporary file named
`.<file name>.<random>.tmp` in the same directory, flushed to disk, and renamed over the old file.
Each file therefore holds either its complete old or its complete new content, even after a crash
or power loss. A crash can leave temporary files behind; the next run removes them. They never hold
data that is not also somewhere else.
