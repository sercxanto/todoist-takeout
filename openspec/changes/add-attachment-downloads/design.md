# Design

## Context

This change builds on three archived changes, cited as "sync D<n>" (`2026-10-18-add-active-data-sync`), "completed D<n>" (`2026-10-18-add-completed-tasks-backup`) and "archived D<n>" (`2026-10-19-add-archived-projects-backup`). The code it builds on:

- `api.py`: `UrllibTransport` sends every request to `BASE_URL` with the token, decodes gzip, reads the whole body into memory, and never follows a redirect (`_NoRedirect`), so that the token cannot leak. `Client.request` holds the retry loop: backoff for network errors and 5xx, the rate-limit wait with its per-run cap (`rate_limit_waited`), and `_classify` for other statuses.
- `storage.py`: `write_bytes_atomic` (temp file `.<name>.<hex>.tmp`, fsync, replace, directory fsync, the fault-injection hook), `remove_temp_files` over the whole target, `encode_component`, `MAX_NAME_LENGTH = 200`, `FORMAT_VERSION = 1`.
- `sync.py`: `run_backup` runs the sync rounds, `completed.run`, `archived.run`, then the plan warnings, all under the target lock.
- Comments are stored as one object per file in `sync/notes/` and `sync/project_notes/`, and as response-shaped files (`{"results": [...]}`) in `completed/comments/<task id>.json`, `archived/<project id>/comments/<task id>.json` and `archived/<project id>/project_comments.json`.
- The hand-written fixtures carry one `file_attachment` (`resource_type: file`, `file_url` on `example.com`). The recorded fixtures of both accounts hold no attachment at all; the real shape comes from the probes R8 and W9 (proposal, API findings).
- The plan limits of the Free account allow uploads (`uploads: true`, `upload_limit_mb: 5`), so the write probe can upload there.

## Goals / Non-Goals

**Goals:**

- One new module that runs last, inside the same lock and run, and needs no checkpoint of its own: the directory under `attachments/` is the record.
- Memory use independent of file size: bodies are streamed to disk.
- The existing API transport keeps its rule of never following a redirect; the file path gets its own, narrower rule.
- Offline tests for every path, with a fake file transport.

**Non-Goals:**

- Link attachments, thumbnails and previews, user and collaborator avatars (`image_id`), and workspace logos.
- Resuming a partial download with HTTP range requests. An interrupted file is downloaded again from the start.
- Checksums beyond the length check. The API offers none.
- Parallel downloads.
- Changing the summary line of the CLI. Warnings are already counted there.

## Decisions

### D1: Module and run sequence

A new module `attachments.py`. `run_backup` calls `attachments.run(target, client, now)` after `archived.run` and before the plan warnings:

```
… sync rounds, completed.run, archived.run (unchanged) …
attachments.run(…)
    1. collect   read every stored comment file, locally, no request
    2. filter    uploaded files only (D3), one entry per comment id (D2)
    3. skip      entries whose attachments/<id>/ already holds a file (D4)
    4. download  one at a time, streamed, size-checked, atomic (D6, D7)
_warn_about_plan_limits(…)
state.json written last (unchanged)
```

Downloads come last because they are the largest and least urgent part: when the rate limit or the network ends a run early, the metadata, above all the activity log with its limited history, is already stored.

Collecting from the whole backup rather than from the comments a run has just written makes every run catch up on what earlier runs could not download, and gives existing backups their files on the first run of the new version, without a migration. The cost is local reads only.

*Alternative:* hooking into each writer of comments (`sync`, `completed`, `archived`). Rejected: three places to change, and files missed by an earlier run would never be retried.

### D2: Which copy of a comment supplies the URL

A comment can be stored in up to two places, for example a completed task of an archived project. The entries are grouped by comment `id`; the copy used is the first one found in the order `sync/`, `completed/comments/`, `archived/`. `sync/` comes first because every incremental sync updates it; the other two are frozen sooner.

