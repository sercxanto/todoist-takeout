# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation sessions.

- Start each session fresh with `/opsx:apply add-active-data-sync`, name the groups it covers, and stop at the end of the last one.
- Commit after each completed group, once its tests pass.
- Session 1: groups 1 and 2, high effort. Atomic writes, file locking and file-name encoding fail only on Windows or macOS, or after a crash, so the dev container would not catch mistakes there.
- Session 2: groups 3 and 4, medium effort.
- Session 3: task 4.7, then group 5, high effort. Pending response, checkpoint order, crash and resume tests.
- Session 4: group 6, medium effort.
- Session 5: groups 7 and 8, medium effort. Tasks 7.1, 7.3 and 7.4 need the user with a real Todoist token, and 7.4 with a dedicated test account. Ask the user before starting them.
- Alternative for sessions 1 and 3: medium effort, followed by `/code-review high` over that session's diff.

## 1. Project scaffolding

- [x] 1.1 Add `pyproject.toml` (setuptools backend, `requires-python = ">=3.11"`, no runtime dependencies, `test` extra with `pytest`, console script `todoist-takeout = todoist_takeout.cli:main`, package data `storage_format.md`) and a `.gitignore` for Python artifacts. Verify that `pip install -e '.[test]'` succeeds in the dev container
- [x] 1.2 Create the `src/todoist_takeout/` package with `__init__.py` (`__version__ = "0.1.0"`), `__main__.py`, a stub `cli.py` that handles `--version` and `--help` only, and `errors.py` with the `UsageError`/`ConfigError`/`RunError` hierarchy from design D3. Verify that `todoist-takeout --version` and `python -m todoist_takeout --version` both print `0.1.0` and exit 0
- [x] 1.3 Add the `tests/` layout, a `pytest` configuration in `pyproject.toml` that registers the `contract` marker and deselects it by default, and one smoke test for `--version`. Verify that `pytest` passes and reports the contract marker as deselected

## 2. Storage primitives

- [x] 2.1 Implement the JSON encoding in `storage.py` (D9: `ensure_ascii=False`, indent 2, LF, final newline, UTF-8, binary write). Verify with unit tests that the bytes are identical for a fixture containing non-ASCII text and emoji, that there is no BOM, and that there are no CRLF line endings
- [x] 2.2 Implement the reversible file-name encoding for key components and composite keys (D7: allowed set, `%XX`, `~` join, Windows device names, 200-character limit). Verify with unit tests covering plain IDs, `CON`/`nul`/`COM1`, non-ASCII, `~` and `.` inside components, round-trip decoding, and the length limit
- [x] 2.3 Implement `write_bytes_atomic` (skip when the bytes are equal, temp file in the same directory, fsync, `os.replace`, Windows `PermissionError` retry, POSIX directory fsync, optional `F_FULLFSYNC`) and the startup cleanup of `.*.tmp` files. Verify with unit tests that an unchanged file keeps its mtime, that a simulated failure before the replace leaves the old content intact, that a patched `os.replace` that fails twice with `PermissionError` still succeeds, and that leftover temp files are removed
- [x] 2.4 Add a fault-injection hook to `storage` that raises after N writes, for the crash tests in task 5.4. Verify with a unit test that the hook fires at the configured count and is inactive by default
- [x] 2.5 Implement `lock.py` (`flock` on POSIX, `msvcrt.locking` on Windows, the lock file is never deleted, `LockHeld` when the lock is taken). Verify with a test that a second lock attempt from a subprocess fails while the first lock is held, and succeeds after the holder is killed
- [x] 2.6 Implement the state file (D13: create without a token, read, keep unknown fields, reject a newer `format_version` with `ConfigError`, write last via `write_bytes_atomic` with full sync). Verify with unit tests for create, round-trip with an extra unknown field, and rejection of `format_version: 2`
- [x] 2.7 Write `src/todoist_takeout/storage_format.md` (format version 1) covering the encoding, file-name rules, `state.json`, `pending/`, `.lock` and temp files. Verify with a test that the format version stated in the file matches the code constant

## 3. API client

