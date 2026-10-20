"""Read-only contract test: the completed-tasks endpoints against the shapes in tests/fixtures/shapes.json.

It sends the requests of `scripts/record_fixtures.py record`, one page each, and fails on added
keys, fields or types. It also fails if `tasks/completed` is gone or the activity log rejects the
completion filter, so that either is noticed before users lose recurring completions (design D14).
"""

import json
import warnings
from datetime import datetime, timezone

import pytest

from todoist_takeout import completed
from todoist_takeout.errors import ApiError


class Watching:
    """A client wrapper that remembers the path of the last GET request."""

    def __init__(self, client):
        self.client = client
        self.path = None

    def get(self, path, params):
        self.path = path
        return self.client.get(path, params)


@pytest.mark.parametrize("account", ["read", "write"])
def test_completed_endpoints_match_recorded_shapes(account, contract_support, recorder, client_for, write_findings):
    variable = contract_support.ACCOUNT_VARIABLES[account]
    token = contract_support.env_token(variable)
    if not token:
        pytest.skip(f"{variable} is not set")

    client = Watching(client_for(token))
    try:
        bodies = recorder.record_completed(client, datetime.now(timezone.utc))
    except ApiError as exc:
        if client.path == completed.TASKS_COMPLETED and exc.http_status in completed.GONE_STATUSES:
            pytest.fail(
                f"GET /{completed.TASKS_COMPLETED} is gone (HTTP {exc.http_status}); "
                f"recurring completions before T0 can no longer be backed up: {exc}"
            )
        if client.path == completed.ACTIVITIES:
            pytest.fail(f"the activity log rejected object_event_types={completed.COMPLETION_EVENTS}: {exc}")
        raise

    expected = json.loads(recorder.SHAPES.read_bytes())["endpoints"]
    findings, added = {}, []
    for name, body in bodies.items():
        want = expected[name]
        new, removed = recorder.compare_shapes(want, recorder.endpoint_shape_of(json.loads(body)))
        # The last page of a cursor walk has no `next_cursor`, and one page may be the last.
        removed = [line for line in removed if line != f"key {completed.CURSOR!r}"]
        findings[name] = {"shape_sources": want["sources"], "added": new, "removed": removed}
        added += [f"{name}: {line}" for line in new]
        for line in removed:
            warnings.warn(f"{account} account, {name}: not in the response: {line}")
    for name in sorted(recorder.COMPLETED_ENDPOINTS.keys() - bodies.keys()):
        findings[name] = "not requested"
        warnings.warn(f"{account} account: no completed task with comments found; {name} not compared")
    write_findings(f"completed-read-{account}", findings, token)

    assert not added, "the responses have keys, fields or types that shapes.json lacks:\n" + "\n".join(added)
