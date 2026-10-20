"""Helpers for the contract tests: account variables, the write-account guard, sync commands, observer,
uploads and attachment downloads.

The offline test `tests/test_contract_support.py` checks the guard, so that a misconfigured shell
can never send write commands to the read account, and the upload and download helpers.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from todoist_takeout import attachments, completed, storage
from todoist_takeout.api import BASE_URL, TIMEOUT, USER_AGENT, Client, is_todoist_file_url
from todoist_takeout.attachments import Attachment
from todoist_takeout.errors import ApiError

READ_TOKEN = "TODOIST_CONTRACT_TOKEN"
WRITE_TOKEN = "TODOIST_CONTRACT_WRITE_TOKEN"
ALLOW_WRITES = "TODOIST_CONTRACT_ALLOW_WRITES"

ACCOUNT_VARIABLES = {"read": READ_TOKEN, "write": WRITE_TOKEN}


class AccountGuardError(Exception):
    """The write account is, or may be, the read account."""


def env_token(variable: str, environ=os.environ) -> str:
    return environ.get(variable, "").strip()


def writes_allowed(environ=os.environ) -> bool:
    return environ.get(ALLOW_WRITES, "").strip() == "1"


def user_id(client: Client):
    """The `user.id` of the token's account, from a read-only sync of the user resource only."""
    response = json.loads(client.post("sync", {"sync_token": "*", "resource_types": '["user"]'}))
    return response["user"]["id"]


def check_write_account(write_token: str, read_token: str, client_for) -> None:
    """Raise AccountGuardError unless the write token clearly belongs to another account.

    Both tokens are required: without the read token, nothing shows that the write token is not the
    personal one. `client_for(token)` returns a Client. Only read-only requests are made.
    """
    if not write_token:
        raise AccountGuardError(f"{WRITE_TOKEN} is not set")
    if not read_token:
        raise AccountGuardError(
            f"{READ_TOKEN} is not set; the write test needs it to check that {WRITE_TOKEN} belongs to another account"
        )
    if write_token == read_token:
        raise AccountGuardError(f"{WRITE_TOKEN} equals {READ_TOKEN}; writes must go to a dedicated test account")
    if user_id(client_for(write_token)) == user_id(client_for(read_token)):
        raise AccountGuardError(
            f"{WRITE_TOKEN} and {READ_TOKEN} belong to the same Todoist user; "
            "writes must go to a dedicated test account"
        )


class Observer:
    """Follows the account with its own sync token, to see the raw response of each step."""

    def __init__(self, client):
        self.client = client
        self.full = json.loads(client.sync("*"))
        self.token = self.full["sync_token"]

    def changes(self) -> dict:
        response = json.loads(self.client.sync(self.token))
        self.token = response["sync_token"]
        return response


@dataclass
class Rejected:
    """A command the API did not execute, with the error it gave."""

    command: str
    error: object

    def as_finding(self) -> dict:
        return {"command": self.command, "error": self.error}


@dataclass
class Commands:
    """Sends sync commands one at a time and keeps every rejection."""

    client: Client
    rejected: list[Rejected] = field(default_factory=list)

    def run(self, command: str, args: dict, *, temp_id: str | None = None):
        """Run one command. Return the new object's ID for adds, True for others, None if rejected."""
        entry = {"type": command, "uuid": str(uuid.uuid4()), "args": args}
        if temp_id is not None:
            entry["temp_id"] = temp_id
        try:
            response = json.loads(self.client.post("sync", {"commands": json.dumps([entry])}))
        except ApiError as exc:
            error = {
                "http_status": exc.http_status,
                "error": exc.error,
                "error_tag": exc.error_tag,
                "error_code": exc.error_code,
            }
            self.rejected.append(Rejected(command, error))
            return None
        status = response.get("sync_status", {}).get(entry["uuid"])
        if status != "ok":
            self.rejected.append(Rejected(command, status))
            return None
        if temp_id is not None:
            return response.get("temp_id_mapping", {}).get(temp_id)
        return True

    def add(self, command: str, args: dict):
        return self.run(command, args, temp_id=str(uuid.uuid4()))


# --- Completed tasks ---------------------------------------------------------

# by_completion_date listed new completions within seconds in the probes.
COMPLETION_TIMEOUT = 60.0
POLL_INTERVAL = 3.0


def completion_listed(client: Client, project_id, task_id, started: datetime) -> bool:
    """Wait until by_completion_date lists the completion; a snapshot fetches it only once."""
    since = completed.month_window(completed.month_of(started))[0]
    deadline = time.monotonic() + COMPLETION_TIMEOUT
    while True:
        until = completed.month_window(completed.month_of(datetime.now(timezone.utc)))[1]
        params = {"project_id": str(project_id), "since": since, "until": until, "limit": "50"}
        page = json.loads(client.get(completed.BY_COMPLETION_DATE, params))
        if str(task_id) in {str(task.get("id")) for task in page.get("items") or [] if isinstance(task, dict)}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


# --- Uploads and attachment files --------------------------------------------

UPLOADS = "uploads"


class CountingTransport:
    """Passes every request on to `transport` and lists the URL of each attachment file request."""

    def __init__(self, transport):
        self.transport = transport
        self.file_requests: list[str] = []

    def send(self, method, path, params):
        return self.transport.send(method, path, params)

    def download(self, url, write):
        self.file_requests.append(url)
        return self.transport.download(url, write)


def multipart(
    fields: dict[str, str], file_field: str, file_name: str, content: bytes, content_type: str
) -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type header."""
    boundary = f"todoist-takeout-{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api_request(
    token: str, method: str, path: str, data: bytes | None = None, content_type: str | None = None
) -> tuple[int, bytes]:
    """One request to the API that the tool's transport cannot send. Redirects are not followed, so
    the token goes to the API host only."""
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(urllib.parse.urljoin(BASE_URL, path), data=data, method=method, headers=headers)
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status if hasattr(response, "status") else response.code, response.read()


def upload_file(token: str, project_id: str, file_name: str, content: bytes, content_type: str) -> dict:
    """Upload a file with `POST /uploads` and return the attachment object that `note_add` takes."""
    body, header = multipart(
        {"file_name": file_name, "project_id": project_id}, "file", file_name, content, content_type
    )
    status, data = _api_request(token, "POST", UPLOADS, body, header)
    if not 200 <= status < 300:
        raise ApiError(f"POST /{UPLOADS} failed (HTTP {status})", http_status=status)
    return json.loads(data)


def delete_upload(token: str, file_url: str) -> int:
    """Delete an uploaded file with `DELETE /uploads` and return the status."""
    return _api_request(token, "DELETE", f"{UPLOADS}?{urllib.parse.urlencode({'file_url': file_url})}")[0]


def check_download(client: Client, attachment: Attachment, directory: Path) -> dict:
    """Download one attachment file into `directory` through the tool's own download step, and
    remove it again. Return its byte count and whether it equals the stated `file_size`.

    Raises SizeMismatch when it does not, and leaves `directory` as it found it, missing or empty.
    """
    assert is_todoist_file_url(attachment.url)
    directory = Path(directory)
    path = directory / storage.attachment_file_name(attachment.file_name)
    try:
        size = attachments._download(client, attachment, directory)
        assert sorted(os.listdir(directory)) == [path.name], "the download left another file behind"
        assert path.stat().st_size == size
    finally:
        path.unlink(missing_ok=True)
        if directory.is_dir() and not os.listdir(directory):
            directory.rmdir()
    return {"bytes": size, "size_stated": attachment.size is not None, "size_matches": size == attachment.size}
