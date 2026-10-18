# Proposal

## Why

Todoist stores all data in the cloud, so the user can reach it only through the service. If the service shuts down or the account becomes unreachable, the data is gone. A local, long-term readable backup protects against this and can also serve as the source for a later migration to another service. Active data is the foundation: the projects, sections and labels it contains give every later part of the backup its context. That includes completed tasks, which come in a follow-up change.

## What Changes

- New command-line tool, written in Python and run unattended in batch mode, that backs up a Todoist account into a target directory.
- Authentication with a personal API token read from an environment variable.
- The first run does a full sync of all active data, including projects, sections, labels, tasks and the comments on active tasks. Later runs sync only incrementally, using the stored sync token as a checkpoint.
- Each object is stored as one JSON file holding the raw API payload unchanged, so fields the tool does not know about are kept.
- Objects that were deleted in Todoist stay in the backup and are marked as deleted (tombstones). Older versions are not kept, because file-based backups of the target directory provide history.
- Every write is atomic, and the checkpoint is written last. A run that is interrupted and started again produces the same result, and data that was already stored is not downloaded again.
- The tool respects the API's rate limits by honouring HTTP 429 and `retry_after`.
- Features not available on the user's plan produce a warning, not a failure.
- Tests run offline against recorded fixtures. An opt-in contract test runs against the real API with a token and detects changes in the shape of its responses.
- Out of scope: completed tasks (planned in `add-completed-tasks-backup`), attachment file downloads, and a derived SQLite database.

## Capabilities

### New Capabilities

- `backup-cli`: invoking the tool, its configuration (token, target directory), behaviour in batch mode, and exit status.
- `api-access`: authenticated requests to the Todoist API, rate-limit handling, and degrading gracefully when the plan does not include a feature.
- `active-data-sync`: full and incremental sync of active data, checkpointing, and tombstones for deleted objects.
- `backup-storage`: the on-disk layout, unchanged raw payloads, atomic writes, and a documented, long-term readable format.

### Modified Capabilities

None. The project has no specs yet.

## Impact

- New Python code base. It contains no Todoist SDK and prefers the standard library in production code; a third-party library is used only where it clearly pays off.
- Must run on Windows, Linux and macOS.
- Depends on the Todoist API v1 `/sync` endpoint.
- Creates a backup directory with `state.json` and one file per object at `sync/<type>/<id>.json`.

## Open Questions

- **Format of deleted objects:** Does the sync return the full object or only its ID when `is_deleted` is set? The contract test will show. The answer decides whether the last content of a deleted object is preserved.
- **Exit codes:** What should the exit status be for "succeeded with limitations", for example a feature that is not on the plan, as opposed to an error? This gets decided in the design.
- **Minimum Python version:** 3.10 or newer is proposed. This gets confirmed in the design.