- [x] 3.1 Implement the `Transport` protocol and the `urllib` transport in `api.py` (POST form encoding, bearer header, `Accept-Encoding: gzip`, User-Agent, 300 s timeout, default SSL context). Verify with an offline test against a local `http.server` thread that the transport sends the expected headers and form body, decodes a gzip response, and that no log record contains the token
- [x] 3.2 Implement the retrying `Client` with an injected clock and sleep (D4): 429 with `error_extra.retry_after`, then `Retry-After`, then the default; a per-run total wait cap; 5 attempts with backoff for network errors, timeouts and 5xx; no retry for other 4xx. Verify with unit tests using a scripted fake transport for each path, including cap exhaustion raising `RateLimitExhausted`
- [x] 3.3 Implement the error classification: `AuthError` for 401 and auth 403 (message names `TODOIST_API_TOKEN`), `PlanRestricted` for known plan tags, and `ApiError` carrying `error`/`error_tag`/`error_code`. Verify with unit tests that each class is raised for a matching recorded error body and that the messages contain the API's fields but not the token
- [x] 3.4 Add the `sync(token)` call that returns the raw response bytes. Verify with a fake-transport test that it posts `sync_token` and `resource_types=["all"]` and returns the body unchanged

## 4. Resources and applying responses

- [x] 4.1 Add hand-written sync fixtures under `tests/fixtures/sync/`, based on the documented API examples: a full sync, an incremental sync with an edit, a full deletion payload, a stub deletion payload, an unknown resource key, `user_plan_limits` with `comments: false`, and a server-initiated full sync. Verify that every fixture parses as JSON in a fixture-loading test
- [x] 4.2 Implement `resources.py` with the kind table from D6, including both spellings of the inconsistently documented keys, and the shape-based fallback with warnings for unknown keys. Verify with unit tests that every key in the fixtures is classified as expected and that an unknown key logs exactly one warning
- [x] 4.3 Implement `apply` for live objects and `single` resources (path computation, encoding, skip when unchanged), including the case-collision check from D7. Verify with tests that applying the full-sync fixture produces the expected tree, that each stored object parses to exactly its source object, and that a colliding key raises `StorageError` without overwriting (forced via a pre-existing file whose content has a different key)
- [x] 4.4 Implement the tombstone rules from D8 (no stored file, superset payload, stub payload → `<key>.deleted.json`), with idempotent re-application. Verify with tests on the full and stub deletion fixtures that the resulting files match the spec scenarios and that applying the same payload twice changes nothing
- [x] 4.5 Implement the missing-object count for full syncs (one warning with the total, no changes). Verify with a test on the server-initiated full-sync fixture that 3 stored objects stay unchanged and the warning reports 3
- [x] 4.6 Extend `storage_format.md` with the `sync/` layout, resource kinds, composite keys and tombstones. Verify that the documented layout matches the tree produced in tests 4.3 and 4.4, in a test that checks every path pattern named in the document
- [x] 4.7 Implement restoring a deleted object (D8 step 1): a live payload removes an existing `<key>.deleted.json` after `<key>.json` is written, also when `<key>.json` is unchanged. Update `storage_format.md` and the `sync.py` docstring, which currently say that no file is ever removed. Verify with tests that applying the stub deletion fixture and then a live payload for the same project leaves the live payload in `<key>.json` and no `<key>.deleted.json`, that a tombstone next to an already up-to-date `<key>.json` (a run interrupted between the write and the removal) is removed when the response is applied again, and that applying the live payload a second time changes nothing

## 5. Sync run orchestration

- [x] 5.1 Implement the run sequence from D10 in `sync.py` (lock, target validation, state creation, temp cleanup, apply a leftover pending response first, full sync plus one follow-up incremental sync, incremental runs, pending file written before parsing and deleted after the checkpoint, state written last). Verify with fixture-transport tests that a first run makes exactly two requests (`*`, then the returned token), that a second run makes one, and that `pending/` is empty afterwards
- [x] 5.2 Write the format description into the backup as `README.md` (only when changed) before the checkpoint. Verify with a test that a completed backup contains `README.md` with the format version from `state.json`
- [x] 5.3 Implement the plan-limit warnings from D12. Verify with a test on the `comments: false` fixture that one warning names comments and the run still succeeds
- [x] 5.4 Add crash and resume tests: for several fault-injection counts during a first run, and after the pending file is written, rerun and verify that the resulting tree is byte-identical to an uninterrupted run and that a saved full response is not requested again
- [x] 5.5 Add idempotence tests: two runs with an unchanged incremental response leave all object files byte-identical with unchanged mtimes, and only `state.json` changes

## 6. Command-line interface

