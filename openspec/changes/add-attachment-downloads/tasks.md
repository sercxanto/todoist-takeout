# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation sessions.

- Start each session fresh with `/opsx:apply add-attachment-downloads`, name the groups it covers, and stop at the end of the last one.
- Commit after each completed group, once its tests pass.
- Session 1: group 1, medium effort. Tasks 1.2 and 1.3 need the user with a real token: ask before each, never ask for a token in chat, use the Pro account (`TODOIST_CONTRACT_TOKEN`) for R8 only, which reads, and the Free test account (`TODOIST_CONTRACT_WRITE_TOKEN`) for W9, which writes. If a result contradicts a spec of this change, stop and settle it with the user before task 1.4.
- Session 2: groups 2 and 3, high effort. The host rule and the redirect handling decide where the token goes.
- Session 3: groups 4 and 5, high effort.
- Session 4: groups 6 and 7, medium effort. Tasks 7.2 and 7.3 need the user with a real token, with the same account rules as session 1.

## 1. Probes and the open API questions

- [ ] 1.1 Write the probes R8 (read-only) and W9 (writes, Free account only, behind the existing write guard) of design D11 in `tests/contract/probes.py` and `tests/contract/test_probe_attachments.py`, including the upload through `POST /api/v1/uploads` and the cleanup. Verify offline, with a test against the fake transport, that the probe's findings hold only booleans, types, statuses and host names, and no ID, URL path, query string, file name or file content
- [ ] 1.2 With the user, run R8 against the Pro account. Verify that the findings file stays untracked (`git status` shows nothing under `tests/contract/findings/`) and that no downloaded file is left outside the temporary directory
- [ ] 1.3 With the user, run W9 against the Free account. Verify that the findings file stays untracked and that the probe removed every project, task and comment it created
- [ ] 1.4 Record the results of 1.2 and 1.3 under "API findings" in `proposal.md`, answer its Open Questions, and settle design D2 (stored or fresh `file_url`), D3 (the set of link types) and the redirect detail of D5. Verify that `proposal.md` holds no live ID, URL or content, and that design.md no longer calls these points open

## 2. Storage primitives

- [ ] 2.1 Add `attachment_file_name` to `storage.py` with the steps of design D8. Verify with unit tests for every scenario of the spec requirements "Attachment file names" and "Attachment file name length", plus `CONIN$`, `COM¹`, `con .txt`, a name of only dots, and an extension longer than 16 characters, and a test that creates every produced name in a temporary directory
- [ ] 2.2 Add `write_stream_atomic` to `storage.py` with the size check and the fault-injection point of design D7. Verify with unit tests: chunks arrive in order, a size mismatch leaves no file and no temp file, `expected_size=None` skips the check, an exception from the chunk iterator leaves no file, and an injected fault leaves only a temp file that `remove_temp_files` removes

## 3. File transport and client

- [ ] 3.1 Add the host check of design D5 to `api.py`. Verify with unit tests: `todoist.com`, `files.todoist.com` and `FILES.Todoist.COM` are allowed; `http://files.todoist.com`, `todoist.com.example.net`, `evil-todoist.com`, `user@files.todoist.com` and port 8443 are rejected
- [ ] 3.2 Add the streaming `download` method to `UrllibTransport` with the manual redirect loop of D5 (at most 5 hops, https only, no token after the first request) and the `Accept-Encoding: identity` and gzip handling of D6. Verify with tests against a local HTTPS test server or a stubbed opener for every scenario of the spec requirement "Token only to Todoist hosts", including that no logged line holds the token or a query string
- [ ] 3.3 Extract the retry loop of `Client.request` and add `Client.download` with the status table of design D6. Verify that every existing test of `tests/test_api.py` passes unchanged, and with new tests for 429 on a file request (wait counted toward the cap), 5xx retried, a network error during the body retried from the start, and 401, 403, 404 and 410 raising `AttachmentRefused` and not the invalid-token error

## 4. Download step

