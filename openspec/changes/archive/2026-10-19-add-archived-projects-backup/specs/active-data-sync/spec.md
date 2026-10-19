# Spec Delta

## MODIFIED Requirements

### Requirement: Server-initiated full sync
When the API answers an incremental request with a full sync (`full_sync` is true), the tool SHALL apply it like an initial full sync. It SHALL NOT mark stored objects as deleted only because they are missing from that response, and it SHALL print a warning with the number of stored objects the response did not contain. Objects that leave the sync because they were completed or archived SHALL NOT count: tasks stored with `checked: true`, projects and sections stored with `is_archived: true`, objects whose `project_id` names such a project, and comments of such tasks.

#### Scenario: Token no longer accepted
- **WHEN** the API returns `full_sync: true` for an incremental request, and 3 stored live objects are missing from the response
- **THEN** the tool stores all returned objects, keeps the 3 missing objects unchanged, warns that 3 stored objects were not in the full sync, and exits with status 0

#### Scenario: Completed tasks
- **WHEN** the backup stores 40 tasks with `checked: true`, and a server-initiated full sync contains none of them and misses no other stored object
- **THEN** the tool keeps the 40 tasks unchanged and prints no warning about missing objects

#### Scenario: Archived project
- **WHEN** the backup stores project `P1` with `is_archived: true` together with one of its sections, one open task, one completed task, a comment on the open task and a project comment, and a server-initiated full sync contains none of them
- **THEN** the tool keeps the six objects unchanged and prints no warning about missing objects

#### Scenario: Archived section
- **WHEN** the backup stores a section with `is_archived: true` and a server-initiated full sync does not contain it
- **THEN** the tool keeps the section unchanged and does not count it as missing

#### Scenario: Mixed
- **WHEN** a server-initiated full sync misses 2 stored live tasks with `checked: false` and 10 stored tasks with `checked: true`
- **THEN** the tool warns that 2 stored objects were not in the full sync
