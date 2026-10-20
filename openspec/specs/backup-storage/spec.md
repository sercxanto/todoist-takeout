# backup-storage Specification

## Purpose

Defines the on-disk format of a todoist-takeout backup: the directory layout, raw JSON payloads, tombstone files, the state file, atomic writes, and the format description that keeps the backup readable without the tool.

## Requirements

### Requirement: Directory layout
A backup directory SHALL contain `state.json`, a format description `README.md` and a `sync/` directory, and `completed/`, `archived/` and `attachments/` once those parts are backed up. A resource that is a collection of objects SHALL be stored as one file per object at `sync/<resource>/<key>.json`, and a single-value resource at `sync/<resource>.json`, where `<resource>` is the top-level key of the sync response.

#### Scenario: Task file
- **WHEN** the sync returns a task with id `6X7rM8997g3RQmvh`
- **THEN** it is stored at `sync/items/6X7rM8997g3RQmvh.json`

#### Scenario: Single-value resource
- **WHEN** the sync returns the `user` object
- **THEN** it is stored at `sync/user.json`

#### Scenario: Object without an id field
- **WHEN** the sync returns an object of a collection whose objects are identified by several fields, such as a view option identified by `view_type` and `object_id`
- **THEN** it is stored at `sync/<resource>/<key>.json`, where `<key>` is built from those fields in the order and with the separator the format description documents

#### Scenario: Object identified by one of several fields
- **WHEN** the sync returns a `completed_info` entry that has `project_id` `P1` and no `section_id`
- **THEN** it is stored at `sync/completed_info/project_id~P1.json`

#### Scenario: Completed tasks
- **WHEN** a run backs up completed tasks
- **THEN** they are stored under `completed/` as the Completed-tasks layout requirement describes

#### Scenario: Archived projects
- **WHEN** a run backs up archived projects
- **THEN** they are stored under `archived/` as the Archived-projects layout requirement describes

#### Scenario: Attachment files
- **WHEN** a run downloads attachment files
- **THEN** they are stored under `attachments/` as the Attachments layout requirement describes

### Requirement: Raw payloads unchanged
Each object file SHALL contain exactly the JSON value the API returned for that object: every field, with the same names, values and order. The tool SHALL NOT add fields, remove fields or wrap the value. Each entry in a file under `completed/` or `archived/` SHALL likewise be exactly the JSON value the API returned for that entry.

#### Scenario: Round trip
- **WHEN** an object file is parsed as JSON
- **THEN** the result equals the object as it appeared in the API response

#### Scenario: Entry of a completed-tasks file
- **WHEN** an entry of `completed/2026-09.json` is compared with the same entry in the API response
- **THEN** they are equal, field by field and in the same order

#### Scenario: Response of `/full`
- **WHEN** `archived/P1/full.json` is parsed as JSON
- **THEN** the result equals the response of `GET /api/v1/projects/P1/full`, with every top-level key

### Requirement: Stable JSON encoding
Every JSON file the tool writes, including object files, the files under `completed/` and `state.json`, SHALL be UTF-8 without a byte order mark, indented with two spaces, with non-ASCII characters written as they are rather than escaped, with LF line endings and a final newline, on every operating system.

#### Scenario: Same bytes on every platform
- **WHEN** the same sync response is applied on Windows, macOS and Linux
- **THEN** the resulting object files are byte-for-byte identical

#### Scenario: Non-ASCII content
- **WHEN** a task's content contains `Größe 🍎`
- **THEN** the stored file contains those characters literally in UTF-8

#### Scenario: Month file on every platform
- **WHEN** the same completed tasks of a month are stored on Windows, macOS and Linux
- **THEN** the resulting `completed/<YYYY-MM>.json` files are byte-for-byte identical

### Requirement: Unchanged files are not rewritten
The tool SHALL NOT rewrite a file whose new content would be identical to its current content.

#### Scenario: Unchanged object in a full sync
- **WHEN** a full sync returns an object whose stored file already holds identical content
- **THEN** the file is not rewritten and its modification time stays the same

### Requirement: Tombstone files
When a deletion payload arrives for an object whose stored `<key>.json` is live (does not have `is_deleted: true`), the tool SHALL keep `<key>.json` unchanged and write the deletion payload to `<key>.deleted.json` in the same directory. When no `<key>.json` exists, or it already holds a deletion payload, the tool SHALL write the deletion payload to `<key>.json`. An object SHALL count as deleted if its `<key>.json` has `is_deleted: true` or a `<key>.deleted.json` exists.

