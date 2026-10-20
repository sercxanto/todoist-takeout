# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

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