The stored `file_url` is used as it is. The probes found it stable: it carries no query string and no expiry, it is the same in every copy of a comment and after the project is archived, and the signed, short-lived URL is only the one the file host redirects to, made fresh for each request (proposal, API findings).

*Alternative:* fetching the comment once with `GET /api/v1/comments/{id}` before downloading, and using the fresh `file_url`. Not chosen: it doubles the requests for every new file and the probes gave no reason for it. Should old URLs turn out to fail, this is the fallback; it changes no spec.

### D3: Which attachments are uploaded files

An entry is downloaded when its `file_attachment` is an object, has a `file_url`, has `upload_state` equal to `completed` (or no `upload_state` at all), and has a `resource_type` that is not a link type. The link types are a fixed set in the code: `{"url"}`, the only one the probes saw. Uploads had `resource_type` `file` or `image`. A link attachment has no `file_url` either, so it is skipped twice over.

A deny list of link types, rather than an allow list of upload types, keeps an upload type that Todoist adds later from being skipped silently. An unknown link type still cannot leak the token: its URL points to a foreign host, which D5 skips with a warning, so it becomes visible and can be added to the set.

### D4: Layout and the "already stored" check

`attachments/<encode_component(comment id)>/<file name>` (spec: Attachments layout). A comment counts as stored when its directory holds at least one entry that is a file and not a temp file (`.*.tmp`). The file name is not compared, so a later change of the rules in D8 never triggers a download.

The directory is created only when the download starts, so a missing directory means not downloaded. A directory left with only a temp file, after a crash, counts as not stored; `remove_temp_files` at the start of the next run clears it, as for every other file.

The directory name follows the key rules of all other files (case-collision check included, archived D12); the file name inside it cannot collide, because it is the only one.

### D5: Host rule and redirects

A new function in `api.py` decides, for a URL, whether the tool may request it and whether the token goes with it:

```
file_url
  |
  +-- scheme https, host == "todoist.com" or endswith ".todoist.com" ?
  |      no  --> UnexpectedHost: warning, no request
  |      yes --> GET with Authorization
  |               |
  |               +-- 3xx with Location --> resolve against the URL,
  |               |                         https only, at most 5 hops,
  |               |                         GET without Authorization
  |               +-- 2xx --> stream (D6)
```

The host check compares the lower-cased host from `urllib.parse.urlsplit`, never a substring of the URL, so `todoist.com.example.net` and `evil-todoist.com` are rejected. A URL with user info (`user@host`) or an explicit port other than 443 is rejected as well.

Redirects are followed by hand, through the same `_NoRedirect` opener: urllib's own redirect handler copies the request headers, including `Authorization`, to the new URL. Every hop after the first goes without the token, also when it points back to a Todoist host. The probes confirmed that none needs it: `files.todoist.com` answers 302 to a CloudFront host outside `todoist.com`, with a signed query string, which serves the file without the token. A real download takes one redirect, well inside the limit of 5.

Only the host and path are logged, never the query string, because a signed URL carries its credential there.

*Alternative:* sending the token to every host listed in `file_url`. Rejected: `file_url` comes from the payload, and an unexpected host would receive the token.

### D6: Streaming transport and retries

The transport gets a second method, `download(url, token_allowed) -> response`, which returns the status, the headers and, for a 2xx, an open stream instead of the body. It asks for `Accept-Encoding: identity`, so the stored bytes are the file itself; if the host still sends `Content-Encoding: gzip`, the stream is decoded with `zlib.decompressobj` while it is written.

`Client.request` and a new `Client.download` share one retry loop, extracted from `request`: network errors and 5xx back off and retry up to `MAX_ATTEMPTS`, 429 waits through `_wait_for_rate_limit` and counts toward the same per-run cap. A network error while the body is streamed counts as a failed attempt; the next attempt starts the file from the beginning, into a new temp file.

Status handling for a file request (spec: Refused files produce a warning):

