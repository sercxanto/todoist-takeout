# Spec Delta

## MODIFIED Requirements

### Requirement: Comments of completed tasks
For each completed task whose payload has a `note_count` greater than 0, the tool SHALL back up its comments with their attachment metadata as it appears in the payload. Uploaded attachment files are downloaded as the `attachment-downloads` capability describes. A comment once stored SHALL stay in the backup, even when it is later deleted in Todoist.

#### Scenario: Task with comments
- **WHEN** a completed task with `note_count: 2` is stored for the first time
- **THEN** the tool stores its two comments, including any `file_attachment` object unchanged

#### Scenario: Task without comments
- **WHEN** a completed task has `note_count: 0`
- **THEN** the tool makes no comment request for it

#### Scenario: Comments not on the plan
- **WHEN** the plan information says comments are not available
- **THEN** the tool makes no comment request for completed tasks
