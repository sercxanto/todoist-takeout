# Design

## Context

The repository has no code yet, only planning documents (see `proposal.md` - Why). The dev container provides Python 3.12. The specs in this change define the behaviour; this document covers how to build it.

Facts about the Todoist API v1 that shape the design (from the public API reference, October 2026):

- `POST https://api.todoist.com/api/v1/sync`, form-encoded, with `sync_token` (`*` for a full sync) and `resource_types` (a JSON array, `["all"]` for everything). The whole account comes back in one response. There is no pagination.
- The response carries `sync_token`, `full_sync` and `full_sync_date_utc`, plus one top-level key per resource. Most resources are arrays of objects with an `id`. Some are single objects (`user`, `user_settings`, `user_plan_limits`, `stats`, ...). Some are arrays whose objects are identified by other fields (`view_options` by `view_type` + `object_id`, `workspace_users` by `workspace_id` + `user_id`, `project_view_options_defaults` by `project_id`). The docs contradict themselves on some key names, for example `collaborator_states` vs `collaborators_states`.
- The docs recommend an incremental sync right after a full sync, because large accounts may get a full sync whose `full_sync_date_utc` lags behind.
- Errors have the shape `{error, error_code, error_tag, http_code, error_extra}`. `error_extra.retry_after` may be present on 429 responses and on other errors.
- `user_plan_limits` describes what the plan includes, for example `comments: false`.
- The docs do not say whether deleted objects arrive as full objects or as stubs.

## Goals / Non-Goals

**Goals:**

- A tool that installs as a single package with no runtime dependencies, on Python 3.11+, on Windows, macOS and Linux.
- A storage layer that later changes (completed tasks, SQLite export) reuse unchanged: atomic writes, JSON encoding, file naming, lock, and state file.
- A test suite that runs fully offline, with fault injection for crash and resume.

**Non-Goals:**

- Archived projects and the tasks inside them. The sync endpoint does not return them. They are covered by `add-completed-tasks-backup`.
- Reconciling deletions that the sync never reports. A server-initiated full sync only warns about objects it no longer contains (see the specs).
- Parallel requests, a daemon or watch mode, and configuration files.

## Decisions

### D1: Python 3.11, stdlib only at runtime

`requires-python = ">=3.11"`. 3.10 reaches end of life in October 2026. 3.11 is widely available on current distributions. The production code uses only the standard library (`urllib.request`, `json`, `gzip`, `argparse`, `logging`, `fcntl`/`msvcrt`). The build backend (setuptools) is a build-time dependency only. Test-only dependencies: `pytest`.

*Alternative:* `requests` or `httpx`, for nicer retries and timeouts. Rejected because the tool makes very few requests, and `urllib` covers HTTPS, proxies (`HTTPS_PROXY`), and gzip with a small wrapper.

### D2: Package layout

```
pyproject.toml                    # console script todoist-takeout = todoist_takeout.cli:main
src/todoist_takeout/
  __init__.py                     # __version__
  __main__.py                     # python -m todoist_takeout
  cli.py                          # argparse, logging setup, exit-code mapping
  errors.py                       # UsageError/ConfigError (exit 2), RunError subclasses (exit 1)
  api.py                          # HTTP transport, retries, rate limits, error classification
  resources.py                    # table of known resource kinds and key rules
  sync.py                         # run orchestration: pending response, full/incremental, apply
  storage.py                      # JSON encoding, atomic write, file names, state file
  lock.py                         # cross-platform exclusive lock
  storage_format.md               # format description; copied into backups as README.md
tests/
  fixtures/sync/                  # recorded or hand-written sync responses
  fixtures/shapes.json            # expected response shapes for the contract test
  contract/                       # opt-in tests against the real API
scripts/record_fixtures.py        # manual: records and scrubs fixtures from a real account
```

`storage_format.md` lives inside the package so that it can be shipped and written into every backup. It is the only copy. The project README links to it.

### D3: CLI and exit codes

`todoist-takeout [--version] backup <target> [--quiet|--verbose] [--max-rate-limit-wait SECONDS]`, built with argparse. argparse already exits with status 2 on usage errors, which matches the spec. `cli.main()` maps exceptions to exit codes:

| Exception | Exit |
|---|---|
| `UsageError`, `ConfigError` (missing token, invalid target, unsupported format version) | 2 |
| `RunError` (API, storage, lock held, rate limit exhausted), any unexpected exception | 1 |

Unexpected exceptions print a one-line error. The traceback appears only with `--verbose`. Warnings never change the exit code. A logging handler counts the warnings for the summary line. The log format is `LEVEL message` on stderr. The token is never formatted into a message: the `Authorization` header is set in one place, and request logs show only method and path.

### D4: HTTP transport and retries

`api.py` defines a small `Transport` protocol, `send(method, path, form) -> (status, headers, body_bytes)`. The real implementation uses `urllib.request` with:

- `Accept-Encoding: gzip`, decoded with `gzip.decompress`;
- a `User-Agent: todoist-takeout/<version>` header;
- a 300 s socket timeout, so that slow full syncs of large accounts don't time out;
- the default SSL context, with certificate verification always on.

On top of the transport, a `Client` handles retries:

- **429:** wait `error_extra.retry_after`, else `Retry-After`, else 60 s, plus a small jitter. The total wait per run is capped at `--max-rate-limit-wait` (default 900 s, matching Todoist's 15-minute windows). Exceeding the cap raises `RateLimitExhausted`.
- **Network errors, timeouts, 5xx:** up to 5 attempts, with 1, 2, 4 and 8 s backoff plus jitter between them.
- **401, or 403 with an auth error tag:** raise `AuthError` immediately. The message names `TODOIST_API_TOKEN`.
- **Plan restriction:** a 403 whose `error_tag` is in a small set of known plan tags is classified as `PlanRestricted`. The set starts from the documented tags and is extended when the contract test finds others. Callers of optional requests catch `PlanRestricted` and turn it into a warning. The sync request itself is mandatory and has no such fallback.
- **Other 4xx:** raise `ApiError` with `error`, `error_tag` and `error_code`.

The clock and `sleep` are injected so that tests never actually wait.

*Alternative:* honour only `Retry-After`. Rejected because Todoist puts the value in the body.

### D5: Responses are bytes first, JSON second

The sync response body is saved byte-for-byte to `pending/sync-response.json` before it is parsed. Parsing uses plain `json.loads`, which keeps key order and exact integers, and no model classes are involved. Objects are sliced out of the parsed response and serialized individually. The re-serialized form keeps every field and value. Only whitespace and number spelling can differ from the API's text. Duplicate keys inside one object, which the API does not produce, would collapse to the last value; this limitation is documented.

### D6: Resource table and storage keys

`resources.py` maps each known top-level key to a kind:

| Kind | Examples | Stored as |
|---|---|---|
| `collection(id)` | `items`, `projects`, `sections`, `labels`, `notes`, `project_notes`, `filters`, `reminders`, `reminders_location`, `collaborators`, `live_notifications`, `workspace_filters` | `sync/<key>/<id>.json` |
| `collection(fields…)` | `view_options` (`view_type`, `object_id`), `workspace_users` (`workspace_id`, `user_id`), `project_view_options_defaults` (`project_id`) | `sync/<key>/<f1>~<f2>.json` |
| `mapping` | resources returned as a JSON object of objects (e.g. `workspaces` if the recorded shape confirms it) | `sync/<key>/<map-key>.json` |
| `single` | `user`, `user_settings`, `user_plan_limits`, `stats`, `notification_settings`/`settings_notifications`, `day_orders`, `live_notifications_last_read_id` | `sync/<key>.json` |
| `meta` | `sync_token`, `full_sync`, `full_sync_date_utc`, `temp_id_mapping`, `sync_status` | not stored as objects; the first three feed `state.json` |

The real kind of each resource is pinned by recorded fixtures and checked by the contract test. Both spellings of the inconsistently documented keys are listed.

Unknown keys are handled by shape:

- an array whose elements all have `id` is treated as `collection(id)`;
- anything else is stored as `single`.

Both cases log a warning naming the key, as the spec requires. A `single` that is really a partial collection would lose entries in incremental syncs; the warning and the contract test exist to catch that.

Arrays that are empty or missing in an incremental response mean "no change".

### D7: File names

Each key component is encoded separately, and the encoded components are joined with `~`. The encoding keeps `[A-Za-z0-9_-]` and percent-encodes every other UTF-8 byte as `%XX` with uppercase hex. A component that matches a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM0-9`, `LPT0-9`, compared case-insensitively) gets its first character encoded, so `CON` becomes `%43ON`. The encoding is reversible. Names never contain `.`, so `<key>.json` and `<key>.deleted.json` cannot be confused. An encoded name longer than 200 characters is a `StorageError`.

**Case-insensitive file systems** (default on Windows and macOS): current Todoist IDs are mixed-case, so `abc` and `ABC` could share a file. Collisions are astronomically unlikely, but silent overwrite is unacceptable. Whenever the tool is about to replace an existing file, it reads it anyway (D9). At that point it recomputes the stored object's key from its content and compares it with the new key; a mismatch raises `StorageError` and the file is left alone. The check costs nothing extra and works the same on every file system.

*Alternative:* encode upper-case letters, or use hashed file names. Rejected because they make the layout unreadable for a case that practically never occurs.

### D8: Applying a response, tombstones, server-initiated full sync

For each object, `apply` computes the target path and the encoded bytes, then:

1. **Live object** (`is_deleted` is not true): write `<key>.json` if its bytes differ from the stored ones.
2. **Deletion payload:**
   - no stored `<key>.json` → write the payload as `<key>.json`;
   - the stored live object's top-level keys ⊆ the payload's keys → overwrite `<key>.json`;
   - otherwise → keep `<key>.json` and write the payload to `<key>.deleted.json`.
   Re-applying the same payload gives the same result, so the step is idempotent.
3. **Full sync:** first collect the set of stored live keys per collection, then count those missing from the response and log one warning with the total. Nothing is changed for missing objects.

`single` resources are overwritten when their bytes change.

### D9: Atomic writes and durability

`storage.write_bytes_atomic(path, data)`:

1. If the file exists and its bytes equal `data`, return. This also makes the case check in D7 free.
2. Write `data` to `<dir>/.<name>.<random>.tmp` in the same directory, opened in binary mode so that Windows does not translate newlines; then `flush` and `os.fsync`.
3. `os.replace(tmp, path)`. On Windows, `PermissionError` from sharing violations (virus scanners, indexers, OneDrive) is retried up to 10 times with 50 ms to 500 ms backoff before failing.
4. On POSIX, fsync the parent directory (best effort). On Windows, NTFS metadata journaling covers the rename.

Durability ordering: object files are fsynced individually, and `state.json` is written last. On macOS, plain `fsync` does not flush the drive cache, so `state.json` is additionally synced with `fcntl.F_FULLFSYNC`. That flushes the whole drive cache, including the object files written before it, so a power loss can never leave a checkpoint that points past data that never reached the disk.

At startup, after taking the lock, the tool removes `.*.tmp` files under the target directory.

JSON encoding: `json.dumps(value, ensure_ascii=False, indent=2) + "\n"`, encoded as UTF-8.

### D10: Run sequence and resume

```
acquire lock (target/.lock)
validate target; read or create state.json (format_version=1, no token)
remove leftover temp files
if pending/sync-response.json exists: apply it, write state (token from response), delete pending
loop (at most 2 rounds: full + follow-up incremental, or 1 incremental):
    POST /sync with state token or "*"
    save body to pending/sync-response.json (atomic)
    parse, apply, write README.md if changed, write state.json, delete pending
check plan limits from user_plan_limits → warnings
write state.json (last_successful_run) ; print summary ; release lock
```

The follow-up incremental sync runs only after a full sync, whether the tool requested it or the server initiated it. Applying a stale pending response again is harmless, because applying is idempotent and that response's token matches or follows the stored one.

### D11: Lock

`target/.lock` is opened (created if missing) and locked without blocking, with `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX and `msvcrt.locking(fd, LK_NBLCK, 1)` on Windows. The OS releases the lock when the process dies, so a crashed run needs no cleanup. The lock file is never deleted, which avoids an unlink race. If the lock is held, the tool raises `LockHeld` (exit 1).

*Alternative:* a PID file with `O_EXCL`. Rejected because detecting stale locks is unreliable across platforms and containers.

### D12: Plan limitations

After the sync, the tool reads the stored `user_plan_limits.current` and compares it with a small table that maps plan flags to parts of this backup, for example `comments` → task and project comments. Each disabled feature produces one warning. If plan information is missing, no warning is produced. Later changes add rows (completed tasks) and use `PlanRestricted` for their optional endpoints.

### D13: State file

`state.json` contains:

```json
{
  "format_version": 1,
  "sync_token": "…",
  "last_full_sync_utc": "2026-10-18T10:00:00Z",
  "last_successful_run_utc": "2026-10-18T10:00:05Z",
  "tool_version": "0.1.0"
}
```

Unknown fields that a newer minor tool version adds are kept when the file is rewritten. A `format_version` greater than the one the tool supports raises `ConfigError` before anything is written.

### D14: Tests

- **Offline unit tests** for file-name encoding, JSON encoding bytes, atomic write, collision check, tombstone rules, resource classification, retry and rate-limit logic (fake clock), and CLI exit codes.
- **Offline integration tests** run full runs against a fixture transport that replays recorded responses. They also exercise the real `urllib` transport against a local `http.server` thread, to test gzip and headers without network access.
- **Crash tests:** a fault-injection hook in `storage` raises after N writes. For several values of N, the test checks that resuming produces exactly the tree of an uninterrupted run and does not make the request again.
- **Case-insensitivity test:** runs where the temporary directory is case-insensitive (macOS and Windows CI) and is skipped elsewhere, detected with a probe file.
- **Contract tests** (`pytest -m contract`): skipped unless `TODOIST_CONTRACT_TOKEN` is set.
  - Read-only part: a full sync, with the shape of every top-level key compared to `tests/fixtures/shapes.json`. The test lists fields and keys that were added or removed.
  - Write part, which additionally requires `TODOIST_CONTRACT_ALLOW_WRITES=1` and a dedicated test account: creates a temporary project, task and comment, deletes them, syncs incrementally, and records the shape of the deletion payloads. It then archives a second temporary project that contains a task, syncs incrementally, and records what the sync reports for the project and its task. This answers the "archiving between runs" question of `add-completed-tasks-backup`. Finally it cleans up.
- **Fixtures:** first hand-written from the documented examples, then replaced by recordings made with `scripts/record_fixtures.py`. That script replaces all strings except IDs, keys and enum-like values with placeholders, so that no personal data is committed.
- **CI:** a GitHub Actions matrix over ubuntu, macos and windows × Python 3.11 and 3.13. It runs once a remote exists, and the repository has none yet.

## Risks / Trade-offs

- [Deletion payloads may be stubs] → D8 keeps the last full content either way. The contract write test documents the real behaviour.
- [A resource's real shape differs from the table, e.g. `workspaces`] → Classification falls back by shape with a warning. The contract test compares shapes, and fixtures pin them.
- [A server-initiated full sync hides deletions] → The tool warns with a count. No data is lost; objects may stay live in the backup after they were deleted.
- [Thousands of fsyncs on the first full sync are slow, especially on Windows] → This affects only the first run. Unchanged files are skipped on later full syncs. Measure during implementation; if it proves too slow, fsyncing in batches is an option that keeps the guarantee.
- [macOS python.org builds without CA certificates] → Document the `Install Certificates.command` step and `SSL_CERT_FILE` in the README. Never disable verification.
- [`flock` on network file systems may not lock] → Document that the target directory should be on a local disk, or that runs must not overlap.
- [A long target path on Windows hits the 260-character limit] → File names stay short (D7). A failing write reports the full path. Document long-path support.
- [Plan-restriction error tags are undocumented] → Unknown 403s fail loudly instead of being silently skipped. Tags are added as they are observed.

## Migration Plan

None. This is the first release and format version 1. To roll back, delete the target directory or restore it from the file-based backup.

## Open Questions

- The exact error tags Todoist uses for plan restrictions. They only extend the D4 table.
- Whether `full_sync_date_utc` can lag so far that one follow-up incremental sync is not enough. If so, the round limit in D10 can be raised without other changes.
