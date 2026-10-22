"""Command-line entry point: argument parsing, logging, and the exit-code mapping (design D3)."""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from todoist_takeout import __version__, backup_and_sqlite, sqlite_view
from todoist_takeout.api import DEFAULT_MAX_RATE_LIMIT_WAIT, Client, UrllibTransport
from todoist_takeout.errors import ConfigError, TakeoutError
from todoist_takeout.sync import ApplyStats, run_backup

PROG = "todoist-takeout"
TOKEN_VARIABLE = "TODOIST_API_TOKEN"
BACKUP = "backup"
SQLITE = "sqlite"
BACKUP_AND_SQLITE = "backup-and-sqlite"

# What an interrupted run leaves behind, by command.
INTERRUPTED = {
    BACKUP: "the next run resumes from the last checkpoint",
    SQLITE: "the database was left unchanged",
    BACKUP_AND_SQLITE: "the next run resumes from the last checkpoint, and the database keeps its previous content",
}

log = logging.getLogger(__name__)

# Builds the API client from the token and the --max-rate-limit-wait value. Tests replace it.
ClientFactory = Callable[[str, float], Client]


def default_client(token: str, max_rate_limit_wait: float) -> Client:
    return Client(UrllibTransport(token), max_rate_limit_wait=max_rate_limit_wait)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Keep a complete, long-term readable local backup of a Todoist account.",
        epilog=f"The personal API token is read from the environment variable {TOKEN_VARIABLE}.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    backup = commands.add_parser(
        BACKUP,
        help="back up the account into a target directory",
        description=(
            "Back up the Todoist account into TARGET. The first run makes a full sync; later runs "
            "sync incrementally. Messages go to stderr. Exit status: 0 when the backup is complete "
            "(even with warnings), 1 when the run failed, 2 for usage or configuration errors."
        ),
        epilog=f"The personal API token is read from the environment variable {TOKEN_VARIABLE}.",
    )
    backup.add_argument("target", metavar="TARGET", type=Path, help="the backup directory; created if missing")
    _add_verbosity(backup)
    _add_max_rate_limit_wait(backup)

    database = commands.add_parser(
        SQLITE,
        help="build a SQLite database from a backup, for browsing it in Datasette",
        description=(
            "Build a SQLite database at DATABASE from the backup in BACKUP, for browsing it, for example "
            "in Datasette. Works offline, needs no token, and never changes the backup. Messages go to "
            "stderr. Exit status: 0 when the database is complete (even with warnings), 1 when the build "
            "failed, 2 for usage or configuration errors."
        ),
    )
    database.add_argument("backup", metavar="BACKUP", type=Path, help="the backup directory")
    database.add_argument(
        "database", metavar="DATABASE", type=Path, help="the database file; created with its parents if missing"
    )
    _add_verbosity(database)

    combined = commands.add_parser(
        BACKUP_AND_SQLITE,
        help="back up the account and build the SQLite database from it, in one directory",
        description=(
            f"Back up the Todoist account into TARGET/{backup_and_sqlite.BACKUP_DIR}, then build the SQLite "
            f"database TARGET/{backup_and_sqlite.DATABASE_FILE} from it. The database is built only after a "
            "complete backup. Messages go to stderr. Exit status: 0 when the backup and the database are "
            "complete (even with warnings), 1 when the run failed, 2 for usage or configuration errors."
        ),
        epilog=f"The personal API token is read from the environment variable {TOKEN_VARIABLE}.",
    )
    combined.add_argument(
        "target",
        metavar="TARGET",
        type=Path,
        help=(
            f"the directory that holds the backup in {backup_and_sqlite.BACKUP_DIR}/ and the database; "
            "created if missing"
        ),
    )
    combined.add_argument(
        "--database",
        metavar="PATH",
        type=Path,
        help=(
            f"build the database at PATH instead of TARGET/{backup_and_sqlite.DATABASE_FILE}; "
            "its metadata file goes next to it"
        ),
    )
    _add_verbosity(combined)
    _add_max_rate_limit_wait(combined)
    return parser


def _add_verbosity(parser: argparse.ArgumentParser) -> None:
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="print only warnings and errors")
    verbosity.add_argument("-v", "--verbose", action="store_true", help="also print debug messages and tracebacks")


def _add_max_rate_limit_wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-rate-limit-wait",
        metavar="SECONDS",
        type=_seconds,
        default=DEFAULT_MAX_RATE_LIMIT_WAIT,
        help=(
            "give up when waiting for the API's rate limit would take longer than this in total "
            f"(default: {DEFAULT_MAX_RATE_LIMIT_WAIT:.0f})"
        ),
    )


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0 or value == float("inf"):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}")
    return value


