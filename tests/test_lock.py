import subprocess
import sys

import pytest

from todoist_takeout.errors import LockHeld
from todoist_takeout.lock import LOCK_FILE, TargetLock

HOLDER = """
import sys, time
from todoist_takeout.lock import TargetLock
TargetLock(sys.argv[1]).acquire()
print("locked", flush=True)
time.sleep(120)
"""

ATTEMPT = """
import sys
from todoist_takeout.errors import LockHeld
from todoist_takeout.lock import TargetLock
try:
    TargetLock(sys.argv[1]).acquire()
except LockHeld:
    sys.exit(3)
"""


def _attempt(target):
    return subprocess.run(
        [sys.executable, "-c", ATTEMPT, str(target)], stdin=subprocess.DEVNULL, timeout=60
    ).returncode


def test_lock_held_by_another_process_until_it_is_killed(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert _attempt(tmp_path) == 3
    finally:
        holder.kill()
        holder.wait(timeout=60)
        holder.stdout.close()
    assert _attempt(tmp_path) == 0
    assert (tmp_path / LOCK_FILE).exists()


def test_second_lock_in_same_process_fails(tmp_path):
    with TargetLock(tmp_path):
        with pytest.raises(LockHeld, match="another run is in progress"):
            TargetLock(tmp_path).acquire()


def test_release_allows_a_new_lock_and_keeps_the_file(tmp_path):
    lock = TargetLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()  # releasing twice is harmless
    with TargetLock(tmp_path):
        pass
    assert (tmp_path / LOCK_FILE).exists()
