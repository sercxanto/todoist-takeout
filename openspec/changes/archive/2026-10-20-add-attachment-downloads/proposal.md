# Proposal

> Status: the scope and the design questions were settled with the user in an explore session (2026-10-19). The API questions were answered by the probes R8 and W9 on 2026-10-19; see API findings. None of them changed a spec.

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

## API findings

From probe R8 (read-only, Pro account) and W9 (writes, Free account), 2026-10-19. The findings files are not committed; this section is their record.

- **Where files are referenced.** Only comments' `file_attachment` refers to uploaded files. Other fields of the sync whose names contain `url`, `file` or `image` are avatars (`image_id` of `user` and `collaborators`) and `user.websocket_url`. The Pro account held no uploaded file in its active comments, in the comments of its recent completed tasks or in those of the archived projects read, so all file facts come from W9.
- **Shape of an upload.** `POST /api/v1/uploads` returns `file_name`, `file_size`, `file_type`, `file_url`, `image`, `image_width`, `image_height`, `resource_type` and `upload_state`; `note_add` takes that object as `file_attachment`. A text file has `resource_type: file`, a PNG `resource_type: image`, with a thumbnail URL in `image` and, once the comment exists, `tn_l`, `tn_m` and `tn_s` arrays. Both have `upload_state: completed`. `file_size` equals the uploaded length.
- **Shape of a link.** `resource_type: url`, with `url` and `title`, no `file_url` and no `file_size`; `upload_state` is `completed` here too.
- **`file_url`.** On `files.todoist.com`, over HTTPS, without a query string. The comment's `file_url` differs from the one the upload returned, but is the same in the sync, in `GET /comments`, after a full sync and after the project is archived. Nothing in it marks a signature or an expiry. Whether a years-old URL still works could not be checked: the Pro account has no uploaded file.
- **Token and redirects.** With the token, `files.todoist.com` answers 302 to a CloudFront host (`*.cloudfront.net`, outside `todoist.com`) with a signed query string, which serves the file with 200 and needs no token. Without the token, `files.todoist.com` redirects three times to `app.todoist.com` and ends with a 200 HTML login page, not with an error status.
- **Bytes.** With `Accept-Encoding: identity` the file host sends `Content-Length` equal to `file_size` and no `Content-Encoding`; the bytes match `file_size`. With `Accept-Encoding: gzip` it compresses a text file, and the decoded bytes match.
- **Deleted comments.** After `note_delete`, the sync returns the comment with `is_deleted: true` and `file_attachment: null`, and its `file_url` answers 403 even with the token. The same holds once the upload is deleted.
- **Rate limits.** Neither the file host nor CloudFront sent a rate-limit header.

## Open Questions

Answered by the probes (tasks 1.1 to 1.3); see API findings.

- Which values of `resource_type` mark uploaded files, and which marks a link: `file` and `image` were observed for uploads, `url` for a link. Uploads also carry `image`, `image_width`, `image_height`, `tn_l`, `tn_m`, `tn_s` and `upload_state`.
- Whether `file_url` needs the token, whether it redirects, and to which host: it needs the token, and redirects once to a CloudFront host, which needs none.
- Whether `file_url` is stable or signed with an expiry: stable as far as could be observed; the signature is in the redirect, made fresh for each request. Design D2 keeps the stored URL.
- Whether the downloaded bytes match `file_size`, and whether the file host sends gzip: they match; gzip only when asked for.
- Whether the file of a deleted comment can still be downloaded: no, it answers 403. A synced deleted comment has `file_attachment: null`.
- Which rate-limit headers the file host sends: none.
- Whether fields other than `file_attachment`, outside comments, refer to uploaded files: no.
