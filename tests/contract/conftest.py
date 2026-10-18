"""Opt-in tests against the real Todoist API.

Every test in this directory carries the `contract` marker, which the default `pytest` run
deselects. Select them with `pytest -m contract`. Each test skips unless its variables are set:

- `TODOIST_CONTRACT_TOKEN`: the read account. Only read-only tests use it.
- `TODOIST_CONTRACT_WRITE_TOKEN`: the write account, a dedicated test account. Read-only tests run
  against it too; the write test uses only this token, and only with `TODOIST_CONTRACT_ALLOW_WRITES=1`.

Findings are written, scrubbed, to `tests/contract/findings/`, which is not committed.
"""

from pathlib import Path

import pytest

from todoist_takeout import storage
from todoist_takeout.api import Client, UrllibTransport

_HERE = Path(__file__).parent
FINDINGS = _HERE / "findings"


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(item.path).parents:
            item.add_marker(pytest.mark.contract)


@pytest.fixture
def client_for():
    return lambda token: Client(UrllibTransport(token))


@pytest.fixture
def write_client(contract_support, client_for):
    """The write token and a client for it, after the guard checked that it is not the read account."""
    write_token = contract_support.env_token(contract_support.WRITE_TOKEN)
    if not write_token:
        pytest.skip(f"{contract_support.WRITE_TOKEN} is not set")
    if not contract_support.writes_allowed():
        pytest.skip(f"{contract_support.ALLOW_WRITES} is not 1")
    read_token = contract_support.env_token(contract_support.READ_TOKEN)
    try:
        contract_support.check_write_account(write_token, read_token, client_for)
    except contract_support.AccountGuardError as exc:
        pytest.fail(f"refusing to write: {exc}")
    return write_token, client_for(write_token)


@pytest.fixture
def write_findings():
    """Write a findings document to tests/contract/findings/<name>.json.

    Callers scrub every API payload in it with `recorder.scrub`; the rest is the test's own text and
    the API's error responses, which hold no account data.
    """

    def write(name: str, findings: dict, token: str) -> Path:
        FINDINGS.mkdir(exist_ok=True)
        path = FINDINGS / f"{name}.json"
        data = storage.encode_json(findings)
        assert token.encode() not in data
        path.write_bytes(data)
        return path

    return write
