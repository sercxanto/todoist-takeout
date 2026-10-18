import subprocess
import sys

import pytest

from todoist_takeout import __version__
from todoist_takeout.cli import main


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
