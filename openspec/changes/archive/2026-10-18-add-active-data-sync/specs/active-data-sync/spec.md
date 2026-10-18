# Spec Delta

## Purpose

Defines how todoist-takeout copies the account's active data from the Todoist sync endpoint into the backup: a full sync first, incremental syncs afterwards, a checkpoint that makes runs resumable, and tombstones for deleted objects.

## ADDED Requirements

### Requirement: All active resource types
Every sync SHALL request all resource types (`resource_types=["all"]`). The tool SHALL store every resource the response contains, including projects, sections, labels, tasks, task comments, project comments, filters, reminders, collaborators, user and settings objects, and any resource type the tool does not know.

#### Scenario: Known resource types
- **WHEN** a sync response contains projects, sections, labels, tasks, task comments and project comments
- **THEN** each of these objects is stored in the backup

#### Scenario: Unknown resource type
- **WHEN** a sync response contains a top-level resource key that the tool does not know
- **THEN** the tool stores its value unchanged and prints a warning naming the key, and the run still exits with status 0

### Requirement: Initial full sync
When the backup has no checkpoint, the tool SHALL perform a full sync (`sync_token="*"`). Immediately afterwards it SHALL perform one incremental sync with the token it received, so that changes made while the server prepared the full data are included.

#### Scenario: First run
- **WHEN** the tool runs on an empty target directory
- **THEN** it requests a full sync, stores all returned objects, performs one incremental sync with the returned token, applies it, and stores the newest token as the checkpoint

### Requirement: Incremental sync
When the backup has a checkpoint, the tool SHALL request an incremental sync with the stored sync token. It SHALL apply only the objects the response contains and leave every other stored object untouched.

#### Scenario: One task changed
- **WHEN** a task's content was edited in Todoist since the last run
- **THEN** the run updates only that task's stored object and the checkpoint

#### Scenario: Nothing changed
- **WHEN** nothing changed in Todoist since the last run
- **THEN** the run rewrites no object file and the stored objects are unchanged

### Requirement: Server-initiated full sync
When the API answers an incremental request with a full sync (`full_sync` is true), the tool SHALL apply it like an initial full sync. It SHALL NOT mark stored objects as deleted only because they are missing from that response, and it SHALL print a warning with the number of stored objects the response did not contain.

#### Scenario: Token no longer accepted
- **WHEN** the API returns `full_sync: true` for an incremental request, and 3 stored live objects are missing from the response
- **THEN** the tool stores all returned objects, keeps the 3 missing objects unchanged, warns that 3 stored objects were not in the full sync, and exits with status 0

### Requirement: Tombstones for deleted objects
When a sync response marks an object as deleted (`is_deleted` is true), the tool SHALL keep it in the backup as deleted and SHALL NOT remove any file, except as the Restored objects requirement allows. Afterwards, the stored data SHALL include the deletion payload as the API returned it and, if the backup held the object as live, the object's last live content unchanged.

#### Scenario: Deletion payload with blanked values
- **WHEN** a stored task is deleted in Todoist and the sync returns it with all of its fields, `is_deleted: true` and emptied values such as `content: ""`
- **THEN** the stored task keeps its previous content, and the deletion payload is stored next to it as a tombstone

#### Scenario: Deletion payload is a stub
- **WHEN** a stored task is deleted in Todoist and the sync returns a payload with `is_deleted: true` that lacks top-level fields of the stored task
- **THEN** the stored task keeps its previous content, and the deletion payload is stored next to it as a tombstone

#### Scenario: Deleted before it was ever stored
- **WHEN** the sync reports an object as deleted that the backup has never stored
- **THEN** the tool stores the deletion payload as that object

### Requirement: Restored objects
When a sync returns an object as live (`is_deleted` is not true) that the backup stores as deleted, the tool SHALL store the returned payload and remove the object's tombstone file, so that the object no longer counts as deleted.

#### Scenario: Deleted object restored
- **WHEN** a task is stored as deleted, and a later sync returns it with `is_deleted: false`
- **THEN** the stored task becomes that payload, its tombstone file is removed, and the task no longer counts as deleted

### Requirement: Comments on active tasks and projects
The backup SHALL include the task comments (`notes`) and project comments (`project_notes`) that the sync returns, with their attachment metadata as it appears in the payload. Attachment files SHALL NOT be downloaded.

#### Scenario: Comment with attachment
- **WHEN** a task comment carries a `file_attachment` object
- **THEN** the stored comment contains that object unchanged and no attachment file is downloaded

### Requirement: Checkpoint advances only after the data is stored
The tool SHALL store a new sync token as the checkpoint only after every object of the corresponding response has been written. If a run ends early, the next run SHALL continue from the last stored checkpoint.

#### Scenario: Interrupted while writing objects
- **WHEN** a run is killed after writing some of the objects of a response but before the checkpoint is updated
- **THEN** the checkpoint still holds the previous token, and the next run brings the backup to the same state as an uninterrupted run

### Requirement: No repeated download after interruption
The tool SHALL keep a downloaded sync response on disk until it has been applied and checkpointed. A run that finds such a response SHALL apply it before making a new sync request, instead of downloading it again.

#### Scenario: Killed after download
- **WHEN** the first run is killed after it received the full sync response but before it finished applying it
- **THEN** the next run applies the saved response without requesting a new full sync, then continues incrementally

### Requirement: Idempotent runs
Two consecutive runs with no change in Todoist in between SHALL leave the backup with identical object files.

#### Scenario: Repeat run
- **WHEN** a run completes and a second run follows with no change in Todoist
- **THEN** every object file has the same content after the second run as after the first, and only run metadata in the state file changes
