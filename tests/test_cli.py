import json
import logging
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from todoist_takeout import __version__, storage
from todoist_takeout.api import Client, TransportError
from todoist_takeout.cli import main
from todoist_takeout.lock import TargetLock

from fake_api import FakeApi, empty_page

SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
TOKEN = "0123456789abcdef0123456789abcdef01234567"


def fixture_bytes(name):
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def unchanged(token):
    return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()


# A first run: the full sync, then a follow-up incremental sync with one edited task.
ACCOUNT = {"*": fixture_bytes("full_sync"), "FULL-1": fixture_bytes("incremental_edit"), "INC-1": unchanged("INC-1")}
# The same, but the follow-up reports a plan without comments, which gives one warning.
NO_COMMENTS = {"*": fixture_bytes("full_sync"), "FULL-1": fixture_bytes("plan_limits_no_comments")}


class Cli:
    """Runs `main` with a fixture transport and no waiting, and records what the client got."""

    def __init__(self, transport):
        self.transport = transport
        self.max_rate_limit_wait = None

    def factory(self, token, max_rate_limit_wait):
        assert token == TOKEN
        self.max_rate_limit_wait = max_rate_limit_wait
        return Client(self.transport, max_rate_limit_wait=max_rate_limit_wait, sleep=lambda delay: None)

    def __call__(self, *argv):
        return main(list(argv), client_factory=self.factory)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)


def stderr_lines(capsys):
    return capsys.readouterr().err.splitlines()


def assert_token_absent(err, target):
    assert TOKEN not in err
    if target.is_dir():
        for path in target.rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes(), path


def file_snapshot(target):
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}


# --- version and usage --------------------------------------------------------------


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"todoist-takeout {__version__}"


