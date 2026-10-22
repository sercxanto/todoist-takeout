"""The backup-and-sqlite command: a backup, then the database rebuilt from it, in one directory (design D1).

The target directory holds the backup in `json/` and, unless another path is given, the database in
`todoist.db` with its metadata file `metadata.json`, which Datasette loads when it opens the directory
(design D7). Every check that needs no API request runs before the backup starts (design D2), and the
whole run holds the lock on the target directory (design D3).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from todoist_takeout import sqlite_view, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.errors import ConfigError
from todoist_takeout.lock import LOCK_FILE, TargetLock
from todoist_takeout.sync import ApplyStats

log = logging.getLogger(__name__)

BACKUP_DIR = "json"
DATABASE_FILE = "todoist.db"
# The name Datasette's configuration directory mode loads (design D7).
METADATA_FILE = "metadata.json"


@dataclass
class Outcome:
    stats: ApplyStats
    built: sqlite_view.Built


def database_path(directory: Path, database: Path | None = None) -> Path:
    """The database file of a run on `directory`: `database` if given, else `todoist.db` in it."""
    return Path(database) if database is not None else Path(directory) / DATABASE_FILE


def metadata_path(directory: Path, database: Path | None = None) -> Path:
    """The metadata file of a run on `directory`: `metadata.json` in it, or the one `sqlite` names for `database`."""
    return sqlite_view.metadata_file(Path(database)) if database is not None else Path(directory) / METADATA_FILE


def run(
    directory: Path,
    client: Client,
    database: Path | None = None,
    *,
    backup_complete: Callable[[ApplyStats], None] | None = None,
) -> Outcome:
    """Back up the account into `directory/json`, then rebuild the database from it.

    `backup_complete` is called with the backup's statistics before the rebuild starts, so that the
    messages of a failed rebuild show that the backup is complete (design D4).
    """
    directory = Path(directory)
    metadata = metadata_path(directory, database)
    database = database_path(directory, database)
    check_directory(directory)
    sqlite_view.check_database(database)
    sqlite_view.check_metadata(metadata)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot use the target directory {directory}: {exc.strerror or exc}") from exc
    with TargetLock(directory):
        backup = directory / BACKUP_DIR
        stats = sync.run_backup(backup, client)
        if backup_complete is not None:
            backup_complete(stats)
        built = sqlite_view.run(backup, database, metadata)
    return Outcome(stats, built)


def check_directory(directory: Path) -> None:
    """Raise ConfigError unless `directory` is missing, empty, or holds a backup in `json/`.

    Only reads.
    """
    if directory.exists() and not directory.is_dir():
        raise ConfigError(f"the target {directory} is not a directory")
    try:
        names = {p.name for p in directory.iterdir()} if directory.exists() else set()
    except OSError as exc:
        raise ConfigError(f"cannot use the target directory {directory}: {exc.strerror or exc}") from exc
    backup = directory / BACKUP_DIR
    if backup.is_dir():
        sync.check_target(backup)
    elif storage.STATE_FILE in names:
        raise ConfigError(
            f"the target directory {directory} is a backup directory; move its content into a subdirectory "
            f"{BACKUP_DIR} of it, or use the backup and sqlite commands"
        )
    elif names - {LOCK_FILE}:
        # A lock file alone is left by a run killed before it created json/.
        raise ConfigError(
            f"the target directory {directory} is not empty and has no subdirectory {BACKUP_DIR}; "
            "choose a new or empty directory"
        )