def main(argv: list[str] | None = None, *, client_factory: ClientFactory = default_client) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    # The sqlite command works offline and does not read the token (design D1).
    token = os.environ.get(TOKEN_VARIABLE, "").strip() if command != SQLITE else ""
    level = logging.WARNING if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    with _Logging(level, token) as counter:
        try:
            if command == SQLITE:
                built = sqlite_view.run(args.backup, args.database)
                view = _datasette_arguments(args.database, sqlite_view.metadata_file(args.database))
            elif not token:
                raise ConfigError(
                    f"the environment variable {TOKEN_VARIABLE} is not set or empty; "
                    "set it to your Todoist personal API token"
                )
            elif command == BACKUP:
                stats = run_backup(args.target, client_factory(token, args.max_rate_limit_wait))
            else:
                built = backup_and_sqlite.run(
                    args.target,
                    client_factory(token, args.max_rate_limit_wait),
                    args.database,
                    backup_complete=lambda stats: _backup_summary(stats, counter.warnings),
                ).built
                # Without --database, Datasette finds the database and metadata.json in the directory (design D7).
                view = (
                    _datasette_arguments(args.target)
                    if args.database is None
                    else _datasette_arguments(
                        args.database, backup_and_sqlite.metadata_path(args.target, args.database)
                    )
                )
        except TakeoutError as exc:
            _report_failure(str(exc), args.verbose)
            return exc.exit_code
        except KeyboardInterrupt:
            _report_failure(f"interrupted; {INTERRUPTED[command]}", args.verbose)
            return 130
        except Exception as exc:
            hint = "" if args.verbose else "; rerun with --verbose for details"
            _report_failure(f"unexpected error: {type(exc).__name__}: {exc}{hint}", args.verbose)
            return 1
        if command == BACKUP:
            _backup_summary(stats, counter.warnings)
        else:
            _database_summary(view, built, counter.warnings)
    return 0


def _backup_summary(stats: ApplyStats, warnings: int) -> None:
    log.info(
        "backup complete: %d objects written, %d unchanged, %d marked deleted, %s",
        stats.written,
        stats.unchanged,
        stats.deleted,
        _plural(warnings, "warning"),
    )


def _database_summary(view: str, built: sqlite_view.Built, warnings: int) -> None:
    log.info("view it with: datasette %s", view)
    rows = [_plural(count, table.replace("_", " ")[:-1]) for table, count in built.rows.items()]
    log.info("database complete: %s, %s", ", ".join(rows), _plural(warnings, "warning"))


def _report_failure(message: str, verbose: bool) -> None:
    if verbose:
        log.debug("traceback of the failure:", exc_info=True)
    log.error("%s", message)  # always the last line on stderr


def _datasette_arguments(path: Path, metadata: Path | None = None) -> str:
    """The arguments that open `path`, a database or a directory, in Datasette, with `metadata` if given."""
    return _argument(path) if metadata is None else f"{_argument(path)} -m {_argument(metadata)}"


def _argument(path: Path) -> str:
    # Double quotes keep a path with spaces one argument in the shells of Windows, macOS and Linux.
    return f'"{path}"' if " " in str(path) else str(path)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class _Formatter(logging.Formatter):
    """Formats `LEVEL message` and removes the token from everything it formats, tracebacks included."""

    def __init__(self, token: str):
        super().__init__("%(levelname)s %(message)s")
        self._token = token

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.replace(self._token, "[redacted]") if self._token else text


class _WarningCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.warnings = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.WARNING:
            self.warnings += 1


class _Logging:
    """Sends the package's log records to stderr for the duration of a run, and counts warnings."""

    def __init__(self, level: int, token: str):
        self._logger = logging.getLogger("todoist_takeout")
        self._level = level
        self._stderr = logging.StreamHandler(sys.stderr)
        self._stderr.setFormatter(_Formatter(token))
        self._counter = _WarningCounter()

    def __enter__(self) -> _WarningCounter:
        self._previous_level = self._logger.level
        self._logger.setLevel(self._level)
        self._logger.addHandler(self._stderr)
        self._logger.addHandler(self._counter)
        return self._counter

    def __exit__(self, *exc_info) -> None:
        self._logger.removeHandler(self._stderr)
        self._logger.removeHandler(self._counter)
        self._logger.setLevel(self._previous_level)