def test_module_invocation_version():
    result = subprocess.run(
        [sys.executable, "-m", "todoist_takeout", "--version"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"todoist-takeout {__version__}"


def test_help_names_the_backup_command_and_the_token_variable(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["backup", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for text in ["TARGET", "--quiet", "--verbose", "--max-rate-limit-wait", "TODOIST_API_TOKEN"]:
        assert text in out


@pytest.mark.parametrize(
    "argv",
    [[], ["backup"], ["backup", "a", "b"], ["backup", "a", "--quiet", "--verbose"], ["backup", "a", "--max-rate-limit-wait", "-1"]],
)
def test_usage_errors_exit_with_status_2(tmp_path, capsys, argv):
    cli = Cli(FakeApi(ACCOUNT))
    with pytest.raises(SystemExit) as exc:
        cli(*argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage: todoist-takeout" in err
    assert cli.transport.requests == []
    assert_token_absent(err, tmp_path)


# --- 6.1 backup command and diagnostic output ---------------------------------------------


def test_default_run_prints_info_lines_one_warning_and_the_summary(tmp_path, capsys):
    target = tmp_path / "backup"
    cli = Cli(FakeApi(NO_COMMENTS))
    assert cli("backup", str(target)) == 0
    lines = stderr_lines(capsys)
    assert all(line.split(" ", 1)[0] in {"INFO", "WARNING"} for line in lines), lines
    assert [line for line in lines if line.startswith("WARNING ")] == [
        "WARNING the Todoist plan 'free' does not include comments; task and project comments cannot be "
        "backed up on this plan"
    ]
    assert "INFO requesting a full sync of the account" in lines
    assert lines[-1] == "INFO backup complete: 31 objects written, 0 unchanged, 0 marked deleted, 1 warning"
    assert cli.transport.requests == ["*", "FULL-1"]


def test_quiet_run_prints_only_the_warning(tmp_path, capsys):
    cli = Cli(FakeApi(NO_COMMENTS))
    assert cli("backup", str(tmp_path), "--quiet") == 0
    assert stderr_lines(capsys) == [
        "WARNING the Todoist plan 'free' does not include comments; task and project comments cannot be "
        "backed up on this plan"
    ]


def test_summary_counts_unchanged_and_deleted_objects(tmp_path, capsys):
    cli = Cli(FakeApi({**ACCOUNT, "INC-1": fixture_bytes("delete_stub"), "INC-3": unchanged("INC-3")}))
    assert cli("backup", str(tmp_path)) == 0
    capsys.readouterr()
    assert cli("backup", str(tmp_path)) == 0
    assert stderr_lines(capsys)[-1] == "INFO backup complete: 0 objects written, 0 unchanged, 1 marked deleted, 0 warnings"


def test_max_rate_limit_wait_reaches_the_client(tmp_path):
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(tmp_path), "--max-rate-limit-wait", "30") == 0
    assert cli.max_rate_limit_wait == 30.0
    cli("backup", str(tmp_path))
    assert cli.max_rate_limit_wait == 900.0


def test_rate_limit_beyond_the_limit_fails(tmp_path, capsys):
    rate_limited = lambda token: (429, {}, b'{"error": "Too many requests", "error_extra": {"retry_after": 60}}')
    cli = Cli(FakeApi(failure=rate_limited))
    assert cli("backup", str(tmp_path), "--max-rate-limit-wait", "30") == 1
    lines = stderr_lines(capsys)
    assert lines[-1].startswith("ERROR the Todoist API kept rate-limiting")
    assert cli.transport.requests == ["*"]


def test_logging_is_detached_after_the_run(tmp_path, capsys):
    cli = Cli(FakeApi(ACCOUNT))
    cli("backup", str(tmp_path))
    capsys.readouterr()
    logging.getLogger("todoist_takeout.sync").warning("after the run")
    assert capsys.readouterr().err == ""


# --- 6.2 token and target directory -------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_token_exits_with_status_2_without_requests_or_files(tmp_path, capsys, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TODOIST_API_TOKEN")
    else:
        monkeypatch.setenv("TODOIST_API_TOKEN", value)
    target = tmp_path / "backup"
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(target)) == 2
    lines = stderr_lines(capsys)
    assert len(lines) == 1
    assert lines[0].startswith("ERROR ") and "TODOIST_API_TOKEN" in lines[0]
    assert cli.transport.requests == []
    assert not target.exists()


def test_new_target_is_created_with_its_parents(tmp_path):
    target = tmp_path / "a" / "b" / "backup"
    assert Cli(FakeApi(ACCOUNT))("backup", str(target)) == 0
    assert (target / "state.json").is_file()
    assert (target / "sync" / "items" / "6X7rM8997g3RQmvh.json").is_file()


def test_existing_backup_is_continued(tmp_path):
    assert Cli(FakeApi(ACCOUNT))("backup", str(tmp_path)) == 0
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(tmp_path)) == 0
    assert cli.transport.requests == ["INC-1"]


def test_directory_with_only_a_state_file_is_accepted(tmp_path):
    storage.load_or_create_state(tmp_path)
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(tmp_path)) == 0
    assert cli.transport.requests == ["*", "FULL-1"]


def test_foreign_directory_is_rejected_unchanged(tmp_path, capsys):
    (tmp_path / "notes.txt").write_bytes(b"mine\n")
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(tmp_path)) == 2
    lines = stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("ERROR ") and "not a todoist-takeout backup" in lines[0]
    assert file_snapshot(tmp_path) == {"notes.txt": b"mine\n"}
    assert cli.transport.requests == []


def test_file_target_is_rejected(tmp_path, capsys):
    target = tmp_path / "file"
    target.write_bytes(b"")
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(target)) == 2
    assert stderr_lines(capsys)[-1].startswith("ERROR ")
    assert target.read_bytes() == b""
    assert cli.transport.requests == []


# --- 6.3 exit status ----------------------------------------------------------------------


def test_success_with_a_warning_exits_with_status_0(tmp_path, capsys):
    assert Cli(FakeApi(NO_COMMENTS))("backup", str(tmp_path), "--verbose") == 0
    assert_token_absent(capsys.readouterr().err, tmp_path)


@pytest.mark.parametrize("verbose", [False, True])
def test_persistent_network_failure_exits_with_status_1(tmp_path, capsys, verbose):
    def unreachable(token):
        raise TransportError("cannot reach api.todoist.com: [Errno -3] Temporary failure in name resolution")

    cli = Cli(FakeApi(failure=unreachable))
    assert cli("backup", str(tmp_path), *(["--verbose"] if verbose else [])) == 1
    err = capsys.readouterr().err
    last = err.splitlines()[-1]
    assert last.startswith("ERROR cannot reach api.todoist.com") and "gave up after 5 attempts" in last
    assert cli.transport.requests == ["*"] * 5
    assert ("Traceback" in err) == verbose
    assert json.loads((tmp_path / "state.json").read_bytes())["sync_token"] is None
    assert_token_absent(err, tmp_path)