#### Scenario: Stub deletion
- **WHEN** a stored project at `sync/projects/P1.json` is deleted and the API returns only `{"id": "P1", "is_deleted": true}`
- **THEN** `sync/projects/P1.json` keeps its previous content and `sync/projects/P1.deleted.json` contains `{"id": "P1", "is_deleted": true}`

#### Scenario: Blanked deletion
- **WHEN** a stored project at `sync/projects/P1.json` is deleted and the API returns project `P1` with all of its fields, `is_deleted: true` and `name: ""`
- **THEN** `sync/projects/P1.json` keeps its previous content and `sync/projects/P1.deleted.json` contains the returned payload

### Requirement: Restored objects
When a live payload (`is_deleted` is not true) arrives for an object that has a `<key>.deleted.json`, the tool SHALL write the payload to `<key>.json` and then remove `<key>.deleted.json`, so that the object counts as live again. This is the only case in which the tool removes a file under `sync/`.

#### Scenario: Restored object
- **WHEN** `sync/projects/P1.json` holds the last full content of project `P1`, `sync/projects/P1.deleted.json` exists, and the sync returns project `P1` with `is_deleted: false`
- **THEN** `sync/projects/P1.json` contains the returned payload and `sync/projects/P1.deleted.json` no longer exists

### Requirement: Portable file names
File names SHALL be valid and unique on Windows, macOS and Linux, including case-insensitive file systems. A key that consists only of ASCII letters, digits, `-` and `_`, and is not a reserved Windows device name, SHALL be used as is. Any other key SHALL be encoded reversibly, as the format description documents. If two different keys would map to the same file, the run SHALL fail rather than overwrite one object with another.

#### Scenario: Plain id
- **WHEN** an object's key is `6X7rM8997g3RQmvh`
- **THEN** its file name is `6X7rM8997g3RQmvh.json`

#### Scenario: Reserved name
- **WHEN** an object's key is `CON`
- **THEN** its file name is an encoded form that Windows accepts, and decoding the name gives `CON`

#### Scenario: Case collision
- **WHEN** a stored object has key `abc`, the file system is case-insensitive, and the sync returns a different object with key `ABC`
- **THEN** the run fails with exit status 1 and an error naming both keys, and `abc.json` is not overwritten

### Requirement: Atomic writes
Every file SHALL be replaced atomically, so that each file is always either complete in its old version or complete in its new version, even after a crash or power loss. Temporary files left by an interrupted run SHALL be removed by the next run.

#### Scenario: Crash during a write
- **WHEN** the process is killed while it writes a file
- **THEN** the file holds either its complete old content or its complete new content, and the next run removes any leftover temporary file

### Requirement: State file
`state.json` SHALL hold the format version, the sync token of the last applied response, the time of the last full sync, and the time of the last successful run. The first run SHALL create it, without a sync token, before writing any other file. Every run SHALL write it last. A newer format version than the tool supports SHALL make the tool refuse to run.

#### Scenario: Interrupted first run
- **WHEN** the first run on an empty directory is killed before it stores any sync token
- **THEN** `state.json` exists without a sync token, and the next run accepts the directory as a backup and starts with a full sync

#### Scenario: Successful run
- **WHEN** a run completes
- **THEN** `state.json` contains the format version, the latest sync token, and the timestamps in UTC ISO 8601 format

#### Scenario: Backup from a newer tool version
- **WHEN** `state.json` declares a format version that the running tool does not support
- **THEN** the tool prints an error, changes no files, and exits with status 2

### Requirement: Pending response file
A sync response that has been downloaded but not yet applied and checkpointed SHALL be stored at `pending/sync-response.json`, holding the response body exactly as received after transfer decoding. It SHALL be removed once it has been applied and checkpointed.

#### Scenario: After a successful run
- **WHEN** a run completes successfully
- **THEN** the `pending/` directory contains no response file

### Requirement: Documented format
The tool SHALL write a `README.md` into the backup directory that describes the layout, file encoding, key and file-name rules, tombstones, the files under `completed/`, `archived/` and `attachments/` and `state.json`, together with the format version. It SHALL name every place where the same task can be stored, and how a reader tells which copy is current. The same description SHALL be kept in the project repository.

#### Scenario: Format description in the backup
- **WHEN** a run completes
- **THEN** the backup's `README.md` describes the format version recorded in `state.json`

