"""Opt-in tests against the real Todoist API.

Every test in this directory carries the `contract` marker, which the default
`pytest` run deselects. Select them with `pytest -m contract`; they skip unless
`TODOIST_CONTRACT_TOKEN` is set.
"""

import os
from pathlib import Path

import pytest

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(item.path).parents:
            item.add_marker(pytest.mark.contract)


@pytest.fixture
def contract_token():
    token = os.environ.get("TODOIST_CONTRACT_TOKEN", "")
    if not token:
        pytest.skip("TODOIST_CONTRACT_TOKEN is not set")
    return token
