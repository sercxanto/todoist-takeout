# todoist-takeout

A command-line tool that keeps a complete, long-term readable local backup of a [Todoist](https://www.todoist.com) account, and lets you browse it in your web browser.

## Why

Todoist is a cloud service, so your data is only reachable through it. If the service shuts down, or you lose access to your account for any other reason, the data is gone. todoist-takeout keeps a local copy that:

- stays readable for years, without Todoist and without this tool,
- includes your history, meaning completed tasks and not only the ones still open,
- can serve as the source for migrating to another service.

## What you get

- **Everything, not just open tasks:** projects, sections, labels, tasks and comments, every completed task since the account was created, every completion of a recurring task, archived projects, and the files uploaded to comments.
- **Plain, documented files:** the API's own JSON, one file per object, with a [format description](https://github.com/sercxanto/todoist-takeout/blob/main/src/todoist_takeout/storage_format.md) that every backup carries along.
- **Fast daily runs:** after the first run, only what changed is downloaded. An interrupted run simply continues where it stopped.
- **Browse and search it:** a SQLite database for [Datasette](https://datasette.io), with filters, full-text search and ready-made views of your open tasks, everything you completed, and how regularly you do your recurring tasks.
- **Safe and simple:** no dependencies beyond Python, runs on Windows, macOS and Linux, and your API token goes to Todoist and nowhere else.

![The open tasks of a demo account in Datasette, with facets by project, priority, overdue and recurring](https://github.com/sercxanto/todoist-takeout/raw/main/docs/images/datasette.png)

## Quick start

todoist-takeout needs Python 3.11 or newer. It is not on PyPI yet, so install it straight from this repository with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/sercxanto/todoist-takeout
```

`pipx install git+https://github.com/sercxanto/todoist-takeout` works as well. From a checkout of the repository, `uv tool install .` installs that checkout.

Copy your API token from Todoist (*Settings → Integrations → Developer*) into the environment, back up your account, and open the result in Datasette:

```sh
export TODOIST_API_TOKEN=0123456789abcdef0123456789abcdef01234567
todoist-takeout backup-and-sqlite ~/Backups/todoist
uvx datasette ~/Backups/todoist
```

Then open the address that Datasette prints, usually `http://127.0.0.1:8001`. Run the same `backup-and-sqlite` command again at any time to bring the backup and the database up to date. The first run on a large account can take a while; see [Backing up](https://github.com/sercxanto/todoist-takeout/blob/main/docs/backup.md#the-first-run-on-a-large-account).

To keep only the JSON backup, use `todoist-takeout backup` instead.

## Run it daily

The tool never asks for input, so a scheduler can run it every night: [Scheduling](https://github.com/sercxanto/todoist-takeout/blob/main/docs/scheduling.md) has examples for cron, launchd and the Windows Task Scheduler.

## Documentation

- [Backing up](https://github.com/sercxanto/todoist-takeout/blob/main/docs/backup.md): the token, what the backup contains, large accounts, the Free plan, options, output and exit status.
- [Viewing the backup](https://github.com/sercxanto/todoist-takeout/blob/main/docs/viewing.md): `backup-and-sqlite` and `sqlite`, the directory layout, and the tables and views of the database.
- [Scheduling](https://github.com/sercxanto/todoist-takeout/blob/main/docs/scheduling.md): unattended daily runs.
- [Platform notes](https://github.com/sercxanto/todoist-takeout/blob/main/docs/platform-notes.md): macOS certificates, network drives, Windows long paths.
- [Format description](https://github.com/sercxanto/todoist-takeout/blob/main/src/todoist_takeout/storage_format.md): the layout and files of a backup, for reading it without this tool.

## Status

Not released yet. All parts of the backup listed above work, as do the database and Datasette views.

## Contributing

Setting up a development environment, running the checks and the tests, the requirements and the key design decisions are described in [CONTRIBUTING.md](https://github.com/sercxanto/todoist-takeout/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/sercxanto/todoist-takeout/blob/main/LICENSE)
