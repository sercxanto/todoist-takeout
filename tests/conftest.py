import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SYNC_FIXTURES = FIXTURES / "sync"


def load_sync_fixture(name: str) -> dict:
    return json.loads((SYNC_FIXTURES / f"{name}.json").read_bytes())


def load_module(name: str, path: Path):
    """Import a module that is not on the import path, such as a script, once per session."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture
def sync_fixture():
    return load_sync_fixture


@pytest.fixture
def recorder():
    """The fixture recorder script: scrubbing and response shapes."""
    return load_module("record_fixtures", ROOT / "scripts" / "record_fixtures.py")


@pytest.fixture
def demo():
    """The script that builds the demo backup for the README's screenshot."""
    return load_module("demo_screenshot", ROOT / "scripts" / "demo_screenshot.py")


@pytest.fixture
def contract_support():
    """Helpers of the contract tests that the offline tests check too."""
    return load_module("contract_support", Path(__file__).parent / "contract" / "support.py")
