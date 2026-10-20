# Spec Delta

## MODIFIED Requirements

### Requirement: Comments on active tasks and projects
The backup SHALL include the task comments (`notes`) and project comments (`project_notes`) that the sync returns, with their attachment metadata as it appears in the payload. Uploaded attachment files are downloaded as the `attachment-downloads` capability describes.

#### Scenario: Comment with attachment
- **WHEN** a task comment carries a `file_attachment` object
- **THEN** the stored comment contains that object unchanged
