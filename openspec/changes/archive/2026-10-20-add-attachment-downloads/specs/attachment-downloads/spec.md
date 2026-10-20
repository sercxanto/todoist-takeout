# Spec Delta

## Purpose

Downloads the files that users uploaded to comments, so that the backup holds the attached files and not only their metadata, each file once and never twice.

## ADDED Requirements

### Requirement: Uploaded files of stored comments are downloaded
After the active data, the completed tasks and the archived projects are backed up, the tool SHALL download the uploaded file of every comment stored anywhere in the backup whose `file_attachment` describes an uploaded file. This covers task and project comments under `sync/`, `completed/comments/` and `archived/`, including comments marked as deleted.

#### Scenario: Comment of an active task
- **WHEN** `sync/notes/N1.json` holds a comment whose `file_attachment` describes an uploaded file `preise.pdf`
- **THEN** the run stores that file at `attachments/N1/preise.pdf`

#### Scenario: Comment of an archived project
- **WHEN** `archived/P1/project_comments.json` holds a comment `N2` with an uploaded file
- **THEN** the run stores that file under `attachments/N2/`

#### Scenario: Comment of a completed task
- **WHEN** `completed/comments/T1.json` holds a comment `N3` with an uploaded file
- **THEN** the run stores that file under `attachments/N3/`

#### Scenario: Deleted comment
- **WHEN** a stored comment with an uploaded file has `is_deleted: true` and its file is not stored yet
- **THEN** the run requests the file once in that run

#### Scenario: Backup made by an earlier version
- **WHEN** the tool runs on a backup that a version without this step wrote, with comments that have uploaded files
- **THEN** the run downloads all of those files, without any migration step

### Requirement: Only uploaded files
The tool SHALL download only attachments that are files uploaded to Todoist. It SHALL NOT request attachments that link to another website, and SHALL NOT request an uploaded file whose upload is not complete. A comment without `file_attachment`, or with `file_attachment: null`, SHALL produce no request.

#### Scenario: Link attachment
- **WHEN** a stored comment's `file_attachment` describes a link to a web page
- **THEN** the run makes no request for it and stores nothing under `attachments/` for that comment

#### Scenario: Upload not complete
- **WHEN** a stored comment's `file_attachment` has an `upload_state` other than `completed`
- **THEN** the run makes no request for it, and a later run downloads it once a stored copy of the comment reports the upload as completed

### Requirement: One file per comment
The tool SHALL store the file of a comment once, keyed by the comment's `id`, however many copies of that comment the backup holds. Within one run it SHALL request the file of a comment at most once.

#### Scenario: Comment stored twice
- **WHEN** comment `N1` with an uploaded file is stored both in `completed/comments/T1.json` and in `archived/P1/comments/T1.json`
- **THEN** the run makes one request for the file and stores it once, under `attachments/N1/`

### Requirement: Stored files are not downloaded again
The tool SHALL NOT request the file of a comment whose directory under `attachments/` already holds a file, other than a temporary file. Whether a file is stored SHALL be decided from the directory alone, without a request and whatever the name of the stored file.

#### Scenario: Repeat run
- **WHEN** a run finds every uploaded file of the stored comments already under `attachments/`
- **THEN** it makes no request for attachment files and rewrites no file under `attachments/`

#### Scenario: File name rules changed
- **WHEN** `attachments/N1/` holds a file whose name differs from what the current file-name rules produce for its `file_name`
- **THEN** the run makes no request for the file of `N1` and leaves the stored file unchanged

### Requirement: Complete files only
The tool SHALL write an attachment file only when its download is complete. When the payload gives a `file_size`, a download whose length differs from it SHALL NOT be stored; the tool SHALL report a warning naming the comment and try again in the next run. An interrupted download SHALL leave no file under the comment's directory except a temporary file, which the next run removes.

#### Scenario: Size matches
- **WHEN** the downloaded file has exactly as many bytes as the payload's `file_size`
- **THEN** the run stores it under the comment's directory

#### Scenario: Size differs
- **WHEN** the payload's `file_size` is 20480 and the download yields 10240 bytes
- **THEN** the run stores no file for that comment, reports a warning naming the comment, finishes with exit status 0, and the next run requests the file again

#### Scenario: Killed during a download
- **WHEN** the process is killed while it writes an attachment file
- **THEN** the comment's directory holds no file other than a temporary file, and the next run removes the temporary file and downloads the file again

### Requirement: Refused files produce a warning
When the server answers a request for an attachment file with an HTTP 4xx status other than 429, or with a redirect the tool does not follow, the tool SHALL report a warning that names the comment's `id` and the status, continue with the other files, and keep no record of the failure, so that every later run requests the file again. Such failures SHALL NOT change the exit status.

#### Scenario: Access denied by the file host
- **WHEN** the request for the file of comment `N1` is answered with HTTP 401 or 403
- **THEN** the run reports a warning naming `N1` and the status, does not treat it as an invalid token, and exits with status 0

#### Scenario: File gone
- **WHEN** the request for the file of comment `N1` is answered with HTTP 404
- **THEN** the run reports a warning naming `N1` and 404, downloads the other files, and exits with status 0

#### Scenario: Next run after a refused file
- **WHEN** the previous run reported a warning for the file of `N1`, and the file is still not stored
- **THEN** the next run requests the file of `N1` again

### Requirement: Persistent failures end the run
A request for an attachment file that still fails after the retries of the API-access requirements, because of a network error, a timeout or an HTTP 5xx status, or that hits the rate-limit cap, SHALL end the run with exit status 1, as any other failed request does. Files stored before the failure SHALL stay stored.

#### Scenario: Network down during the downloads
- **WHEN** every attempt to request an attachment file fails with a connection error
- **THEN** the run stops, the files already stored under `attachments/` stay, and the exit status is 1

#### Scenario: Rate limit on the file host
- **WHEN** a request for an attachment file is answered with HTTP 429 and `Retry-After: 12`
- **THEN** the tool waits at least 12 seconds and repeats the request, counting the wait toward the run's rate-limit cap

### Requirement: Attachment on an unexpected host
The tool SHALL request an attachment file only when its URL uses HTTPS and its host is `todoist.com` or ends in `.todoist.com`. For any other URL it SHALL make no request and report a warning naming the comment and the host.

#### Scenario: Foreign host
- **WHEN** a stored comment's uploaded file has a `file_url` on `example.com`
- **THEN** the run makes no request to `example.com`, reports a warning naming the comment and `example.com`, and exits with status 0

#### Scenario: Plain HTTP
- **WHEN** a stored comment's uploaded file has a `file_url` that starts with `http://`
- **THEN** the run makes no request for it and reports a warning naming the comment