- [ ] 4.1 Extend `tests/fake_api.py` with the fake file transport of design D12, and move the `file_url` of the hand-written fixtures to `files.todoist.com` with invented paths. Add fixtures for a link attachment, an incomplete upload and a foreign host. Verify that the existing offline suite passes unchanged apart from the URLs
- [ ] 4.2 Create `attachments.py` with `run(target, client, now)` (collect, filter, skip, download; design D1 to D4, with D2 and D3 as settled in 1.4) and call it from `run_backup` after `archived.run`. Verify with run tests for every scenario of the spec requirements "Uploaded files of stored comments are downloaded", "Only uploaded files", "One file per comment" and "Stored files are not downloaded again", and that every produced path matches the spec requirement "Attachments layout"
- [ ] 4.3 Handle failures as in design D6 and D7: refused files, size mismatch and unexpected host as warnings, persistent failures ending the run. Verify with run tests for every scenario of the spec requirements "Complete files only", "Refused files produce a warning", "Persistent failures end the run" and "Attachment on an unexpected host", including the exit status through the CLI
- [ ] 4.4 Add the messages of design D9. Verify with a run test that reads the log of a run that downloads two files, skips one stored file and gets one 404

## 5. Format description and README

- [ ] 5.1 Add the section "Attachment files" to `storage_format.md` (design D10) and change the comment sections of `sync/`, `completed/` and `archived/`, which say that files are not downloaded. Verify that the path-pattern tests of `tests/test_storage_format.py`, extended to `attachments/`, find every produced path documented and every documented pattern produced, and that the description covers the scenario "File of a comment" of the modified requirement "Documented format"
- [ ] 5.2 Update the project `README.md`: the status line, the backup scope with attachments, roadmap item 4 as implemented, and advice for the first run on an account with many attachments (time, disk space, rerunning). Verify that every link in the changed sections resolves

## 6. Whole runs

- [ ] 6.1 Add crash and resume tests: with the fault-injection hook at several counts during a first run with three attachments, one of them in a comment stored twice, a rerun produces the tree of an uninterrupted run byte for byte and requests no file that is already stored
- [ ] 6.2 Add an idempotence test: two runs with unchanged fake responses make no file request in the second run and leave every file under `attachments/` with the same bytes and modification time
- [ ] 6.3 Extend `tests/fixtures/expected_tree/` with `attachments/`, including a file whose name needs cleaning. Verify that the end-to-end CLI test of `tests/test_cross_platform.py` matches the tree byte for byte, and that it runs in the CI matrix on Linux, macOS and Windows

## 7. Contract tests and probe removal

- [ ] 7.1 Extend `scripts/record_fixtures.py` and `shapes.json` with the shape of a `file_attachment` of an uploaded file, scrubbed so that `file_name`, `file_url` and every ID are pseudonymized. Verify with an offline scrubber test that no recorded value holds a real host path, query string or file name
- [ ] 7.2 Add a read-only contract test that downloads one attachment of the read account into a temporary directory through `Client.download` and checks the byte count against `file_size`, and run it with the user against the Pro account. Verify that it passes, skips cleanly on an account without attachments, and leaves nothing behind
- [ ] 7.3 Add a write contract test, Free account only and behind the existing guard: upload a small file to a comment of a new task, run the tool into a temporary backup, and assert the file under `attachments/` byte for byte; then complete the task, archive its project, run again, and assert that no file request is made. Run it with the user. Verify that it passes and cleans up what it created
- [ ] 7.4 Remove the probes (`tests/contract/probes.py`, `tests/contract/test_probe_attachments.py` and their offline test) now that their findings are in the proposal. Verify that the offline suite and `pytest --collect-only tests/contract` pass, and that no remaining file imports `probes`

## Workflow follow-up

- Archive the change once the review is done, and check that the main specs gain `attachment-downloads`, the modified and added requirements of `backup-storage` and `api-access`, the modified comment requirements of `active-data-sync` and `completed-tasks-backup`, and the replaced attachment requirement of `archived-projects-backup`.
