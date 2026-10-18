# Spec Delta

## Purpose

Defines the on-disk format of a todoist-takeout backup: the directory layout, raw JSON payloads, tombstone files, the state file, atomic writes, and the format description that keeps the backup readable without the tool.

## ADDED Requirements

### Requirement: Directory layout
A backup directory SHALL contain `state.json`, a format description `README.md`, and a `sync/` directory. A resource that is a collection of objects SHALL be stored as one file per object at `sync/<resource>/<key>.json`. A resource that is a single value SHALL be stored at `sync/<resource>.json`. `<resource>` is the top-level key of the sync response, for example `items`, `projects` or `notes`.

#### Scenario: Task file
- **WHEN** the sync returns a task with id `6X7rM8997g3RQmvh`
- **THEN** it is stored at `sync/items/6X7rM8997g3RQmvh.json`

#### Scenario: Single-value resource
- **WHEN** the sync returns the `user` object
- **THEN** it is stored at `sync/user.json`

#### Scenario: Object without an id field
- **WHEN** the sync returns an object of a collection whose objects are identified by several fields, such as a view option identified by `view_type` and `object_id`
- **THEN** it is stored at `sync/<resource>/<key>.json`, where `<key>` is built from those fields in the order and with the separator the format description documents

### Requirement: Raw payloads unchanged
Each object file SHALL contain exactly the JSON value the API returned for that object: every field, with the same names, values and order. The tool SHALL NOT add fields, remove fields or wrap the value.

#### Scenario: Round trip
- **WHEN** an object file is parsed as JSON
- **THEN** the result equals the object as it appeared in the API response

### Requirement: Stable JSON encoding
Every object file and `state.json` SHALL be UTF-8 without a byte order mark, indented with two spaces, with non-ASCII characters written as they are rather than escaped, with LF line endings and a final newline, on every operating system.

#### Scenario: Same bytes on every platform
- **WHEN** the same sync response is applied on Windows, macOS and Linux
- **THEN** the resulting object files are byte-for-byte identical

#### Scenario: Non-ASCII content
- **WHEN** a task's content contains `Größe 🍎`
- **THEN** the stored file contains those characters literally in UTF-8

### Requirement: Unchanged files are not rewritten
The tool SHALL NOT rewrite a file whose new content would be identical to its current content.

#### Scenario: Unchanged object in a full sync
- **WHEN** a full sync returns an object whose stored file already holds identical content
- **THEN** the file is not rewritten and its modification time stays the same

### Requirement: Tombstone files
When a deletion payload lacks top-level fields that the stored object has, the tool SHALL keep `<key>.json` unchanged and write the deletion payload to `<key>.deleted.json` in the same directory. An object SHALL count as deleted if its `<key>.json` has `is_deleted: true` or a `<key>.deleted.json` exists.

#### Scenario: Stub deletion
- **WHEN** a stored project at `sync/projects/P1.json` is deleted and the API returns only `{"id": "P1", "is_deleted": true}`
- **THEN** `sync/projects/P1.json` keeps its previous content and `sync/projects/P1.deleted.json` contains `{"id": "P1", "is_deleted": true}`

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
The tool SHALL write a `README.md` into the backup directory that describes the layout, file encoding, key and file-name rules, tombstones and `state.json`, together with the format version. The same description SHALL be kept in the project repository.

#### Scenario: Format description in the backup
- **WHEN** a run completes
- **THEN** the backup's `README.md` describes the format version recorded in `state.json`
