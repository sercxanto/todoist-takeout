"""A fake Todoist API for the offline tests.

It answers sync requests by their sync token and GET requests by path and parameters, and records
every request. GET endpoints that a test does not configure return an empty page, so that tests of
the sync keep working while the completed-tasks and archived-projects backups run after it.

Attachment files are answered by URL, through the real `UrllibTransport.download` with a stub
opener, so that redirects, the token rule and gzip are handled by the code under test. The files
of the hand-written fixtures are served unless a test configures otherwise.
"""

import email.message
import gzip
import io
import json
import urllib.error
from pathlib import Path

from todoist_takeout.api import UrllibTransport

FIXTURES = Path(__file__).parent / "fixtures"
COMPLETED_FIXTURES = FIXTURES / "completed"
ARCHIVED_FIXTURES = FIXTURES / "archived"
ATTACHMENT_FIXTURES = FIXTURES / "attachments"

# The token the fake's file transport sends to Todoist hosts.
FILE_TOKEN = "fake-file-token"

BY_COMPLETION_DATE = "tasks/completed/by_completion_date"
TASKS_COMPLETED = "tasks/completed"
ACTIVITIES = "activities"
COMMENTS = "comments"
ARCHIVED_PROJECTS = "projects/archived"

# What each GET endpoint returns when it has nothing, in the shape of its real responses.
EMPTY_PAGES = {
    BY_COMPLETION_DATE: {"items": []},
    TASKS_COMPLETED: {"items": [], "projects": {}, "sections": {}},
    ACTIVITIES: {"results": [], "next_cursor": None},
    COMMENTS: {"results": [], "next_cursor": None},
    ARCHIVED_PROJECTS: {"results": [], "next_cursor": None},
}


def completed_fixture(name: str) -> dict:
    return json.loads((COMPLETED_FIXTURES / f"{name}.json").read_bytes())


def archived_fixture(name: str) -> dict:
    return json.loads((ARCHIVED_FIXTURES / f"{name}.json").read_bytes())


def attachment_fixture(name: str) -> dict:
    return json.loads((ATTACHMENT_FIXTURES / f"{name}.json").read_bytes())


