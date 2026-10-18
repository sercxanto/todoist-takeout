import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SYNC_FIXTURES = FIXTURES / "sync"


def load_sync_fixture(name: str) -> dict:
    return json.loads((SYNC_FIXTURES / f"{name}.json").read_bytes())


@pytest.fixture
def sync_fixture():
    return load_sync_fixture
