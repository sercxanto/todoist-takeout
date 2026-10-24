import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from fake_api import FakeApi

from todoist_takeout import backup_and_sqlite, sqlite_view, storage
from todoist_takeout.api import Client, TransportError
from todoist_takeout.cli import main
from todoist_takeout.errors import ConfigError, LockHeld, NetworkError, StorageError
from todoist_takeout.lock import LOCK_FILE, TargetLock

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
TOKEN = "0123456789abcdef0123456789abcdef01234567"


def fixture_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def unchanged(token):
    return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()


# A first run: the full sync, then a follow-up incremental sync; a second run finds nothing new.
ACCOUNT = {"*": fixture_bytes("full_sync"), "FULL-1": fixture_bytes("incremental_edit"), "INC-1": unchanged("INC-1")}
# The same, but the second run finds a deleted project.
CHANGED = {**ACCOUNT, "INC-1": fixture_bytes("delete_stub"), "INC-3": unchanged("INC-3")}
# A first run whose follow-up reports a plan without comments, which gives one warning.
NO_COMMENTS = {"*": fixture_bytes("full_sync"), "FULL-1": fixture_bytes("plan_limits_no_comments")}


def client(api: FakeApi) -> Client:
    return Client(api, sleep=lambda delay: None)


def unreachable(token):
    raise TransportError("cannot reach api.todoist.com: [Errno -3] Temporary failure in name resolution")


class Cli:
    """Runs `main` with a fake API and no waiting, and records what the client got."""

    def __init__(self, transport):
        self.transport = transport
        self.max_rate_limit_wait = None

    def factory(self, token, max_rate_limit_wait):
        assert token == TOKEN
        self.max_rate_limit_wait = max_rate_limit_wait
        return Client(self.transport, max_rate_limit_wait=max_rate_limit_wait, sleep=lambda delay: None)

    def __call__(self, *argv):
        return main(["backup-and-sqlite", *argv], client_factory=self.factory)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)


def stderr_lines(capsys):
    return capsys.readouterr().err.splitlines()


