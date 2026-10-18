"""A fake Todoist API for the offline tests.

It answers sync requests by their sync token and GET requests by path and parameters, and records
every request. GET endpoints that a test does not configure return an empty page, so that tests of
the sync keep working while the completed-tasks backup runs after it.
"""

import json
from pathlib import Path

COMPLETED_FIXTURES = Path(__file__).parent / "fixtures" / "completed"

BY_COMPLETION_DATE = "tasks/completed/by_completion_date"
TASKS_COMPLETED = "tasks/completed"
ACTIVITIES = "activities"
COMMENTS = "comments"

# What each GET endpoint returns when it has nothing, in the shape of its real responses.
EMPTY_PAGES = {
    BY_COMPLETION_DATE: {"items": []},
    TASKS_COMPLETED: {"items": [], "projects": {}, "sections": {}},
    ACTIVITIES: {"results": [], "next_cursor": None},
    COMMENTS: {"results": [], "next_cursor": None},
}


def completed_fixture(name: str) -> dict:
    return json.loads((COMPLETED_FIXTURES / f"{name}.json").read_bytes())


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
    """

    def __init__(self, sync=None, *, failure=None):
        self.sync = dict(sync or {})
        self.failure = failure
        self.rules = []
        self.requests = []
        self.get_requests = []

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
