# Proposal

> Status: placeholder. The scope is agreed, but several questions below depend on facts about the API. Answer them, preferably with the contract test from `add-active-data-sync`, before writing the specs and design.

## Why

Completed tasks are the part of the history the user cares about most, and the incremental sync of active data does not include them. Without them the backup is incomplete and not good enough for a migration. Accounts can hold tens of thousands of completed tasks, so they have to be fetched efficiently and in a way that survives interruptions.

## What Changes

- The tool backs up all completed tasks through the endpoint `/api/v1/tasks/completed/by_completion_date`, including the full history from the start of the account.
- Completed tasks are stored as one JSON file per month, at `completed/<YYYY-MM>.json`, with month boundaries in UTC. Each file holds the raw API payloads unchanged.
- The current month is written too, so the backup is complete after every run and could be used for a migration at any time.
- Proposed but not yet confirmed: a single forward marker in `state.json`, called `completed_until`, covers both the initial backfill and later incremental runs. A run walks month by month from the marker to now, fetches each month with a small overlap, merges the results into the month file without duplicates, writes the file atomically, and only then advances the marker.
- Months that have passed are never rewritten. Changes made later to completed or archived tasks are deliberately not captured.
- On plans without access to completed tasks, the tool warns and skips this part instead of failing.

## Capabilities

### New Capabilities

- `completed-tasks-backup`: fetching completed tasks incrementally, splitting them into month files, removing duplicates, and handling plan limitations.

### Modified Capabilities

- `backup-storage`: adds the `completed/` layout and the `completed_until` checkpoint. This capability is introduced by `add-active-data-sync` and must be archived before this change modifies it.

## Impact

- Depends on `add-active-data-sync`, for the CLI, API access, storage and checkpoint handling.
- The first run on large accounts takes several hundred requests and must stay within the rate limits.

## Open Questions

- **Deduplication key:** Recurring tasks may be completed several times under the same task ID. Is there a separate ID for each completion, or is the key task ID plus `completed_at`?
- **Comments and attachments of completed tasks:** Does the completed-tasks endpoint include them, for example through a parameter like the old `annotate_notes`? If it does not, fetching comments per task means tens of thousands of requests, and this becomes a separate design problem. The source code of the official SDK may answer this.
- **Overlap with the sync:** Tasks completed while the tool is running may show up both in `sync/items/` and in `completed/`. Do the two endpoints return the same object format? Where are the two sources reconciled?
- **Free plans:** Does the endpoint reject the request, return a limited history, or return nothing? The tool must handle every case without failing.
- **Forward marker:** Confirm the `completed_until` approach, and the account start date (for example `joined_at`) as the starting point of the backfill.