def snapshot(root: Path) -> dict[str, bytes | None]:
    # Lock files are listed but not read: on Windows a held lock also blocks reading, and they hold no data.
    return {
        p.relative_to(root).as_posix(): None if p.name == LOCK_FILE else p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def dump(database: Path) -> dict[str, list]:
    with closing(sqlite3.connect(database)) as con:
        return {t.name: con.execute(f"SELECT * FROM {t.name} ORDER BY 1").fetchall() for t in sqlite_view.TABLES}


def assert_token_absent(err, directory):
    assert TOKEN not in err
    for path in directory.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


def first_run(directory: Path, account=ACCOUNT, database: Path | None = None) -> Path:
    backup_and_sqlite.run(directory, client(FakeApi(account)), database)
    return directory


# --- 1.1 directory validation -------------------------------------------------------------


def test_new_directory_is_created_with_its_parents(tmp_path):
    directory = tmp_path / "a" / "b" / "todoist"
    api = FakeApi(ACCOUNT)
    outcome = backup_and_sqlite.run(directory, client(api))
    assert api.requests == ["*", "FULL-1"]
    assert (directory / "json" / "state.json").is_file()
    assert outcome.built.rows["tasks"] == len(dump(directory / "todoist.db")["tasks"]) > 0
    assert outcome.stats.written > 0


def test_empty_directory(tmp_path):
    directory = tmp_path / "todoist"
    directory.mkdir()
    backup_and_sqlite.run(directory, client(FakeApi(ACCOUNT)))
    assert (directory / "json" / "state.json").is_file()
    assert (directory / "todoist.db").is_file()


def refused(directory: Path, database: Path | None = None) -> ConfigError:
    api = FakeApi(ACCOUNT)
    with pytest.raises(ConfigError) as exc:
        backup_and_sqlite.run(directory, client(api), database)
    assert api.requests == [] and api.get_requests == []
    assert exc.value.exit_code == 2
    return exc.value


def test_foreign_non_empty_directory(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"mine\n")
    error = refused(tmp_path)
    assert "not empty" in str(error) and "json" in str(error)
    assert snapshot(tmp_path) == {"notes.txt": b"mine\n"}


def test_backup_directory_given(tmp_path):
    storage.write_state(tmp_path, storage.new_state())
    before = snapshot(tmp_path)
    error = refused(tmp_path)
    assert "is a backup directory" in str(error) and "subdirectory json" in str(error)
    assert snapshot(tmp_path) == before


def test_path_is_a_file(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"")
    assert "not a directory" in str(refused(path))
    assert path.read_bytes() == b""


@pytest.mark.parametrize("kind", ["foreign", "file"])
def test_foreign_json_subdirectory(tmp_path, kind):
    if kind == "foreign":
        (tmp_path / "json").mkdir()
        (tmp_path / "json" / "notes.txt").write_bytes(b"mine\n")
    else:
        (tmp_path / "json").write_bytes(b"[]")
    before = snapshot(tmp_path)
    refused(tmp_path)
    assert snapshot(tmp_path) == before


# --- 1.1 checks before the backup ---------------------------------------------------------


@pytest.mark.parametrize("content", [b"not a database", b"SQLite format 3\x00" + bytes(100)])
def test_foreign_database_file(tmp_path, content):
    directory = first_run(tmp_path / "todoist")
    (directory / "todoist.db").write_bytes(content)
    before = snapshot(directory)
    assert str(directory / "todoist.db") in str(refused(directory))
    assert snapshot(directory) == before


def test_foreign_database_file_given_with_the_option(tmp_path):
    database = tmp_path / "mine.db"
    database.write_bytes(b"not a database")
    directory = tmp_path / "todoist"
    assert str(database) in str(refused(directory, database))
    assert not directory.exists()
    assert database.read_bytes() == b"not a database"


def test_foreign_metadata_file(tmp_path):
    directory = first_run(tmp_path / "todoist")
    (directory / "metadata.json").write_bytes(b"{}\n")
    before = snapshot(directory)
    assert str(directory / "metadata.json") in str(refused(directory))
    assert snapshot(directory) == before


def test_foreign_metadata_file_of_a_new_database(tmp_path):
    (tmp_path / "mine.metadata.json").write_bytes(b"{}\n")
    directory = tmp_path / "todoist"
    assert str(tmp_path / "mine.metadata.json") in str(refused(directory, tmp_path / "mine.db"))
    assert not directory.exists() and not (tmp_path / "mine.db").exists()


def test_backup_from_a_newer_version(tmp_path):
    directory = first_run(tmp_path / "todoist")
    (directory / "json" / "state.json").write_bytes(storage.encode_json({"format_version": 99, "sync_token": "X"}))
    before = snapshot(directory)
    assert "format version 99" in str(refused(directory))
    assert snapshot(directory) == before


# --- 1.1 backup, then database ------------------------------------------------------------


def test_backup_fails(tmp_path):
    directory = first_run(tmp_path / "todoist")
    database = snapshot(directory)
    api = FakeApi(failure=unreachable)
    with pytest.raises(NetworkError) as exc:
        backup_and_sqlite.run(directory, client(api))
    assert exc.value.exit_code == 1
    assert api.requests == ["INC-1"] * 5
    after = snapshot(directory)
    for name in ["todoist.db", "metadata.json"]:
        assert after[name] == database[name]


def test_backup_fails_before_a_first_database(tmp_path):
    directory = tmp_path / "todoist"
    with pytest.raises(NetworkError):
        backup_and_sqlite.run(directory, client(FakeApi(failure=unreachable)))
    assert sorted(p.name for p in directory.iterdir()) == [LOCK_FILE, "json"]


def test_rebuild_fails_after_a_complete_backup(tmp_path, monkeypatch):
    directory = first_run(tmp_path / "todoist")
    database = directory / "todoist.db"
    before = dump(database)
    metadata = (directory / "metadata.json").read_bytes()
    monkeypatch.setattr(sqlite_view, "BUSY_TIMEOUT", 0.2)
    completed = []
    api = FakeApi(CHANGED)
    with closing(sqlite3.connect(database, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageError) as exc:
            backup_and_sqlite.run(directory, client(api), backup_complete=completed.append)
        holder.execute("ROLLBACK")
    assert exc.value.exit_code == 1 and str(database) in str(exc.value)
    assert api.requests == ["INC-1"]
    assert [stats.deleted for stats in completed] == [1]
    assert json.loads((directory / "json" / "state.json").read_bytes())["sync_token"] == "INC-3"
    assert dump(database) == before
    assert (directory / "metadata.json").read_bytes() == metadata


def test_nothing_changed_rebuilds_the_database(tmp_path):
    directory = first_run(tmp_path / "todoist")
    database = directory / "todoist.db"
    before = dump(database)
    with closing(sqlite3.connect(database)) as con, con:
        con.execute("DELETE FROM tasks")
    api = FakeApi(ACCOUNT)
    outcome = backup_and_sqlite.run(directory, client(api))
    assert api.requests == ["INC-1"]
    assert outcome.stats.written == 0
    assert dump(database) == before


# --- 1.2 directory layout and the database option -----------------------------------------


def test_paths_after_a_run(tmp_path):
    directory = first_run(tmp_path / "todoist")
    assert sorted(p.name for p in directory.iterdir()) == [LOCK_FILE, "json", "metadata.json", "todoist.db"]
    assert (directory / "json" / "state.json").is_file()
    assert not [p for p in (directory / "json").rglob("*") if p.name.startswith("todoist.")]
    metadata = json.loads((directory / "metadata.json").read_bytes())
    assert metadata["source"] == "todoist-takeout"


def test_backup_used_by_the_other_commands(tmp_path):
    directory = first_run(tmp_path / "todoist")
    cli = Cli(FakeApi(ACCOUNT))
    assert main(["backup", str(directory / "json")], client_factory=cli.factory) == 0
    assert cli.transport.requests == ["INC-1"]
    assert main(["sqlite", str(directory / "json"), str(directory / "todoist.db")]) == 0


def test_database_elsewhere(tmp_path):
    database = tmp_path / "elsewhere" / "todoist.db"
    directory = first_run(tmp_path / "todoist", database=database)
    assert sorted(p.name for p in directory.iterdir()) == [LOCK_FILE, "json"]
    assert sorted(p.name for p in database.parent.iterdir()) == ["todoist.db", "todoist.metadata.json"]
    assert dump(database)["tasks"]


def test_database_inside_the_backup_directory_on_request(tmp_path):
    directory = tmp_path / "todoist"
    database = directory / "json" / "todoist.db"
    first_run(directory, database=database)
    assert (directory / "json" / "todoist.metadata.json").is_file()
    before = dump(database)
    api = FakeApi(CHANGED)
    backup_and_sqlite.run(directory, client(api), database)
    assert api.requests == ["INC-1"]
    assert len(dump(database)["projects"]) == len(before["projects"]) - 1
    assert not (directory / "todoist.db").exists()


def test_opened_in_datasette_as_a_directory(tmp_path):
    from datasette.app import Datasette

    directory = first_run(tmp_path / "todoist")

    async def browse():
        # As `datasette <dir>` does it: the directory as the configuration directory.
        ds = Datasette([], config_dir=directory)
        await ds.invoke_startup()
        databases = (await ds.client.get("/-/databases.json")).json()
        metadata = (await ds.client.get("/-/metadata.json")).json()
        tasks = (await ds.client.get("/todoist/tasks.json?_shape=array")).json()
        return databases, metadata, tasks

    databases, metadata, tasks = asyncio.run(browse())
    assert [(db["name"], db["is_mutable"]) for db in databases] == [("todoist", True)]
    assert metadata == json.loads((directory / "metadata.json").read_bytes())
    assert len(tasks) == len(dump(directory / "todoist.db")["tasks"]) > 0


# --- 1.3 single run per directory ---------------------------------------------------------


def test_concurrent_run(tmp_path, capsys):
    directory = first_run(tmp_path / "todoist")
    capsys.readouterr()
    second = Cli(FakeApi(ACCOUNT))

    def start_second_run(stats):
        before = snapshot(directory)
        assert second(str(directory)) == 1
        assert stderr_lines(capsys)[-1].startswith(f"ERROR another run is in progress on {directory}")
        assert snapshot(directory) == before

    api = FakeApi(CHANGED)
    backup_and_sqlite.run(directory, client(api), backup_complete=start_second_run)
    assert second.transport.requests == []
    assert api.requests == ["INC-1"]
    assert json.loads((directory / "json" / "state.json").read_bytes())["sync_token"] == "INC-3"


def test_held_lock_of_the_directory(tmp_path):
    directory = first_run(tmp_path / "todoist")
    before = snapshot(directory)
    api = FakeApi(CHANGED)
    with TargetLock(directory), pytest.raises(LockHeld) as exc:
        backup_and_sqlite.run(directory, client(api))
    assert exc.value.exit_code == 1
    assert api.requests == []
    assert snapshot(directory) == before


def test_backup_run_in_progress(tmp_path, capsys):
    directory = first_run(tmp_path / "todoist")
    before = snapshot(directory)
    cli = Cli(FakeApi(CHANGED))
    with TargetLock(directory / "json"):
        assert cli(str(directory)) == 1
    assert stderr_lines(capsys)[-1].startswith("ERROR another run is in progress on")
    assert cli.transport.requests == []
    assert snapshot(directory) == before


@pytest.mark.parametrize("killed", ["before json", "during the backup"])
def test_lock_after_a_crash(tmp_path, killed):
    directory = tmp_path / "todoist"
    directory.mkdir()
    (directory / LOCK_FILE).write_bytes(b"")
    if killed == "during the backup":
        storage.write_state(directory / "json", storage.new_state())
        (directory / "json" / LOCK_FILE).write_bytes(b"")
    api = FakeApi(ACCOUNT)
    backup_and_sqlite.run(directory, client(api))
    assert api.requests == ["*", "FULL-1"]
    assert dump(directory / "todoist.db")["tasks"]


# --- 2.1 the command ----------------------------------------------------------------------


def test_run_both_steps(tmp_path, capsys):
    directory = tmp_path / "todoist"
    cli = Cli(FakeApi(ACCOUNT))
    assert cli(str(directory)) == 0
    assert (directory / "json" / "state.json").is_file()
    assert dump(directory / "todoist.db")["tasks"]
    assert (directory / "metadata.json").is_file()
    assert_token_absent(capsys.readouterr().err, directory)


def test_module_invocation(tmp_path):
    run = lambda *argv: subprocess.run(
        [sys.executable, "-m", "todoist_takeout", "backup-and-sqlite", *argv],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    result = run("--help")
    assert result.returncode == 0
    for text in ["TARGET", "--database", "--quiet", "--verbose", "--max-rate-limit-wait", "TODOIST_API_TOKEN"]:
        assert text in result.stdout
    result = run()
    assert result.returncode == 2
    assert "usage: todoist-takeout backup-and-sqlite" in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [[], ["a", "b"], ["a", "--quiet", "--verbose"], ["a", "--max-rate-limit-wait", "-1"], ["a", "--database"]],
)
def test_usage_errors(tmp_path, capsys, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    cli = Cli(FakeApi(ACCOUNT))
    with pytest.raises(SystemExit) as exc:
        cli(*argv)
    assert exc.value.code == 2
    assert "usage: todoist-takeout" in capsys.readouterr().err
    assert cli.transport.requests == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", [None, ""])
def test_missing_token(tmp_path, capsys, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TODOIST_API_TOKEN")
    else:
        monkeypatch.setenv("TODOIST_API_TOKEN", value)
    directory = tmp_path / "todoist"
    cli = Cli(FakeApi(ACCOUNT))
    assert cli(str(directory)) == 2
    lines = stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("ERROR ") and "TODOIST_API_TOKEN" in lines[0]
    assert cli.transport.requests == []
    assert not directory.exists()


def test_configuration_error_exits_with_status_2(tmp_path, capsys):
    storage.write_state(tmp_path, storage.new_state())
    assert Cli(FakeApi(ACCOUNT))(str(tmp_path)) == 2
    lines = stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("ERROR ") and "is a backup directory" in lines[0]


def test_rate_limit_wait_reaches_the_client(tmp_path):
    cli = Cli(FakeApi(ACCOUNT))
    assert cli(str(tmp_path / "todoist"), "--max-rate-limit-wait", "3600") == 0
    assert cli.max_rate_limit_wait == 3600.0
    assert cli(str(tmp_path / "todoist")) == 0
    assert cli.max_rate_limit_wait == 900.0


@pytest.mark.parametrize("verbosity", [[], ["--verbose"], ["--quiet"]])
def test_token_never_appears_in_output(tmp_path, capsys, verbosity):
    directory = tmp_path / "todoist"
    assert Cli(FakeApi(NO_COMMENTS))(str(directory), *verbosity) == 0
    assert_token_absent(capsys.readouterr().err, directory)

    def broken(token):
        raise RuntimeError(f"something went wrong with Bearer {TOKEN}")

    assert Cli(FakeApi(failure=broken))(str(directory), *verbosity) == 1
    err = capsys.readouterr().err
    assert "[redacted]" in err
    assert_token_absent(err, directory)


def test_successful_run_with_a_warning(tmp_path, capsys):
    directory = tmp_path / "todoist"
    assert Cli(FakeApi(NO_COMMENTS))(str(directory)) == 0
    lines = stderr_lines(capsys)
    assert all(line.split(" ", 1)[0] in {"INFO", "WARNING"} for line in lines), lines
    assert len([line for line in lines if line.startswith("WARNING ")]) == 1
    summary = lines.index("INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning")
    assert lines.index(f"INFO reading the backup in {directory / 'json'}") == summary + 1
    assert lines[-2] == f"INFO view it with: datasette {directory}"
    assert lines[-1].startswith("INFO database complete: ") and lines[-1].endswith(", 1 warning")


def test_view_hint_of_a_directory_with_spaces(tmp_path, capsys):
    directory = tmp_path / "my data" / "todoist"
    assert Cli(FakeApi(ACCOUNT))(str(directory)) == 0
    assert stderr_lines(capsys)[-2] == f'INFO view it with: datasette "{directory}"'


def test_view_hint_names_the_database_option(tmp_path, capsys):
    database = tmp_path / "my data" / "todoist.db"
    assert Cli(FakeApi(ACCOUNT))(str(tmp_path / "todoist"), "--database", str(database)) == 0
    hint = f'INFO view it with: datasette "{database}" -m "{database.parent / "todoist.metadata.json"}"'
    assert stderr_lines(capsys)[-2] == hint


def test_quiet_run(tmp_path, capsys):
    assert Cli(FakeApi(ACCOUNT))(str(tmp_path / "todoist"), "--quiet") == 0
    assert capsys.readouterr().err == ""


def test_failed_rebuild_explains_itself_last(tmp_path, capsys, monkeypatch):
    directory = first_run(tmp_path / "todoist")
    database = directory / "todoist.db"
    monkeypatch.setattr(sqlite_view, "BUSY_TIMEOUT", 0.2)
    with closing(sqlite3.connect(database, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        assert Cli(FakeApi(CHANGED))(str(directory)) == 1
        holder.execute("ROLLBACK")
    lines = stderr_lines(capsys)
    assert "INFO backup complete: 0 objects written, 0 unchanged, 1 marked deleted, 0 warnings" in lines
    assert lines[-1].startswith("ERROR ") and str(database) in lines[-1]


# --- 2.2 interruption ---------------------------------------------------------------------


def test_interrupted_rebuild(tmp_path, capsys, monkeypatch):
    directory = first_run(tmp_path / "todoist")
    database = directory / "todoist.db"
    before = dump(database)

    def interrupted(backup):
        raise KeyboardInterrupt

    monkeypatch.setattr(sqlite_view, "read_backup", interrupted)
    assert Cli(FakeApi(CHANGED))(str(directory)) == 130
    assert stderr_lines(capsys)[-1] == (
        "ERROR interrupted; the next run resumes from the last checkpoint, and the database keeps its previous content"
    )
    assert dump(database) == before
    # Both locks were released.
    with TargetLock(directory), TargetLock(directory / "json"):
        pass
