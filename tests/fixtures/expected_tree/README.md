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
| `completed/` | Completed tasks, completion events and the comments of completed tasks. See [Completed tasks](#completed-tasks). |
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
| Single value | `sync/<resource>.json` | `sync/user.json` |

The key of a collection object is its `id`, except in these collections, where it is built from
several fields in this order (see [File names](#file-names)):

| Resource | Key fields |
|---|---|
| `view_options` | `view_type`, `object_id` |
| `workspace_users` | `workspace_id`, `user_id` |
| `collaborator_states`, `collaborators_states` | `project_id`, `user_id` |
| `project_view_options_defaults` | `project_id` |
| `user_item_orders` | `scope`, `scope_id`, `item_id` |
| `suggestions` | `type` |

For example, the view option with `view_type` `project` and `object_id` `6X7fphhgwcXVGccJ` is
stored at `sync/view_options/project~6X7fphhgwcXVGccJ.json`. Integer key fields are written in
decimal.

The objects of `completed_info` each have exactly one of `project_id`, `section_id` and `item_id`. Their key
is the name of that field followed by its value, so the entry with `project_id` `6Jf8VQXxpwv5XvQ2`
is stored at `sync/completed_info/project_id~6Jf8VQXxpwv5XvQ2.json`. A project and a section with
the same id therefore never share a file.

Resources keyed by one of several fields: `completed_info`.

Resources stored as single values: `user`, `user_settings`, `user_plan_limits`, `stats`,
`notification_settings`, `settings_notifications`, `live_notifications_last_read_id`,
`live_notifications_last_read`, `day_orders`, `locations`, `role_actions`, `tooltips`,
`incomplete_item_ids`, `incomplete_project_ids`, `id_mapping`.

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
`"is_deleted": true`. The deletion payload usually has all its fields, but with their values
emptied (for example `"content": ""` and `"updated_at": null`), so it does not preserve the
object's content. The object's file is never removed:

- If the backup holds the object as live, `<key>.json` keeps its last live content, and the
  deletion payload is written next to it as a tombstone, `sync/<resource>/<key>.deleted.json`, for
  example `sync/projects/6X7fphhgwcXVGccJ.deleted.json`.
- If the backup has no file for the object yet, or its `<key>.json` already holds a deletion
  payload, the deletion payload is stored as `<key>.json`.

An object counts as deleted if its `<key>.json` has `"is_deleted": true` or a `<key>.deleted.json`
exists.

**Restored objects.** When the sync returns an object without `"is_deleted": true` that the
backup stores as deleted, the returned payload is written to `<key>.json`, and then its
`<key>.deleted.json` is removed, so that the object counts as live again. This is the only case in
which the tool removes a file under `sync/`.

Comments keep their attachment metadata (`file_attachment`) exactly as the API returned it. The
attached files themselves are not downloaded.

## Completed tasks

`completed/` holds the account's completed tasks, month by month, one event per completion
(including the completions of recurring tasks), and the comments of completed tasks. The sync in
`sync/` does not include them. Months are UTC months in the form `YYYY-MM`.

| Path | Content | Source |
|---|---|---|
| `completed/<YYYY-MM>.json` | The tasks completed in that month, for example `completed/2026-09.json`. | `GET /api/v1/tasks/completed/by_completion_date` |
| `completed/comments/<task id>.json` | The comments of one completed task, for example `completed/comments/6X8Gq4CfVh9xPm2R.json`. | `GET /api/v1/comments` |
| `completed/activity/<YYYY-MM>.json` | The completion events from T0 on whose `event_date` is in that month, for example `completed/activity/2026-10.json`. | `GET /api/v1/activities` |
| `completed/backfill/<YYYY-MM>.json` | The completions before T0 whose `completed_at` is in that month, for example `completed/backfill/2026-09.json`. | `GET /api/v1/tasks/completed` |

**Contents.** Each file holds one JSON object shaped like the API response it comes from: its list
of entries (`items` for tasks, `results` for comments and events) and every other top-level key of
the response, except the pagination cursor `next_cursor`. The tool adds no keys. When the entries
arrive in several pages, they are merged into the one list, each entry exactly as the API returned
it. The entries are ordered by time (`completed_at` for tasks, `posted_at` for comments,
`event_date` for events) and then by `id`, with integer IDs compared as numbers; an entry without
the time field comes first.

A task entry is the task's full payload, with the same fields as in `sync/items/`, as it was when its
month was last fetched. A task belongs to the month of its `completed_at`, so a task completed at
`2026-09-30T23:59:59.999999Z` is in `completed/2026-09.json`.

**Open and closed months.** Each run fetches every month that is not closed, from the month the
account was created through the current month, and replaces the month's file with the result. A
month without completed tasks has a file too, holding `{"items": []}`. A month closes once a run
has fetched it at least 7 days after the month ended (the grace period); `tasks_closed_through` in
`state.json` names the last closed month. Months that ended more than 7 days before the first run
close as soon as they are stored. A closed month is never fetched again. As a consequence:

- While a month is open, its file follows completions, uncompletions and changes to its tasks.
- Changes made in Todoist to the tasks of a closed month are not in the backup. This includes
  completions backdated into a closed month.
- A task that was completed, uncompleted and completed again in a later month is in both months'
  files. The entry with the newest `completed_at` is the task's current state.
- Tasks in archived projects are not included: the API returns them only for a request filtered
  by project.
- Completions of recurring tasks are not included: such a task is never completed as a whole, it
  moves to its next date. Its completions are in the [completion events](#completion-events).

**Comments.** For each completed task whose `note_count` is greater than 0, the file
`completed/comments/<task id>.json` holds its comments; the file name follows the rules of
[File names](#file-names). While the task's month is open, its comments are fetched again whenever
its `note_count` differs from the one stored in the month's file: new comments are added, and
changed ones replace the stored version (matched by `id`). A comment once stored stays in the file,
even after it is deleted in Todoist. Comments keep their attachment metadata (`file_attachment`)
exactly as the API returned it; the attached files are not downloaded. On a plan without comments,
no comments of completed tasks are stored.

### Completion events

A completion event records one completion of a task, including each completion of a recurring task
and completions by other people in shared projects. Completions before T0, the start of the first
run that backed up completed tasks (`t0_utc` in `state.json`), come from the one-time
[backfill](#backfill-of-earlier-completions); completions from T0 on come from the activity log. The
two sources cover separate time ranges, so no completion is in both.

`completed/activity/<YYYY-MM>.json` holds the `item:completed` events of the activity log
(`GET /api/v1/activities`) from T0 on, in the month of their `event_date`; an event without a valid
`event_date` is in the month in which it was fetched. Only months with events have a file. Each run
fetches the events since the day before the previous fetch, so fetches overlap, and merges them into
the stored files by `id`: an event is stored once, with the content of its latest fetch, and an
event once stored stays. An event's `extra_data` holds a snapshot of the task (content, due date,
`is_recurring`, labels, `note_count`, section), not the full task.

The activity log keeps a limited history: on the Free plan, 7 days (`activity_log_limit` in
`sync/user_plan_limits.json`). If the backup is not run within that period, events in the gap may
be missing; the tool warns which period is affected.

**Large integers.** The `id` of an event is an integer larger than 64 bits, for example
`152735830295847392018475629384756102938`. The files hold it exactly, but tools that parse JSON
numbers as 64-bit integers or as doubles corrupt it, among them JavaScript (`JSON.parse`) and an
SQLite `INTEGER` column. Read these files with a parser that keeps large integers, such as Python's
`json`, or treat the `id` as text.

### Backfill of earlier completions

`completed/backfill/<YYYY-MM>.json` holds the completions before T0, fetched once from
`GET /api/v1/tasks/completed`, month by month from the first month through the month of T0. The
month of T0 holds only the completions before T0. A month without completions has a file too. The
backfill can take several runs; `backfill_through` in `state.json` names the last month stored,
and the next run continues with the month after it. Once the month of T0 is stored,
`backfill_status` is `complete` and the endpoint is not requested again.

Each entry in `items` is one completion, with its own `id` (a UUID), the `task_id` of the completed
task and its `completed_at`; a recurring task has one entry per completion. `item_object` holds the
full task object, with the same fields as in `sync/items/`; for a task deleted since, it may be the
only full copy left. The maps `projects` and `sections` hold the projects and sections the entries
refer to, keyed by their `id`, which may be the only names left of projects and sections deleted
since. Unlike the activity log, this endpoint does not return completions by other people in
shared projects, so those are missing before T0.

The endpoint is deprecated. If the API no longer offers it (HTTP 404 or 410), `backfill_status`
becomes `unavailable`, the months already stored are kept, the tool warns once, and it does not
request the endpoint again. The completions of recurring tasks before T0 are then missing from the
backup; other completed tasks are still in the month files.

## JSON encoding

Every `.json` file the tool writes is encoded the same way on every operating system:

- UTF-8 without a byte order mark;
- indented with two spaces;
- non-ASCII characters written as they are, not as `\u` escapes. The only exception is a lone
  UTF-16 surrogate, which has no UTF-8 form and is written as a `\uXXXX` escape;
- LF line endings and a final newline;
- object keys in the order the API returned them.

Each object file holds exactly the JSON value the API returned for that object, with every field
and nothing added, and so does each entry in a file under `completed/`. Only whitespace and the
spelling of numbers can differ from the API's text.

## File names

An object's file name is built from its key: usually its `id`, sometimes several fields (the
`sync/` section names them). The comments of a completed task are named after the task's `id`. Each key component is encoded on its own:

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
  "completed": {
    "t0_utc": "2026-10-18T10:00:05.123456Z",
    "first_month": "2016-03",
    "tasks_closed_through": "2026-09",
    "backfill_through": "2026-10",
    "backfill_status": "complete",
    "activity_fetched_utc": "2026-10-18T10:00:05.123456Z"
  },
  "tool_version": "0.1.0"
}
```

| Field | Meaning |
|---|---|
| `format_version` | The version of this format. A tool that supports only older versions refuses to touch the backup. |
| `sync_token` | The sync token of the last applied response, or `null` before the first one is applied. |
| `last_full_sync_utc` | The `full_sync_date_utc` of the last full sync, or `null`. |
| `last_successful_run_utc` | When the last run completed, or `null`. |
| `completed` | The checkpoint of the completed tasks, see below. Missing until a run backs up completed tasks. |
| `tool_version` | The version of todoist-takeout that last wrote the file. |

The `completed` object records how far the backup of completed tasks, completion events and the
comments of completed tasks has come. Months are UTC months in the form `YYYY-MM`.

| Field | Meaning |
|---|---|
| `t0_utc` | The start of the first run that backed up completed tasks (T0). It never changes. Completion events from T0 on come from the activity log; completions before T0 come from the one-time backfill. |
| `first_month` | The first month that is backed up: the month the account was created (`joined_at` of the user), or `2007-01` if that is unknown. It never changes. |
| `tasks_closed_through` | The last closed month of completed tasks, or `null`. A month closes once it was fetched at least 7 days after it ended; closed months are never fetched again. |
| `backfill_through` | The last month the one-time backfill has stored, or `null`. |
| `backfill_status` | `in_progress` while the backfill has months left, `complete` once it has stored the month of T0, or `unavailable` if the API no longer offers its endpoint. |
| `activity_fetched_utc` | When the last complete fetch of the activity log started, or `null`. The next fetch continues from there. |

Each field advances only after the files it covers are written. A backup without the `completed`
object starts this part as on a first run.

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
