"""On-disk primitives: JSON encoding, file names, atomic writes and the state file.

The format these functions produce is described in `storage_format.md`.
"""

import json
import os
import re
import secrets
import time
from collections.abc import Sequence
from pathlib import Path

from todoist_takeout import __version__
from todoist_takeout.errors import ConfigError, StorageError

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

FORMAT_VERSION = 1
STATE_FILE = "state.json"
MAX_NAME_LENGTH = 200

_WINDOWS = os.name == "nt"
_F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None)  # macOS only
_REPLACE_RETRIES = 10
_sleep = time.sleep


# --- JSON encoding -----------------------------------------------------------

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def encode_json(value) -> bytes:
    """Encode a JSON value the same way on every platform: UTF-8, two-space indent, LF, final newline."""
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (from a `\ud800` escape in the API's JSON) has no UTF-8 form.
        # It can only occur inside a JSON string, so writing it back as an escape keeps the value.
        return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text).encode("utf-8")


# --- File names --------------------------------------------------------------

_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(10)] + [f"LPT{i}" for i in range(10)]
)
KEY_SEPARATOR = "~"


def encode_component(component: str) -> str:
    """Encode one key component: keep `[A-Za-z0-9_-]`, write every other UTF-8 byte as `%XX`."""
    parts = []
    for char in component:
        if char in _SAFE_CHARS:
            parts.append(char)
        else:
            parts.extend(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
    encoded = "".join(parts)
    if encoded.upper() in _DEVICE_NAMES:
        encoded = f"%{ord(encoded[0]):02X}{encoded[1:]}"
    return encoded


def decode_component(encoded: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(encoded):
        if encoded[i] == "%":
            raw.append(int(encoded[i + 1 : i + 3], 16))
            i += 3
        else:
            raw.extend(encoded[i].encode("ascii"))
            i += 1
    return raw.decode("utf-8", "surrogatepass")


def encode_key(components: str | Sequence[str]) -> str:
    """Return the file-name stem for a key: encoded components joined with `~`."""
    if isinstance(components, str):
        components = [components]
    stem = KEY_SEPARATOR.join(encode_component(c) for c in components)
    if not stem:
        raise StorageError("cannot store an object with an empty key")
    if len(stem) > MAX_NAME_LENGTH:
        raise StorageError(
            f"key {list(components)!r} encodes to {len(stem)} characters, "
            f"more than the limit of {MAX_NAME_LENGTH}"
        )
    return stem


def decode_key(stem: str) -> tuple[str, ...]:
    return tuple(decode_component(part) for part in stem.split(KEY_SEPARATOR))


# --- Fault injection (tests only) --------------------------------------------


class InjectedFault(BaseException):
    """Simulates a crash in tests. Derives from BaseException so that no handler swallows it."""


_fault_after: int | None = None


def set_fault_after(writes: int | None) -> None:
    """Let the next `writes` writes succeed and make the one after raise InjectedFault.

    A file removal counts as a write. The hook fires once and then disarms itself. `None` disarms it.
    """
    global _fault_after
    _fault_after = writes


def _count_write() -> None:
    global _fault_after
    if _fault_after is None:
        return
    if _fault_after == 0:
        _fault_after = None
        raise InjectedFault("injected fault")
    _fault_after -= 1


# --- Atomic writes -----------------------------------------------------------

_TEMP_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
)


def read_bytes(path: Path) -> bytes | None:
    """Return the file's content, or None if it does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(f"cannot read {path}: {exc.strerror or exc}") from exc


def write_bytes_atomic(path: Path, data: bytes, *, full_sync: bool = False) -> bool:
    """Replace `path` with `data` atomically and durably. Return False if the file already held `data`.

    `full_sync` flushes the drive cache as well where plain fsync does not (macOS). Use it for
    the checkpoint, so that it never reaches the disk before the files written earlier.
    """
    path = Path(path)
    if read_bytes(path) == data:
        return False
    try:
        _ensure_dir(path.parent)
        fd, tmp = _create_temp(path)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                _sync_file(f.fileno(), full_sync)
        except BaseException:
            _unlink_quietly(tmp)
            raise
        # A fault injected here leaves the temp file behind, like a killed process would.
        _count_write()
        try:
            _replace(tmp, path)
        except BaseException:
            _unlink_quietly(tmp)
            raise
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc.strerror or exc}") from exc
    _sync_dir(path.parent)
    return True


def remove_file(path: Path) -> bool:
    """Remove `path` durably. Return False if it did not exist."""
    path = Path(path)
    if not path.exists():
        return False
    # A fault injected here leaves the file in place, like a process killed before the removal.
    _count_write()
    try:
        _retry_on_sharing_violation(os.unlink, path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StorageError(f"cannot remove {path}: {exc.strerror or exc}") from exc
    _sync_dir(path.parent)
    return True


def remove_temp_files(root: Path) -> int:
    """Remove temp files (`.*.tmp`) that an interrupted run left under `root`. Return their number."""
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(".") and name.endswith(".tmp"):
                path = Path(dirpath, name)
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise StorageError(f"cannot remove temp file {path}: {exc.strerror or exc}") from exc
                removed += 1
    return removed


def _create_temp(path: Path) -> tuple[int, Path]:
    while True:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            return os.open(tmp, _TEMP_FLAGS, 0o666), tmp
        except FileExistsError:
            continue


def _sync_file(fd: int, full_sync: bool) -> None:
    if full_sync and _F_FULLFSYNC is not None:
        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError:
            pass  # not supported by this file system; fall back to fsync
    os.fsync(fd)


def _replace(src: Path, dst: Path) -> None:
    _retry_on_sharing_violation(os.replace, src, dst)


def _retry_on_sharing_violation(operation, *args) -> None:
    # On Windows, virus scanners, indexers and sync clients briefly open files without
    # FILE_SHARE_DELETE, which makes a replace or removal fail with a sharing violation.
    delay = 0.05
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            operation(*args)
            return
        except PermissionError:
            if not _WINDOWS or attempt == _REPLACE_RETRIES:
                raise
            _sleep(delay)
            delay = min(delay * 2, 0.5)


def _ensure_dir(directory: Path) -> None:
    if directory.is_dir():
        return
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _sync_dir(created.parent)


def _sync_dir(directory: Path) -> None:
    # Makes a rename or a new entry durable on POSIX. NTFS journals metadata, and Windows
    # cannot open a directory for fsync.
    if _WINDOWS:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- State file --------------------------------------------------------------


def new_state() -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "sync_token": None,
        "last_full_sync_utc": None,
        "last_successful_run_utc": None,
        "tool_version": __version__,
    }


def read_state(target: Path) -> dict | None:
    """Read `state.json`, or return None if the target has none.

    Raises ConfigError if the file is unreadable as a state file or declares a newer format.
    """
    path = Path(target) / STATE_FILE
    data = read_bytes(path)
    if data is None:
        return None
    try:
        state = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"{path} is not a valid state file: {exc}") from exc
    if not isinstance(state, dict):
        raise ConfigError(f"{path} is not a valid state file: not a JSON object")
    version = state.get("format_version")
    if type(version) is not int or version < 1:
        raise ConfigError(f"{path} is not a valid state file: invalid format_version {version!r}")
    if version > FORMAT_VERSION:
        raise ConfigError(
            f"{path} declares backup format version {version}, but this version of "
            f"todoist-takeout supports only version {FORMAT_VERSION}; upgrade todoist-takeout"
        )
    return state


def write_state(target: Path, state: dict) -> None:
    """Write `state.json`, keeping fields this version does not know. The caller writes it last."""
    state = {**{k: v for k, v in state.items() if k != "tool_version"}, "tool_version": __version__}
    write_bytes_atomic(Path(target) / STATE_FILE, encode_json(state), full_sync=True)


def load_or_create_state(target: Path) -> dict:
    """Return the target's state, creating `state.json` without a sync token if it is missing."""
    state = read_state(target)
    if state is None:
        state = new_state()
        write_state(target, state)
    return state


# The `completed` object of state.json: the checkpoint of the completed-tasks backup.
COMPLETED_KEY = "completed"


def new_completed_state() -> dict:
    """The `completed` object of a backup that has not started this part yet."""
    return {
        "t0_utc": None,
        "first_month": None,
        "tasks_closed_through": None,
        "backfill_through": None,
        "backfill_status": "in_progress",
        "activity_fetched_utc": None,
    }


def completed_state(state: dict) -> dict:
    """Return a copy of the `completed` object of `state`, with defaults for missing fields.

    A state without the object, such as one written before this part existed, starts this part as
    on a first run. Fields this version does not know are kept.
    """
    stored = state.get(COMPLETED_KEY)
    if stored is None:
        return new_completed_state()
    if not isinstance(stored, dict):
        raise ConfigError(f"{STATE_FILE} is not a valid state file: {COMPLETED_KEY!r} is not a JSON object")
    return {**new_completed_state(), **stored}


def update_completed_state(target: Path, state: dict, **changes) -> None:
    """Set fields of the `completed` object of `state` and write `state.json`.

    The caller writes the files a change covers first.
    """
    state[COMPLETED_KEY] = {**completed_state(state), **changes}
    write_state(target, state)
