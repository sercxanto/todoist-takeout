# todoist-takeout

A command-line tool that keeps a complete, long-term readable local backup of a [Todoist](https://www.todoist.com) account.

> **Status:** planning. Nothing is usable yet; see the [Roadmap](#roadmap).

## Why

Todoist is a cloud service, so your data is only reachable through it. If the service shuts down, or you lose access to your account for any other reason, the data is gone. todoist-takeout keeps a local copy that:

- stays readable for years, without Todoist and without this tool,
- includes your history, meaning completed tasks and not only the ones still open,
- can serve as the source for migrating to another service.

## Requirements

These are the goals the tool is built against. As each one is implemented, it moves into the specs under [openspec/specs/](openspec/specs/).

### Platform and development

- Runs on Windows, Linux and macOS.
- Written in Python, without a Todoist SDK; the tool talks to the Todoist API v1 directly.
- The production code prefers the Python standard library and uses third-party libraries only where they clearly pay off.
- Good test coverage, in two directions:
  - offline tests against recorded fixtures or mocks, which never call the real API;
  - an opt-in contract test against the real API, which detects when Todoist changes its API.

### Behaviour

- A command-line interface, suitable for unattended batch runs.
- Running the tool again gives the same result. An interrupted run resumes where it stopped, and data that was already backed up is not downloaded again.
- After every successful run the backup is complete and up to date.
- Respects the API's rate limits.
- Works on free plans too. Features a plan does not include are skipped with a warning and never cause the tool to fail.

### Scope of the backup

- Active data and **all** completed tasks.
- Efficient even with tens of thousands of completed tasks.
- As much metadata as the API offers, not just task titles: projects, sections, labels, comments, attachments and so on.

### Storage

- A long-term stable format that people and machines can both read.
- A documented storage schema.

## Key decisions so far

These will be written up in more detail in the design documents of the corresponding changes.

- **Raw payloads are the source of truth.** API responses are stored unchanged as JSON, so fields the tool does not know about are preserved. A queryable SQLite database may be derived from them later.
- **The backup holds the current state, with tombstones.** Objects deleted in Todoist stay in the backup and are marked as deleted. Older versions are not kept, because a file-based backup of the backup directory provides history.
- **Layout:** one file per active object at `sync/<type>/<id>.json`, and one file per month for completed tasks at `completed/<YYYY-MM>.json`, with month boundaries in UTC. The current month is included.
- **Completed tasks are frozen.** Changes made later to completed or archived tasks are not captured.

## Roadmap

1. **Sync active data:** a full sync, then incremental syncs, with checkpointing and tombstones. See [openspec/changes/add-active-data-sync/](openspec/changes/add-active-data-sync/).
2. **Back up completed tasks:** month files, written incrementally. Several open questions remain. See [openspec/changes/add-completed-tasks-backup/](openspec/changes/add-completed-tasks-backup/).
3. **Comments and attachments of completed tasks.** How expensive this is depends on the API (see the open questions in step 2).
4. **Download attachment files.**
5. **Derive a SQLite database** from the raw JSON, for queries and migration.
6. **Download Todoist's own backups** (`/api/v1/backups`) as an additional safety net. This is an idea, not yet evaluated.

The documented storage schema is part of each step and grows with the backup format.

## Development

- The dev container ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json)) provides Python 3.12.
- Planning uses [OpenSpec](https://github.com/Fission-AI/OpenSpec). Run `openspec list` to see changes in progress.
