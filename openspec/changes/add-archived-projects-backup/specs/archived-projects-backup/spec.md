# Spec Delta

## Purpose

Defines how todoist-takeout backs up archived projects, which the sync endpoint leaves out: one snapshot per archived project with its open and completed tasks, sections, sub-projects and comments, fetched once and kept even after the project changes again.

## ADDED Requirements

### Requirement: Archived projects are listed
On every run, the tool SHALL list the account's archived projects through `GET /api/v1/projects/archived`, following every page, and SHALL back up each listed project that has no current snapshot in the backup.

#### Scenario: First run
- **WHEN** the account has 56 archived projects and the backup has none of them
- **THEN** the run stores a snapshot of each of the 56 projects

#### Scenario: Several pages
- **WHEN** the list of archived projects arrives in two pages
- **THEN** the projects of both pages are backed up

#### Scenario: No archived projects
- **WHEN** the account has no archived project
- **THEN** the run makes no request for archived projects other than the list, and exits with status 0

### Requirement: Snapshot of an archived project
The snapshot of an archived project SHALL hold the project's payload as listed, the response of `GET /api/v1/projects/{id}/full` with its open tasks, sections and sub-projects, the project's completed tasks, the comments of its tasks whose `note_count` is greater than 0, and the project's comments.

#### Scenario: Project with open and completed tasks
- **WHEN** an archived project has 3 open tasks, 2 sections and 18 completed tasks
- **THEN** its snapshot holds the 3 open tasks and 2 sections as `/full` returned them, and the 18 completed tasks

#### Scenario: Task with comments
- **WHEN** a task of an archived project, open or completed, has `note_count: 2`
- **THEN** the snapshot holds its two comments

#### Scenario: Task without comments
- **WHEN** a task of an archived project has `note_count: 0`
- **THEN** the tool makes no comment request for it

#### Scenario: Project comments
- **WHEN** an archived project has one project comment
- **THEN** the snapshot holds that comment

#### Scenario: Comments of a task gone
- **WHEN** the comment request of a task with `note_count: 2` answers 404
- **THEN** the snapshot holds an empty comment file for it, and later runs make no comment request for it

### Requirement: Completed tasks over the project's lifetime
The tool SHALL fetch an archived project's completed tasks from `GET /api/v1/tasks/completed/by_completion_date` filtered by the project's `id`, in consecutive windows of at most 3 months from the month of the project's `created_at` through the month of the project's `updated_at`, or through the current month if that is earlier or `updated_at` is not parseable, following every page.

#### Scenario: Project created three years ago
- **WHEN** an archived project was created in October 2023, its `updated_at` is in June 2025, and the run happens in October 2026
- **THEN** the tool requests 7 windows, the first starting on 2023-10-01 and the last ending on 2025-06-30

#### Scenario: Archive date unknown
- **WHEN** an archived project was created in October 2023, its payload has no parseable `updated_at`, and the run happens in October 2026
- **THEN** the tool requests 13 windows, the last ending after the current month

#### Scenario: Creation date unknown
- **WHEN** an archived project's payload has no parseable `created_at`
- **THEN** the windows start at the account's first month of completed tasks

### Requirement: Attachments are not downloaded
Comments in a snapshot SHALL keep their attachment metadata as it appears in the payload. The tool SHALL NOT download attachment files.

#### Scenario: Comment with attachment
- **WHEN** a comment of an archived project's task carries a `file_attachment` object
- **THEN** the stored comment contains that object unchanged and no attachment file is downloaded

### Requirement: Snapshots are fetched once
The tool SHALL NOT fetch a project's snapshot again while the payload in the list of archived projects equals the stored one. Changes made to the content of an archived project after its snapshot was stored are not captured.

#### Scenario: Repeat run
- **WHEN** a run follows another with no change to the archived projects in Todoist
- **THEN** it requests only the list of archived projects and rewrites no file of any snapshot

### Requirement: Changed or re-archived project
When the listed payload of a project differs from the stored one, for example because it was unarchived and archived again, the tool SHALL fetch the project's snapshot again and replace its project payload, `/full` response and completed tasks. Comments already stored SHALL stay, and new or changed comments SHALL be added.

#### Scenario: Unarchived and archived again
- **WHEN** a stored archived project is unarchived, a task is added to it, and it is archived again
- **THEN** the next run fetches its snapshot again, and the snapshot holds the new task

