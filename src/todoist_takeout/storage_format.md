# todoist-takeout backup format

Format version: 1

This directory is a backup of a [Todoist](https://www.todoist.com) account, written by
todoist-takeout. Every file is plain JSON or
Markdown, so you can read the backup without the tool. This file describes the format and is
rewritten by every run.

## Directory layout

| Path | Content |
|---|---|
| `README.md` | This description. |
| `state.json` | The checkpoint and run metadata. See [State file](#state-file). |
| `sync/` | The account's active data, as returned by the Todoist sync API. |
| `pending/` | A downloaded response that has not been applied yet. Normally empty. See [Pending response](#pending-response). |
| `.lock` | Lock file that keeps two runs from working on the same backup. See [Lock file](#lock-file). |

## JSON encoding

Every `.json` file the tool writes is encoded the same way on every operating system:

- UTF-8 without a byte order mark;
- indented with two spaces;
- non-ASCII characters written as they are, not as `\u` escapes. The only exception is a lone
  UTF-16 surrogate, which has no UTF-8 form and is written as a `\uXXXX` escape;
- LF line endings and a final newline;
- object keys in the order the API returned them.

Each object file holds exactly the JSON value the API returned for that object, with every field
and nothing added. Only whitespace and the spelling of numbers can differ from the API's text.

## File names

An object's file name is built from its key: usually its `id`, sometimes several fields (the
`sync/` section names them). Each key component is encoded on its own:

- the characters `A`-`Z`, `a`-`z`, `0`-`9`, `_` and `-` are kept;
- every other character is written as its UTF-8 bytes, each as `%XX` with uppercase hex digits.
  For example, `Größe` becomes `Gr%C3%B6%C3%9Fe` and `a.b` becomes `a%2Eb`;
- a component that equals a reserved Windows device name (`CON`, `PRN`, `AUX`, `NUL`,
  `COM0`-`COM9`, `LPT0`-`LPT9`, in any letter case) has its first character encoded, so `CON`
  becomes `%43ON` and `nul` becomes `%6Eul`.

Encoded components are joined with `~`, in the order the `sync/` section documents. To decode a
name, drop the `.json` or `.deleted.json` suffix, split the rest at `~`, and percent-decode each
part as UTF-8.

Encoded names contain no `.`, so the suffix is always unambiguous. They are at most 200
characters long; the tool refuses to store a key whose encoded name would be longer.

On a case-insensitive file system (the default on Windows and macOS), two keys that differ only in
letter case would share a file. The tool checks the key stored in a file before it replaces it, and
fails instead of overwriting one object with another.

## State file

`state.json` is the checkpoint. The tool writes it after all other files of a run, so it never
points past data that is not on disk.

```json
{
  "format_version": 1,
  "sync_token": "…",
  "last_full_sync_utc": "2026-10-18T10:00:00Z",
  "last_successful_run_utc": "2026-10-18T10:00:05Z",
  "tool_version": "0.1.0"
}
```

| Field | Meaning |
|---|---|
| `format_version` | The version of this format. A tool that supports only older versions refuses to touch the backup. |
| `sync_token` | The sync token of the last applied response, or `null` before the first one is applied. |
| `last_full_sync_utc` | The `full_sync_date_utc` of the last full sync, or `null`. |
| `last_successful_run_utc` | When the last run completed, or `null`. |
| `tool_version` | The version of todoist-takeout that last wrote the file. |

Times are UTC in ISO 8601 format. The first run creates `state.json` without a sync token before
it writes anything else, which marks the directory as a todoist-takeout backup. Fields that a later
version of the tool adds are kept when an older version rewrites the file.

## Pending response

A sync response is saved to `pending/sync-response.json` as soon as it has been downloaded,
exactly as received (after transfer decoding, otherwise unchanged), and before anything is taken
from it. It is deleted once all its objects are written and `state.json` holds its sync token. If a
run is interrupted in between, the next run applies the saved response instead of downloading it
again.

## Lock file

A run holds an exclusive operating-system lock on `.lock` while it works, so that a second run on
the same backup stops instead of interfering. The lock ends with the process, even after a crash.
The file itself is empty and is never deleted. Locks may not work on network file systems; keep the
backup on a local disk, or make sure runs do not overlap.

## Temporary files

Every file is replaced atomically: the new content is written to a temporary file named
`.<file name>.<random>.tmp` in the same directory, flushed to disk, and renamed over the old file.
Each file therefore holds either its complete old or its complete new content, even after a crash
or power loss. A crash can leave temporary files behind; the next run removes them. They never hold
data that is not also somewhere else.
