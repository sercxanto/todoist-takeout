# Spec Delta

## MODIFIED Requirements

### Requirement: Directory layout
A backup directory SHALL contain `state.json`, a format description `README.md`, and a `sync/` directory, a `completed/` directory once completed tasks are backed up, and an `archived/` directory once archived projects are backed up. A resource that is a collection of objects SHALL be stored as one file per object at `sync/<resource>/<key>.json`. A resource that is a single value SHALL be stored at `sync/<resource>.json`. `<resource>` is the top-level key of the sync response, for example `items`, `projects` or `notes`.

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

### Requirement: Documented format
The tool SHALL write a `README.md` into the backup directory that describes the layout, file encoding, key and file-name rules, tombstones, the files under `completed/` and `archived/` and `state.json`, together with the format version. It SHALL name every place where the same task can be stored, and how a reader tells which copy is current. The same description SHALL be kept in the project repository.

#### Scenario: Format description in the backup
- **WHEN** a run completes
- **THEN** the backup's `README.md` describes the format version recorded in `state.json`

#### Scenario: Large integers
- **WHEN** a reader consults the description of the activity log's events
- **THEN** it warns that their `id` values exceed 64 bits and that tools which parse numbers as 64-bit integers or doubles, such as JavaScript or an SQLite `INTEGER` column, corrupt them

#### Scenario: Copies of a task
- **WHEN** a reader looks up where a completed task of an archived project can be stored
- **THEN** the description names `sync/items/`, `completed/<YYYY-MM>.json` and `archived/<project id>/completed.json`, states that all copies share the task's `id`, and gives the rule for choosing the current one

## ADDED Requirements

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