| Response | Result |
|---|---|
| 2xx | stream to a temp file, then D7 |
| 3xx | D5 |
| 429 | wait, retry |
| other 4xx, including 401 and 403 | `AttachmentRefused`: warning, next file |
| 5xx, network, timeout after the last attempt | `NetworkError` / `ApiError`: the run fails, exit 1 |
| rate-limit cap | `RateLimitExhausted`: the run fails, exit 1 |

`_classify` is not used for file requests: a 401 there must not become the invalid-token error of sync D4.

### D7: Streaming atomic write and size check

`storage.py` gets `write_stream_atomic(path, chunks, expected_size)`. It writes the chunks to a temp file in the target directory, in blocks of 64 KiB, fsyncs, compares the byte count with `expected_size` when it is not `None`, and only then replaces `path` and fsyncs the directory. On a mismatch it removes the temp file and raises `SizeMismatch`, which `attachments.run` reports as a warning (spec: Complete files only). The fault-injection hook counts it as one write, at the same point as in `write_bytes_atomic`, so the crash tests cover it.

`expected_size` is `file_size` from the payload when it is a non-negative integer, else `None`.

### D8: File-name cleanup

A pure function `attachment_file_name(file_name) -> str` in `storage.py`, applied in this order:

1. Not a string, or empty: `attachment`.
2. Unicode NFC.
3. Each character below U+0020, U+007F, and each of `< > : " / \ | ? *` becomes `_`.
4. Trailing spaces and dots are removed.
5. A leading dot becomes `_`.
6. If the part before the first dot, compared case-insensitively and with trailing spaces removed, is a reserved device name (`_DEVICE_NAMES`, extended by `CONIN$`, `CONOUT$` and `COM¹`…`LPT³`), `_` is prefixed.
7. If the name is longer than 100 characters or 200 bytes in UTF-8, the stem is cut, character by character, until both limits hold. The extension, the part from the last dot, is kept when it is at most 16 characters; a longer one counts as part of the stem. Step 4 runs again on the cut stem.
8. Empty after all steps: `attachment`.

Steps 3 to 6 are what Windows, macOS and Linux need for a name to be valid and visible; nothing else is changed, so most names stay as the user typed them. The original name stays in the comment's `file_name`, so nothing is lost when a name is changed.

*Alternative:* `encode_component`, as for all other files. Rejected with the user: `Preise%202024%2Epdf` is hard to read for someone browsing the backup without the tool.

### D9: Messages

- INFO at the start: `downloading N attachment files (M already stored)`, or nothing when N is 0.
- DEBUG per file: the comment id and the host.
- WARNING per refused file, size mismatch and unexpected host, naming the comment id and the reason. These repeat on every run until the file is stored, as decided.
- INFO at the end: `downloaded N attachment files (X MB); K could not be downloaded`.

### D10: Format description

`storage_format.md` gains a section "Attachment files": the layout, the rule that a directory holds exactly one file, the name rules of D8, how a reader finds the file of a comment (by `id`, from any of its copies), and that a missing directory means the file is not downloaded, for one of the reasons the warning names. The section "Comments" of each part refers to it instead of saying that files are not downloaded. `FORMAT_VERSION` stays 1: the change only adds a directory, and an older version of the tool ignores it.

### D11: Probes

The probe spike was removed by archived task 7.4, so the probes are written from scratch, in `tests/contract/probes.py` and `tests/contract/test_probe_*.py`, and removed again in task 7.4 of this change, once the results are in the proposal.

- **R8, read-only, Pro account:** walk the comments of the sync, of a few completed tasks and of the archived projects; for each `file_attachment` record the set of keys, `resource_type`, `upload_state`, the host of `file_url`, and for up to five uploaded files: the status without and with the token, each redirect hop (status and host only), `Content-Length`, `Content-Encoding` and whether the byte count equals `file_size`. Files are written to a temporary directory and deleted. Also list every field name in the sync response that contains `url`, `file` or `image`.
- **W9, writes, Free account:** upload a small text file and a small PNG through `POST /api/v1/uploads`, add them as comments with `note_add`, add a link comment, and record the same facts as R8. Then request the same `file_url` again after a full sync, compare it with the first one, archive the project and compare again, delete one comment and request its file once more. Clean up.

