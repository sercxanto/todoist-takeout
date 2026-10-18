"""Exclusive, non-blocking lock on a target directory (design D11).

The lock is held on `<target>/.lock`. The operating system releases it when the process
ends, so a crashed run needs no cleanup. The lock file is never deleted, which avoids a
race between one run unlinking it and another run opening it.
"""

import errno
import os
from pathlib import Path

from todoist_takeout.errors import LockHeld, StorageError

LOCK_FILE = ".lock"

_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)

if os.name == "nt":
    import msvcrt

    _HELD_ERRNOS = {errno.EACCES, errno.EDEADLK}

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    _HELD_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class TargetLock:
    """Use as a context manager, or call acquire() and release()."""

    def __init__(self, target: Path):
        self.target = Path(target)
        self.path = self.target / LOCK_FILE
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, _OPEN_FLAGS, 0o666)
        except OSError as exc:
            raise StorageError(f"cannot open lock file {self.path}: {exc.strerror or exc}") from exc
        try:
            _try_lock(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _HELD_ERRNOS:
                raise LockHeld(
                    f"another run is in progress on {self.target} (it holds {self.path})"
                ) from None
            raise StorageError(f"cannot lock {self.path}: {exc.strerror or exc}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the file releases the lock as well
        finally:
            os.close(fd)

    def __enter__(self) -> "TargetLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