- [ ] 6.1 Implement `backup <target>` in `cli.py` with `--quiet`, `--verbose` and `--max-rate-limit-wait`, the `LEVEL message` stderr format, the warning counter, and the summary line (written, unchanged, deleted, warnings). Verify with CLI tests (fixture transport injected) that the stderr output matches the spec scenarios for the default and quiet modes
- [ ] 6.2 Implement reading `TODOIST_API_TOKEN` and validating the target (create with parents, accept an existing backup or a directory holding only `state.json`, reject foreign non-empty directories and files). Verify with CLI tests that each case gives the specified exit status and that no request is made when the exit status is 2
- [ ] 6.3 Implement the exception-to-exit-code mapping (D3), with tracebacks only under `--verbose`. Verify with CLI tests for exit 0 with a warning, 1 for persistent network failure, auth failure and a held lock, and 2 for usage and configuration errors, each asserting that the token appears neither on stderr nor in any file of the target
- [ ] 6.4 Add a test that runs the CLI with stdin closed and no terminal (subprocess with `stdin=DEVNULL`) against a local `http.server` fixture, and verify that it completes with exit status 0
- [ ] 6.5 Document installation, the token variable, the `backup` command, exit codes, scheduling examples (cron, launchd, Task Scheduler), and platform notes (macOS certificates, local disk for the target, Windows long paths) in the project `README.md`. Verify that the documented commands match `todoist-takeout --help`

## 7. Fixture recording and contract tests

- [ ] 7.1 Write `scripts/record_fixtures.py`, which records a full and an incremental sync with a real token and replaces every string except IDs, keys and enum-like values with placeholders. Verify with an offline unit test of the scrubber on a fixture containing names, emails and task content
- [ ] 7.2 Create `tests/fixtures/shapes.json` from the fixtures (top-level keys, kinds, per-object field names and JSON types), with a generator in the recorder script. Verify that a test regenerates shapes from the fixtures and finds no difference from the committed file
- [ ] 7.3 Implement the read-only contract test (`pytest -m contract`, skipped without `TODOIST_CONTRACT_TOKEN`). It runs a full sync, compares the response with `shapes.json`, and asserts that every top-level key is classified by `resources.py` without the fallback. Verify that it is skipped without the variable, and with a manual run against a test account if a token is available
- [ ] 7.4 Implement the write contract test (also requires `TODOIST_CONTRACT_ALLOW_WRITES=1`). It creates a temporary project, task and comment through sync commands, deletes them, syncs incrementally, asserts that the tool's tombstone handling stores the result as specified, and records the deletion payload shape. It then checks whether the API can restore a deleted object with the same id (for example through an undo or undelete command). If it can, the test restores the temporary task, syncs incrementally, asserts that the tombstone is removed as specified, and records the payload; otherwise it records that no restore path was found. It then archives a second temporary project with a task, syncs incrementally, and records what the sync returns for the project and the task (`is_archived`, `is_deleted`, or absent). It cleans up in a `finally` block. Verify that it is skipped without the variables
- [ ] 7.5 Document fixture recording and both contract-test modes in the README's Development section, including the recommendation to use a dedicated test account for writes. Verify that the documented commands run as written (the skipped path at minimum)

## 8. Cross-platform integration

- [ ] 8.1 Add a GitHub Actions workflow with a matrix of ubuntu, macos and windows × Python 3.11 and 3.13 that runs `pytest`. Verify that the workflow file passes `actionlint` (or a YAML lint) locally; the first real run happens once a remote exists
- [ ] 8.2 Add the case-insensitive file-system test (D14), which uses a probe to detect case sensitivity and is skipped on case-sensitive file systems, and an end-to-end CLI test that compares the produced tree with a committed expected tree byte-for-byte. Verify that both pass locally on Linux (with the case test skipped) and are part of the CI matrix
- [ ] 8.3 Update the README roadmap and status to reflect that active-data sync is implemented. Verify that the README links to `storage_format.md`

## Workflow follow-up

- If the contract tests show stub deletion payloads, plan-restriction error tags or resource shapes that differ from the fixtures, record the findings in this change's design before archiving.
- Record what the write contract test found about archiving a project in the open questions of `add-completed-tasks-backup`.
- Record what the write contract test found about restoring deleted objects in this change's design.
- Archive this change once it is reviewed, so that `add-completed-tasks-backup` can modify `backup-storage`.