#### Scenario: Large integers
- **WHEN** a reader consults the description of the activity log's events
- **THEN** it warns that their `id` values exceed 64 bits and that tools which parse numbers as 64-bit integers or doubles, such as JavaScript or an SQLite `INTEGER` column, corrupt them

#### Scenario: Copies of a task
- **WHEN** a reader looks up where a completed task of an archived project can be stored
- **THEN** the description names `sync/items/`, `completed/<YYYY-MM>.json` and `archived/<project id>/completed.json`, states that all copies share the task's `id`, and gives the rule for choosing the current one

#### Scenario: File of a comment
- **WHEN** a reader looks up the file attached to a stored comment
- **THEN** the description says that it is the only file in `attachments/<comment id>/`, that its original name is the comment's `file_attachment.file_name`, and that a missing directory means the file is not downloaded

### Requirement: Completed-tasks layout
Completed tasks SHALL be stored at `completed/<YYYY-MM>.json`, completion events from the activity log at `completed/activity/<YYYY-MM>.json`, completions from the one-time backfill at `completed/backfill/<YYYY-MM>.json`, and the comments of a completed task at `completed/comments/<task id>.json`. Months are UTC. An event belongs to the month of its `event_date`, a backfill entry to the month of its `completed_at`.

#### Scenario: Month of completed tasks
- **WHEN** the completed tasks of September 2026 are stored
- **THEN** they are in `completed/2026-09.json`

#### Scenario: Month without completions
- **WHEN** a fetched month has no completed tasks
- **THEN** `completed/<YYYY-MM>.json` exists for it and holds an empty list of tasks

#### Scenario: Completion event
- **WHEN** the activity log returns a completion event with `event_date` `2026-10-01T00:00:01.000000Z`
- **THEN** it is stored in `completed/activity/2026-10.json`

#### Scenario: Comments of a completed task
- **WHEN** the comments of the completed task with id `6X7rM8997g3RQmvh` are stored
- **THEN** they are in `completed/comments/6X7rM8997g3RQmvh.json`, with the file name built by the same rules as object file names

### Requirement: Contents of completed-tasks files
Each file under `completed/` SHALL hold one JSON object shaped like the API response it comes from: its list of entries, and every other top-level key of the response except the pagination cursor. When the entries come from several pages or fetches, they SHALL be merged into that one list, each entry unchanged, ordered by time and then by `id`. The tool SHALL NOT add keys.

#### Scenario: Several pages
- **WHEN** the completed tasks of a month arrive in three pages
- **THEN** `completed/<YYYY-MM>.json` holds one object whose `items` list contains the entries of all three pages, ordered by `completed_at` and then `id`, and no `next_cursor`

#### Scenario: Additional top-level keys
- **WHEN** a response of `tasks/completed` carries `projects` and `sections` next to `items`
- **THEN** the backfill file of that month holds `projects` and `sections` too

### Requirement: Completed-tasks checkpoint
`state.json` SHALL also hold T0, the last closed month of completed tasks, the progress of the one-time backfill, and the time of the last fetch of the activity log. The tool SHALL record T0 before its first request for completed tasks, and SHALL advance each of the other values only after the files it covers are written.

#### Scenario: Month closed
- **WHEN** a run closes 2026-09
- **THEN** `state.json` names 2026-09 as the last closed month only after `completed/2026-09.json` and the comments of its tasks are written

#### Scenario: Killed before the checkpoint
- **WHEN** a run is killed after writing `completed/activity/2026-10.json` but before recording the time of that fetch
- **THEN** the next run fetches those events again and the file ends up with each event once

### Requirement: Archived-projects layout
The snapshot of an archived project SHALL be stored in `archived/<project id>/`: the listed project payload at `project.json`, the `/full` response at `full.json`, the completed tasks at `completed.json`, the project's comments at `project_comments.json`, and the comments of one task at `comments/<task id>.json`. Directory and file names SHALL follow the rules of object file names.

#### Scenario: Snapshot files
- **WHEN** the snapshot of archived project `P1` with a commented task `T1` is stored
- **THEN** the backup holds `archived/P1/project.json`, `archived/P1/full.json`, `archived/P1/completed.json`, `archived/P1/project_comments.json` and `archived/P1/comments/T1.json`

#### Scenario: Project id that needs encoding
- **WHEN** an archived project's `id` is `CON`
- **THEN** its directory name is the encoded form that object file names use for `CON`