The findings files hold booleans, types, statuses and host names, never an ID, URL path, query or file content. They are not committed (as before, `tests/contract/findings/`); the results go into the proposal under "API findings".

### D12: Tests and fixtures

- A fake file transport in `tests/fake_api.py`, keyed by URL, that can answer with a body, a status, a redirect chain, gzip, a short body, or a network error during the body.
- The `file_url` of the hand-written fixtures moves from `example.com` to a `files.todoist.com` URL with invented paths, plus one fixture each for a link attachment, an incomplete upload and a foreign host.
- `tests/fixtures/expected_tree/` gains `attachments/` with the file of each fixture comment, so the cross-platform end-to-end test checks names and bytes on Linux, macOS and Windows.
- Unit tests for D8 with every spec scenario, run on all three platforms by CI, and a test that creates each produced name on disk.
- Crash and resume tests with the fault-injection hook during the downloads; an idempotence test (second run: no file request, no file rewritten).

### D13: Cross-platform

- Temp file, replace and directory fsync are the existing helpers, including the sharing-violation retry on Windows.
- Path length: with a 16-character id and at most 100 characters of name, `attachments/<id>/<name>` adds at most 130 characters to the target path. Windows without long-path support still fails for target paths longer than about 130 characters; the storage error then names the path. The README already advises a short target path.
- File systems that store names in NFD (HFS+) do not matter for the check of D4, which does not compare names.

## Risks / Trade-offs

- [The first run on an account with many attachments transfers gigabytes and can take hours] → Each file is stored as soon as it is complete, so a cancelled or failed run loses at most one file; the README advises the first run.
- [A permanently refused file warns on every run, which shows up in a cron mail every night] → Accepted with the user (Variant 1). The warning names the comment, so the user can find and delete the comment or the attachment.
- [`file_url` expires and frozen copies hold stale URLs] → The probes saw no sign of it, but could not check a years-old URL, as the read account has no uploaded file. A stale URL would show up as a refused-file warning; D2 names the fallback that fetches the comment fresh.
- [A request that reaches the file host without access is redirected to the login page on `app.todoist.com`, which answers 200 with HTML] → Seen in the probes without the token, which the tool always sends to `files.todoist.com`. Should it happen, the length check of D7 rejects the page, since every upload the probes saw has a `file_size`; the warning then reports a size mismatch rather than the refusal.
- [A comment deleted in Todoist before its file is stored] → Its file answers 403. The sync's tombstone has `file_attachment: null`, so a copy in `sync/` causes no request, but a frozen copy in `completed/` or `archived/` keeps the attachment and warns on every run, as accepted above.
- [Todoist moves files to a host outside `todoist.com`] → The files are skipped with a warning naming the host, so the change becomes visible; widening the host list is a one-line change with its own review.
- [An unknown upload type is downloaded that the user does not consider an attachment] → Only types on a Todoist host are fetched, so at worst a Todoist-hosted preview is stored once.
- [Disk use grows by the size of all files] → Inherent to the goal. Files are stored once per comment, never per copy.
- [A file changes on the server under the same comment] → Not detected: a stored file is never downloaded again. Todoist offers no way to replace a comment's file without a new comment, so this is accepted.

## Migration Plan

None beyond the first run: it downloads the files of every comment already in the backup. `state.json` gains nothing. To roll back, use an older version of the tool; it ignores `attachments/`, which can be deleted.

## Open Questions

None. The probes answered the API questions of the proposal (API findings), which settled D2 (the stored `file_url`), D3 (the link type `url`) and the redirect detail of D5 (no hop after the first needs the token).
