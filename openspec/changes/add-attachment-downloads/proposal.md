# Proposal

> Status: the scope and the design questions were settled with the user in an explore session (2026-10-19). The API questions are open until the probes of tasks 1.1 to 1.3 have run; see Open Questions. Design D2 (where the download URL comes from) depends on their answer.

## Why

The backup keeps the metadata of every comment's `file_attachment`, but not the file itself, so the files people upload to Todoist, often the most valuable part of a comment, such as a scanned invoice or a photo, are lost when the account or the service goes away. All the places that store comments are in place now (`sync/`, `completed/`, `archived/`), so the files can be added once for all of them.

## What Changes

- Every run ends with a new step that downloads the uploaded files of all comments stored in the backup: task and project comments in `sync/`, the comments of completed tasks in `completed/comments/`, and the comments of archived projects in `archived/`.
- Only uploaded files are downloaded. Attachments that are links to other websites are not.
- Each file is stored once, at `attachments/<comment id>/<file name>`, however many copies of its comment the backup holds. The file name is the attachment's `file_name`, cleaned up to be valid on Windows, macOS and Linux while staying readable.
- A file that is stored is never downloaded again. Files are streamed to disk, checked against the size the API reports, and written atomically, so an interrupted run leaves no partial file behind.
- Downloading is always on; there is no option to turn it off and no size limit.
- A file that the server refuses (any HTTP 4xx other than 429, for example 403, 404 or 410) produces a warning and is tried again in every later run. A 401 from the file host is such a refusal, not an invalid token, because the API accepted the token earlier in the same run. Network and server errors that persist after the retries end the run with exit status 1, as for every other request.
- The token is sent only to Todoist's own hosts over HTTPS. Redirects are followed without the token, and an attachment on any other host is skipped with a warning.
- The format description documents `attachments/` and how a reader finds the file of a comment.
- The requirements that say attachment files are not downloaded are replaced in `active-data-sync`, `completed-tasks-backup` and `archived-projects-backup`.

## Capabilities

### New Capabilities

- `attachment-downloads`: finding the uploaded files of stored comments, downloading each one once, the host and redirect rule for the token, and how failures are reported.

### Modified Capabilities

- `backup-storage`: adds the `attachments/` layout and its file-name rules, and its place in the format description.
- `api-access`: requests for attachment files go to Todoist's file hosts, not only to `https://api.todoist.com/api/v1/`, with the token rule above; the invalid-token rule applies to API requests only.
- `active-data-sync`: the requirement "Comments on active tasks and projects" no longer forbids downloading attachment files.
- `completed-tasks-backup`: the requirement "Comments of completed tasks" no longer forbids downloading attachment files.
- `archived-projects-backup`: the requirement "Attachments are not downloaded" is replaced by a pointer to `attachment-downloads`.

## Impact

- New module for the download step, called from `run_backup` in `sync.py` after `archived.run`.
- `api.py`: a second transport path that streams a response body to a file, sends the token only to allowed hosts and follows redirects without it. The existing transport, which never follows redirects, stays as it is for API requests.
- `storage.py`: a streaming variant of the atomic write, and the readable file-name cleanup.
- `storage_format.md`, the project `README.md` (backup scope, roadmap item 4, first-run advice) and the expected tree of the cross-platform test.
- Requests: one per uploaded file not yet stored. The first run on an account with many attachments transfers all of them; later runs only new ones. Disk use grows by the size of the account's files.
- No new runtime dependency.

## Open Questions

To be answered by the probes (tasks 1.1 to 1.3), read-only on the Pro account and with writes on the Free account, which allows uploads up to 5 MB:

- Which values of `resource_type` mark uploaded files (for example `file`, `image`, `audio`, `video`), and which marks a link; which other fields the object has (thumbnails, `upload_state`).
- Whether `file_url` needs the token, whether it redirects, and to which host.
- Whether `file_url` is stable or signed with an expiry. If it expires, the URL of a frozen copy (a closed month, an archived snapshot) cannot be used, and design D2 must fetch a fresh one.
- Whether the downloaded bytes match `file_size`, and whether the file host sends gzip.
- Whether the file of a deleted comment can still be downloaded.
- Which rate-limit headers the file host sends.
- Whether fields other than `file_attachment`, outside comments, refer to uploaded files.
