# Backing up

How the `backup` command works, what the backup contains, and what it prints. For installation and a quick start, see the [README](../README.md); for browsing the backup, see [Viewing the backup](viewing.md).

## Token

The tool authenticates with your personal API token, which you find in Todoist under *Settings → Integrations → Developer*. It reads the token only from the environment variable `TODOIST_API_TOKEN`; there is no command-line option for it, and the tool never writes it to its output or into the backup. The token is sent only over HTTPS to Todoist's own hosts; when the file host redirects the download of an attachment file to a storage host, the tool follows the redirect without the token.

```sh
export TODOIST_API_TOKEN=0123456789abcdef0123456789abcdef01234567
```

For unattended runs, keep the token in a file that only you can read; [Scheduling](scheduling.md) shows how.

## Running a backup

```sh
todoist-takeout backup ~/Backups/todoist
# the same:
python -m todoist_takeout backup ~/Backups/todoist
```

The target directory is created if it does not exist. The first run downloads all active data and every completed task since the account was created; later runs download only what changed since the previous run. An interrupted run is simply started again: it resumes from the last checkpoint and does not download data it already stored. The tool refuses to write into a directory that is not empty and is not a todoist-takeout backup. Only one run at a time can use a target directory.

To keep a database for browsing up to date as well, use [`backup-and-sqlite`](viewing.md#backup-and-database-in-one-directory) instead, which runs a backup and then rebuilds the database.

The backup contains a `README.md` that describes its format; the same description is in the [format description](../src/todoist_takeout/storage_format.md).

## What the backup contains

- **Active data** under `sync/`: one file per project, section, label, task, comment and every other object the sync API returns. Objects deleted in Todoist stay, marked as deleted.
- **Completed tasks** under `completed/`, one file per month (UTC). A month is fetched again on every run until 7 days after it ended, then it is frozen: later changes to its tasks, and completions backdated into it, are not captured.
- **One event per completion**, including every completion of a recurring task, which the completed tasks do not show. Completions from the first run on come from the activity log; earlier ones are fetched once, on the first runs.
- **Comments of completed tasks**, with the metadata of their attachments.
- **Archived projects** under `archived/`, one directory per project with its open and completed tasks, its sections, its archived sub-projects, and the comments of the project and of its tasks. A project is fetched once, when it first shows up as archived; later changes to its content are not captured, unless it is unarchived and archived again. A snapshot stays in the backup after the project is unarchived or deleted.
- **Attachment files** under `attachments/`: the file uploaded to each stored comment, in `attachments/<comment id>/` under its original name, cleaned up only as far as Windows, macOS and Linux need. Each file is downloaded once and checked against the size Todoist reports. Attachments that link to other websites are not downloaded. A file the server refuses, for example because its comment was deleted, produces a warning and is tried again on every run.

Not included: sections that were archived before the first backup, because the API cannot list them.

### The same task in several places

The same task can be stored more than once, for example in `sync/items/`, in a month file under `completed/` and in the snapshot of its archived project. All copies share the task's `id`. The current copy is the one with the newest `updated_at`; on equal values, the copy under `archived/` is current, then the one under `completed/`, then the one under `sync/`. The [format description](../src/todoist_takeout/storage_format.md#copies-of-the-same-task) lists every place.

### The first run on a large account

The first run makes a few requests per month since the account was created, and one per completed task that has comments. Each archived project adds one request for the project, one per three months since it was created, one per task with comments and one for the project's comments, so an account with 50 archived projects needs a few hundred more; later runs fetch only newly archived projects. The first run can therefore reach the API's rate limit, and stops with exit status 1 once waiting would exceed `--max-rate-limit-wait`. Simply run it again, as often as needed: each run continues where the previous one stopped, and does not fetch the archived projects it already stored. Or allow a longer wait for the first run, for example `--max-rate-limit-wait 3600`.

### Accounts with many attachments

The first run downloads every file uploaded to the stored comments, one at a time, after all other data; later runs download only new ones. On an account with years of attachments this can take hours and as much disk space as the files take in Todoist, so check the free space of the target first. A run that stops, because of the network or because you cancel it, keeps every file it completed; run it again to download the rest. Files already stored are never downloaded again.

### The Free plan

On the Free plan, the activity log keeps only 7 days. Run the backup at least weekly; otherwise the completions of recurring tasks in the gap may be lost, and the tool warns about it.

Features your Todoist plan does not include, such as comments on some plans, produce a warning and never make the run fail.

### Todoist's own backups

Todoist's own backups are not downloaded. On paid plans, Todoist keeps up to 21 daily backups under *Settings → Backups* in the web and desktop apps: ZIP files with one CSV file per active project. They hold nothing that this backup lacks: no completed tasks, no archived projects, at most 500 comments per task, and only links to attachments. They do not exist on the Free plan, and when two-factor authentication is on, the API hands them out only with a one-time code from the authenticator app, which rules out unattended runs. They offer one thing this tool does not: a project's CSV file can be imported into Todoist directly (*Import from CSV*), which brings back a deleted project with its active tasks. If you want that option, download them yourself in the app.

## Options

| Option | Meaning |
|---|---|
| `-q`, `--quiet` | Print only warnings and errors. Useful under a scheduler that mails any output. |
| `-v`, `--verbose` | Also print debug messages, and a traceback when the run fails. |
| `--max-rate-limit-wait SECONDS` | Give up when waiting for the API's rate limit would take longer than this in total (default 900). |

## Output

All messages go to stderr, one line each, starting with `INFO`, `WARNING` or `ERROR`. A successful run ends with a summary line such as:

```
INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning
```

The summary line counts the objects under `sync/`; separate `INFO` lines report the completed tasks, the archived projects and the attachment files.

## Exit status

| Status | Meaning |
|---|---|
| 0 | The backup is complete and up to date, possibly with warnings. |
| 1 | The run failed after it started: network or API error, invalid token, persistent rate limit, a storage error, or another run holds the target directory. The last line on stderr explains why. The backup stays consistent; the next run resumes. |
| 2 | Usage or configuration error, detected before any request: invalid arguments, missing token, a target that is a file or a foreign directory, or a backup written by a newer version of the tool. Nothing was changed. |

If a run fails with a certificate error or the target lives on a network drive, see the [platform notes](platform-notes.md).
