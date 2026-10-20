"""Offline checks of the contract tests: the write-account guard and the skipped path."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from todoist_takeout.api import Client
from todoist_takeout.attachments import Attachment
from todoist_takeout.errors import SizeMismatch

from fake_api import FakeApi, file_body

ROOT = Path(__file__).parent.parent
READ = "read-token-0123456789abcdef0123456789abcd"
WRITE = "write-token-0123456789abcdef0123456789abc"


class UserTransport:
    """Answers a user-only sync with the user ID of the token; records every request."""

    def __init__(self, user_ids, requests):
        self.user_ids = user_ids
        self.requests = requests

    def for_token(self, token):
        transport = self

        class Bound:
            def send(self, method, path, form):
                transport.requests.append(dict(form))
                return 200, {}, json.dumps({"user": {"id": transport.user_ids[token]}}).encode()

        return Client(Bound(), sleep=lambda delay: None)


def guard(contract_support, write, read, user_ids):
    requests = []
    transport = UserTransport(user_ids, requests)
    contract_support.check_write_account(write, read, transport.for_token)
    return requests


def test_guard_refuses_the_same_token(contract_support):
    with pytest.raises(contract_support.AccountGuardError, match="equals"):
        guard(contract_support, READ, READ, {})


def test_guard_refuses_two_tokens_of_the_same_user(contract_support):
    requests = []
    transport = UserTransport({READ: "42", WRITE: "42"}, requests)
    with pytest.raises(contract_support.AccountGuardError, match="same Todoist user"):
        contract_support.check_write_account(WRITE, READ, transport.for_token)
    assert requests == [{"sync_token": "*", "resource_types": '["user"]'}] * 2  # read-only, no commands


def test_guard_accepts_two_accounts(contract_support):
    requests = guard(contract_support, WRITE, READ, {READ: "1", WRITE: "2"})
    assert all("commands" not in form for form in requests)


def test_guard_refuses_a_missing_read_token_without_a_request(contract_support):
    requests = []
    transport = UserTransport({}, requests)
    with pytest.raises(contract_support.AccountGuardError, match="TODOIST_CONTRACT_TOKEN is not set"):
        contract_support.check_write_account(WRITE, "", transport.for_token)
    assert requests == []


def test_guard_refuses_a_missing_write_token(contract_support):
    with pytest.raises(contract_support.AccountGuardError, match="TODOIST_CONTRACT_WRITE_TOKEN"):
        guard(contract_support, "", READ, {})


def test_writes_need_the_explicit_flag(contract_support):
    assert contract_support.writes_allowed({"TODOIST_CONTRACT_ALLOW_WRITES": "1"})
    for value in ["", "0", "true", "yes"]:
        assert not contract_support.writes_allowed({"TODOIST_CONTRACT_ALLOW_WRITES": value})


def test_contract_tests_skip_without_the_variables(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TODOIST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "contract", "-q", "-rs", "-p", "no:cacheprovider", "tests/contract"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "12 skipped" in result.stdout, result.stdout
    assert "passed" not in result.stdout and "failed" not in result.stdout
    assert "TODOIST_CONTRACT_TOKEN is not set" in result.stdout
    assert "TODOIST_CONTRACT_WRITE_TOKEN is not set" in result.stdout


def test_write_tests_skip_without_the_allow_flag(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TODOIST_")}
    env["TODOIST_CONTRACT_WRITE_TOKEN"] = WRITE
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "contract",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/contract/test_write.py",
            "tests/contract/test_archived_write.py",
            "tests/contract/test_attachments_write.py",
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 skipped" in result.stdout and "TODOIST_CONTRACT_ALLOW_WRITES is not 1" in result.stdout


# --- Uploads and attachment downloads ----------------------------------------


def test_multipart_body_holds_the_fields_and_the_file(contract_support):
    body, header = contract_support.multipart(
        {"file_name": "a: b.txt", "project_id": "P1"}, "file", "a: b.txt", b"bytes\r\n", "text/plain"
    )
    boundary = header.removeprefix("multipart/form-data; boundary=")
    assert header != boundary and boundary.startswith("todoist-takeout-")
    assert body.split(f"--{boundary}".encode()) == [
        b"",
        b'\r\nContent-Disposition: form-data; name="file_name"\r\n\r\na: b.txt\r\n',
        b'\r\nContent-Disposition: form-data; name="project_id"\r\n\r\nP1\r\n',
        b'\r\nContent-Disposition: form-data; name="file"; filename="a: b.txt"\r\nContent-Type: text/plain\r\n\r\n'
        b"bytes\r\n\r\n",
        b"--\r\n",
    ]


URL = "https://files.todoist.com/fake/support/N1/notiz.txt"


def test_check_download_stores_through_the_tool_and_leaves_nothing(contract_support, tmp_path):
    transport = contract_support.CountingTransport(FakeApi(files={URL: file_body("notiz.txt", 300)}))
    result = contract_support.check_download(
        Client(transport), Attachment("N1", URL, "a: b.txt", 300), tmp_path / "check"
    )
    assert result == {"bytes": 300, "size_stated": True, "size_matches": True}
    assert transport.file_requests == [URL]
    assert list(tmp_path.iterdir()) == []


def test_check_download_of_another_size_raises_and_leaves_nothing(contract_support, tmp_path):
    transport = contract_support.CountingTransport(FakeApi(files={URL: file_body("notiz.txt", 200)}))
    with pytest.raises(SizeMismatch):
        contract_support.check_download(Client(transport), Attachment("N1", URL, "notiz.txt", 300), tmp_path / "check")
    assert list(tmp_path.iterdir()) == []


def test_counting_transport_passes_api_requests_on(contract_support):
    api = FakeApi({"*": b'{"sync_token": "S1"}'})
    transport = contract_support.CountingTransport(api)
    assert Client(transport).sync("*") == b'{"sync_token": "S1"}'
    assert api.requests == ["*"] and transport.file_requests == []