#### Scenario: Comment deleted before the project was archived again
- **WHEN** a comment stored in a snapshot was deleted in Todoist before the project was archived again
- **THEN** the comment stays in the snapshot

### Requirement: Archived sub-projects
The tool SHALL also back up each archived project that appears among the `subprojects` of a backed-up archived project, with a snapshot of its own, even if the list of archived projects does not contain it. Each project SHALL be backed up once per run.

#### Scenario: Sub-project only in its parent
- **WHEN** the parent project `P` is listed as archived and its `/full` response holds the archived sub-project `C`, which the list does not contain
- **THEN** the backup holds a snapshot of `P` and a snapshot of `C`

#### Scenario: Sub-project listed too
- **WHEN** the sub-project `C` is listed as archived and also appears among the `subprojects` of `P`
- **THEN** the run fetches `C` once

### Requirement: Resumable archived-projects backup
A snapshot SHALL count as stored only after all its parts are written. If a run ends early, the next run SHALL fetch the snapshot of an unfinished project again, and SHALL NOT fetch the snapshots it already finished.

#### Scenario: Killed in the middle of a project
- **WHEN** the first run is killed after it finished 20 snapshots and wrote part of the 21st
- **THEN** the next run fetches the 21st project again and makes no `/full` request for the first 20

### Requirement: Snapshots are kept
A snapshot SHALL stay in the backup when its project is unarchived or deleted in Todoist. The tool SHALL NOT remove any file of a snapshot.

#### Scenario: Project unarchived
- **WHEN** a project with a stored snapshot is unarchived
- **THEN** the snapshot stays unchanged, and the sync stores the project's active data under `sync/` again

#### Scenario: Project deleted
- **WHEN** a project with a stored snapshot is deleted in Todoist
- **THEN** the snapshot stays unchanged

### Requirement: Archived sections
The tool SHALL keep a section that the sync reported as archived, with the payload the sync returned, although later full syncs and `/full` responses no longer contain it. The format description SHALL state that sections archived before the first backup are not in the backup, apart from the `section_id` of their tasks.

#### Scenario: Section archived after the first backup
- **WHEN** a stored section is archived in Todoist, and a later server-initiated full sync no longer contains it
- **THEN** the backup holds the section with `is_archived: true` as the incremental sync returned it

### Requirement: Plan limits for archived projects
When the plan excludes completed tasks or comments, the tool SHALL store the snapshot without that part. A later run on a plan that includes it SHALL add the missing part to the stored snapshots. When a request of this part is rejected as restricted by the plan, the tool SHALL warn naming the part and skip it. The run SHALL finish with exit status 0.

#### Scenario: Plan without completed tasks
- **WHEN** the plan information says `completed_tasks: false`
- **THEN** the snapshots hold no completed tasks, the tool makes no request to `by_completion_date`, and the run exits with status 0

#### Scenario: Plan upgraded
- **WHEN** snapshots were stored without completed tasks, and a later run finds `completed_tasks: true`
- **THEN** that run fetches the completed tasks of each stored snapshot that lacks them, and does not fetch the rest of those snapshots again

#### Scenario: Comments allowed again after a refetch without them
- **WHEN** a snapshot with `project_comments.json` was fetched again while the plan excluded comments, and a later run finds `comments: true`
- **THEN** that run fetches the comments of the snapshot's tasks that have `note_count` > 0 and no comment file

#### Scenario: List restricted
- **WHEN** the request for the list of archived projects is rejected as restricted by the plan
- **THEN** the tool warns that archived projects cannot be backed up, stores everything else, and exits with status 0

### Requirement: Project gone during the run
When `/full` for a listed project answers HTTP 404, the tool SHALL skip that project without failing the run, and SHALL NOT store a snapshot of it.

#### Scenario: Deleted between list and fetch
- **WHEN** a project is deleted after the list was fetched and its `/full` request answers 404
- **THEN** the tool stores no snapshot of it, backs up the other projects, and exits with status 0

### Requirement: Idempotent archived-projects backup
Two consecutive runs with no change in Todoist in between SHALL leave every file of every snapshot with the same content, and SHALL NOT rewrite any of them.

#### Scenario: Repeat run
- **WHEN** a run completes and a second run follows with no change in Todoist
- **THEN** every snapshot file keeps its content and modification time