### Requirement: Contents of archived-project files
`project.json` SHALL hold the project's payload from the list of archived projects, and `full.json` the `/full` response, each unchanged. `completed.json` and the comment files SHALL each hold one JSON object shaped like the API response, with the entries of all windows and pages merged into one list, ordered by time and then by `id`, and without the pagination cursor. The tool SHALL NOT add keys.

#### Scenario: Completed tasks in several windows
- **WHEN** an archived project's completed tasks arrive in 13 windows, some with several pages
- **THEN** `completed.json` holds one object whose `items` list contains every entry, ordered by `completed_at` and then `id`, and no `next_cursor`

#### Scenario: Project without completed tasks
- **WHEN** an archived project has no completed task, and the plan includes completed tasks
- **THEN** `completed.json` exists and holds an empty list of tasks

#### Scenario: Project without comments
- **WHEN** an archived project has no project comment, and the plan includes comments
- **THEN** `project_comments.json` exists and holds an empty list of comments

### Requirement: Archived-project checkpoint
The tool SHALL write `project.json` of a snapshot after every other file of that snapshot, so that a snapshot with a `project.json` is complete for the parts the plan allowed. `state.json` SHALL hold no checkpoint for archived projects.

#### Scenario: Killed before `project.json`
- **WHEN** a run is killed after writing `archived/P1/full.json` but before writing `archived/P1/project.json`
- **THEN** the next run fetches the snapshot of `P1` again

### Requirement: Attachments layout
The uploaded file of a comment SHALL be stored as the only file in `attachments/<comment id>/`, holding the bytes the file host served, after transfer decoding. The directory name SHALL follow the rules of object file names. The file name SHALL follow the Attachment file names requirement.

#### Scenario: Attachment file
- **WHEN** the file `preise.pdf` of comment `6X7rfFVQXwRmcvFp` is stored
- **THEN** the backup holds `attachments/6X7rfFVQXwRmcvFp/preise.pdf` with the downloaded bytes and no other file in that directory

#### Scenario: Comment id that needs encoding
- **WHEN** a comment's `id` is `CON`
- **THEN** its directory name is the encoded form that object file names use for `CON`

### Requirement: Attachment file names
The name of an attachment file SHALL be the attachment's `file_name`, changed only as far as needed to be valid and visible on Windows, macOS and Linux: normalized to Unicode NFC, with control characters and `< > : " / \ | ? *` replaced by `_`, trailing spaces and dots removed, a leading dot replaced by `_`, and `_` prefixed to a reserved Windows device name. An empty or missing name SHALL become `attachment`. The format description SHALL document these rules.

#### Scenario: Readable name
- **WHEN** `file_name` is `Preise 2024.pdf`
- **THEN** the file is stored as `Preise 2024.pdf`

#### Scenario: Characters Windows rejects
- **WHEN** `file_name` is `Rechnung: März/April.pdf`
- **THEN** the file is stored as `Rechnung_ März_April.pdf`

#### Scenario: Reserved device name with extension
- **WHEN** `file_name` is `con.txt`
- **THEN** the file is stored as `_con.txt`

#### Scenario: Hidden file
- **WHEN** `file_name` is `.env`
- **THEN** the file is stored as `_env`

#### Scenario: Trailing dot
- **WHEN** `file_name` is `bericht.pdf.`
- **THEN** the file is stored as `bericht.pdf`

#### Scenario: Decomposed umlaut
- **WHEN** `file_name` contains `u` followed by a combining diaeresis
- **THEN** the stored name contains the single character `ü`

#### Scenario: No name
- **WHEN** `file_name` is missing or empty, or nothing remains after the rules above
- **THEN** the file is stored as `attachment`

### Requirement: Attachment file name length
An attachment file name SHALL be at most 100 characters and at most 200 bytes in UTF-8 long, so that the path stays within the Windows path limit for ordinary target directories and within the name limit of every file system. A longer name SHALL be shortened before its extension, keeping the extension, without splitting a character.

#### Scenario: Long name
- **WHEN** `file_name` is 180 ASCII characters long and ends in `.pdf`
- **THEN** the stored name is 100 characters long and ends in `.pdf`

#### Scenario: Long name with multi-byte characters
- **WHEN** `file_name` consists of 150 copies of `🍎` (4 bytes each in UTF-8) followed by `.pdf`
- **THEN** the stored name is at most 200 bytes in UTF-8, ends in `.pdf`, and is valid UTF-8
