# Changelog

All notable changes to todoist-takeout are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and todoist-takeout follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-24

### Added

- `todoist-takeout backup` keeps a complete local backup of a Todoist account: projects, sections, labels, tasks and comments, every completed task since the account was created, every completion of a recurring task, archived projects, and the files uploaded to comments.
- The backup is the API's own JSON, one file per object, with a format description that every backup carries along.
- After the first run, only what changed is downloaded, and an interrupted run continues where it stopped.
- `todoist-takeout sqlite` and `todoist-takeout backup-and-sqlite` build a SQLite database of the backup for Datasette, with full-text search and views of the open tasks, the completed tasks and the regularity of recurring tasks.
- Features that the Todoist plan does not include produce a warning and never make a run fail.
- Runs on Windows, macOS and Linux with Python 3.11 or newer, without further dependencies.

[Unreleased]: https://github.com/sercxanto/todoist-takeout/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sercxanto/todoist-takeout/releases/tag/v0.1.0