def test_auth_failure_exits_with_status_1(tmp_path, capsys):
    assert Cli(FakeApi(ACCOUNT))("backup", str(tmp_path)) == 0
    before = json.loads((tmp_path / "state.json").read_bytes())["sync_token"]
    capsys.readouterr()

    unauthorized = lambda token: (401, {}, b'{"error": "Invalid token", "error_tag": "AUTH_INVALID_TOKEN", "http_code": 401}')
    cli = Cli(FakeApi(failure=unauthorized))
    assert cli("backup", str(tmp_path), "--verbose") == 1
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("ERROR ") and "TODOIST_API_TOKEN" in err.splitlines()[-1]
    assert cli.transport.requests == ["INC-1"]
    assert json.loads((tmp_path / "state.json").read_bytes())["sync_token"] == before
    assert_token_absent(err, tmp_path)


def test_held_lock_exits_with_status_1_without_changes(tmp_path, capsys):
    assert Cli(FakeApi(ACCOUNT))("backup", str(tmp_path)) == 0
    before = file_snapshot(tmp_path)
    capsys.readouterr()
    cli = Cli(FakeApi(ACCOUNT))
    with TargetLock(tmp_path):
        assert cli("backup", str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("ERROR another run is in progress")
    assert cli.transport.requests == []
    assert file_snapshot(tmp_path) == before
    assert_token_absent(err, tmp_path)


def test_newer_format_version_exits_with_status_2(tmp_path, capsys):
    (tmp_path / "state.json").write_bytes(storage.encode_json({"format_version": 2, "sync_token": "X"}))
    cli = Cli(FakeApi(ACCOUNT))
    assert cli("backup", str(tmp_path), "--verbose") == 2
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("ERROR ") and "format version 2" in err
    assert cli.transport.requests == []
    assert_token_absent(err, tmp_path)


@pytest.mark.parametrize("verbose", [False, True])
def test_unexpected_error_exits_with_status_1_and_never_shows_the_token(tmp_path, capsys, verbose):
    def broken(token):
        raise RuntimeError(f"something went wrong with Bearer {TOKEN}")

    cli = Cli(FakeApi(failure=broken))
    assert cli("backup", str(tmp_path), *(["--verbose"] if verbose else [])) == 1
    err = capsys.readouterr().err
    last = err.splitlines()[-1]
    assert last.startswith("ERROR unexpected error: RuntimeError: something went wrong")
    assert ("rerun with --verbose" in last) != verbose
    assert ("Traceback" in err) == verbose
    assert "[redacted]" in err
    assert_token_absent(err, tmp_path)


# --- 6.4 unattended run ---------------------------------------------------------------------


class _SyncHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode("ascii"))
        if self.path != "/api/v1/sync" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, b'{"error": "Invalid token", "http_code": 401}')
        else:
            self._send(200, ACCOUNT[form["sync_token"][0]])

    def do_GET(self):
        # The completed-tasks backup: every endpoint has nothing to return.
        path = urllib.parse.urlsplit(self.path).path.removeprefix("/api/v1/")
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, b'{"error": "Invalid token", "http_code": 401}')
        else:
            self._send(200, empty_page(path))

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


# Runs the CLI against the local server instead of api.todoist.com.
_LOCAL_CLI = """
import sys
from todoist_takeout import api, cli

def client(token, max_rate_limit_wait):
    transport = api.UrllibTransport(token, base_url=sys.argv[1])
    return api.Client(transport, max_rate_limit_wait=max_rate_limit_wait)

sys.exit(cli.main(sys.argv[2:], client_factory=client))
"""


def test_runs_without_stdin_and_terminal(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SyncHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [sys.executable, "-c", _LOCAL_CLI, f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/", "backup", str(tmp_path)],
            env={**os.environ, "TODOIST_API_TOKEN": TOKEN},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            # Detached from any controlling terminal, as under cron or launchd.
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr.splitlines()[-1].startswith("INFO backup complete: 31 objects written")
    assert json.loads((tmp_path / "state.json").read_bytes())["sync_token"] == "INC-1"
    assert_token_absent(result.stderr, tmp_path)