def file_body(name: str, size: int) -> bytes:
    """The invented content of a fixture file: lines naming it, cut to `size` bytes."""
    line = f"content of the attachment file {name}\n".encode()
    return (line * (size // len(line) + 1))[:size]


# The uploaded files of the hand-written fixtures, by `file_url`, each as long as its `file_size`.
FIXTURE_FILES = {
    "https://files.todoist.com/fake/sync/6X7rfFVQXwRmcvFp/list.pdf": file_body("list.pdf", 10520),
    "https://files.todoist.com/fake/completed/6X8Hc2Jf4mQpRw9V/preise.pdf": file_body("preise.pdf", 20480),
    "https://files.todoist.com/fake/archived/6Xc7Gd2Nr5Th8Wk3/fliesen.jpg": file_body("fliesen.jpg", 183204),
}


def empty_page(path: str) -> bytes:
    """The body of an empty page of a GET endpoint. Fails for an endpoint the tool should not request."""
    assert path in EMPTY_PAGES, f"unexpected GET request to {path}"
    return json.dumps(EMPTY_PAGES[path]).encode()


def _body(response) -> bytes:
    return response if isinstance(response, bytes) else json.dumps(response).encode()


class FakeApi:
    """A transport that answers from fixtures instead of the network.

    `sync` maps a sync token to the response body for it, and `failure`, if given, answers every
    sync request instead: it gets the sync token and returns a status, headers and body, or raises.

    GET answers come from rules added with `answer`. A rule applies when every parameter it names
    has that value in the request; a parameter set to None must be absent. Of the rules for the
    path that apply, the one that names the most parameters wins, and among those the one added last.

    `requests` lists the sync tokens of the sync requests in order; `get_requests` lists the path
    and parameters of each GET request.

    Attachment files are answered from routes added with `serve_file`, `answer_file`,
    `redirect_file` and `fail_file`, starting with `files` (by default `FIXTURE_FILES`). A request
    for a URL without a route fails the test. `file_requests` lists the URL of each file request
    the client made, one per attempt; `file_hops` lists each request the file transport sent, as
    the URL and its `Authorization` header (None if it had none).
    """

    def __init__(self, sync=None, *, failure=None, files=FIXTURE_FILES):
        self.sync = dict(sync or {})
        self.failure = failure
        self.rules = []
        self.requests = []
        self.get_requests = []
        self.file_routes = {}
        self.file_requests = []
        self.file_hops = []
        for url, body in files.items():
            self.serve_file(url, body)
        self._files = UrllibTransport(FILE_TOKEN)
        self._files._opener = _FileOpener(self)

    def answer(self, path: str, params: dict, response, *, status: int = 200) -> "FakeApi":
        """Answer GET `path` with `response` (a JSON value or bytes) when `params` match."""
        self.rules.append((path, dict(params), status, _body(response)))
        return self

    def cursor_pages(self, path: str, params: dict, pages: list[dict]) -> "FakeApi":
        """Answer a cursor walk: the first page without a cursor, each later one for the cursor before it."""
        cursor = None
        for page in pages:
            self.answer(path, {**params, "cursor": cursor}, page)
            cursor = page.get("next_cursor")
        return self

    def offset_pages(self, path: str, params: dict, pages: list[dict], limit: int) -> "FakeApi":
        """Answer an offset walk: page i for `offset` i × `limit`."""
        for i, page in enumerate(pages):
            self.answer(path, {**params, "offset": str(i * limit)}, page)
        return self

    def requested(self, path: str) -> list[dict]:
        """The parameters of the GET requests to `path`, in order."""
        return [params for requested, params in self.get_requests if requested == path]

    def send(self, method, path, params):
        if method == "POST":
            assert path == "sync", f"unexpected POST request to {path}"
            token = params["sync_token"]
            self.requests.append(token)
            if self.failure is not None:
                return self.failure(token)
            return 200, {}, self.sync[token]
        assert method == "GET", f"unexpected {method} request"
        self.get_requests.append((path, dict(params)))
        best = None
        for rule in self.rules:
            rule_path, rule_params, _status, _body = rule
            if rule_path == path and all(params.get(k) == v for k, v in rule_params.items()):
                if best is None or len(rule_params) >= len(best[1]):
                    best = rule
        if best is None:
            return 200, {}, empty_page(path)
        return best[2], {}, best[3]

    # --- Attachment files ----------------------------------------------------------------------

    def serve_file(self, url: str, body: bytes, *, gzipped: bool = False, fail_after: int | None = None,
                   times: int | None = None) -> "FakeApi":
        """Answer `url` with 200 and `body`, with a Content-Length.

        `gzipped` sends the body gzip-compressed with `Content-Encoding: gzip`. `fail_after` makes
        reading the body time out after that many bytes. `times` limits the answer to that many
        requests, after which the next route added for `url` applies.
        """
        headers = {}
        if gzipped:
            body = gzip.compress(body, mtime=0)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(body))
        return self._route(url, (200, headers, body, fail_after), times)

    def answer_file(self, url: str, status: int, body: bytes = b"", headers=None, *,
                    times: int | None = None) -> "FakeApi":
        """Answer `url` with any status, such as 404, or 429 with a `Retry-After` header."""
        return self._route(url, (status, dict(headers or {}), body, None), times)

    def redirect_file(self, url: str, location: str, *, status: int = 302) -> "FakeApi":
        """Answer `url` with a redirect to `location`. Chain calls for a redirect chain."""
        return self._route(url, (status, {"Location": location}, b"", None), None)

    def fail_file(self, url: str, *, times: int | None = None) -> "FakeApi":
        """Fail the connection to `url`, as when the network is down."""
        return self._route(url, urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), times)

    def _route(self, url: str, answer, times: int | None) -> "FakeApi":
        # Routes with a limit answer first, in the order added. The one without a limit answers
        # after them; adding another replaces it.
        routes = self.file_routes.setdefault(url, [])
        fallback = routes[-1:] if routes and routes[-1][1] is None else []
        limited = routes[: len(routes) - len(fallback)]
        if times is None:
            fallback = [[answer, None]]
        else:
            limited.append([answer, times])
        routes[:] = limited + fallback
        return self

    def download(self, url, write):
        self.file_requests.append(url)
        return self._files.download(url, write)

    def _next_file_answer(self, url: str):
        routes = self.file_routes.get(url)
        assert routes, f"unexpected file request to {url}"
        route = routes[0]
        if route[1] is not None:
            route[1] -= 1
            if route[1] == 0:
                routes.pop(0)
        return route[0]


class _FileResponse(io.BytesIO):
    """A 2xx response of the stub opener, whose body can time out part way."""

    def __init__(self, status, headers, body, fail_after):
        super().__init__(body)
        self.status = status
        self.headers = headers
        self._fail_after = fail_after

    def read(self, size=-1):
        if self._fail_after is not None and self.tell() >= self._fail_after:
            raise TimeoutError("timed out")
        if self._fail_after is not None and size is not None and size >= 0:
            size = min(size, self._fail_after - self.tell())
        return super().read(size)


class _FileOpener:
    """Answers like the transport's real opener, which never follows redirects: non-2xx raise HTTPError."""

    def __init__(self, api: FakeApi):
        self.api = api

    def open(self, request, timeout):
        url = request.full_url
        self.api.file_hops.append((url, request.get_header("Authorization")))
        answer = self.api._next_file_answer(url)
        if isinstance(answer, Exception):
            raise answer
        status, headers, body, fail_after = answer
        message = email.message.Message()
        for name, value in headers.items():
            message[name] = value
        if 200 <= status < 300:
            return _FileResponse(status, message, body, fail_after)
        raise urllib.error.HTTPError(url, status, "fake", message, io.BytesIO(body))
