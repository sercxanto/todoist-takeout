# Spec Delta

## REMOVED Requirements

### Requirement: Attachments are not downloaded
**Reason**: Uploaded attachment files are now downloaded, for the comments of archived projects as for all other stored comments.
**Migration**: None. The metadata stays in the snapshot unchanged, as the requirement "Snapshot of an archived project" and the raw-payload rule of `backup-storage` already require. The files are covered by the `attachment-downloads` capability, and the next run downloads them for existing snapshots.

## ADDED Requirements

### Requirement: Attachment metadata in snapshots
Comments in a snapshot SHALL keep their attachment metadata as it appears in the payload. Their uploaded files are downloaded as the `attachment-downloads` capability describes, and are not part of the snapshot directory.

#### Scenario: Comment with attachment
- **WHEN** a comment of an archived project's task carries a `file_attachment` object
- **THEN** the stored comment contains that object unchanged, and its file, once downloaded, is under `attachments/` and not under `archived/`
