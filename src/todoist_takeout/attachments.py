"""Downloading the uploaded files of stored comments (design D1 to D9).

Runs last in a backup. It reads every comment stored under `sync/`, `completed/comments/` and
`archived/`, and downloads the file of each comment whose `file_attachment` describes an uploaded
file to `attachments/<comment id>/<file name>`. A comment directory that holds a file is the record
that its file is stored, so `state.json` holds nothing for this part.
"""

import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from todoist_takeout import archived, completed, storage, sync
from todoist_takeout.api import Client, url_host
from todoist_takeout.errors import AttachmentSkipped, StorageError

log = logging.getLogger(__name__)

ATTACHMENTS_DIR = "attachments"
# The resource directories of `sync/` that hold comments.
SYNC_COMMENT_RESOURCES = ("notes", "project_notes")
# The `resource_type` values of attachments that link to another website (design D3).
LINK_TYPES = frozenset({"url"})
COMPLETED_UPLOAD = "completed"


@dataclass(frozen=True)
class Attachment:
    """The uploaded file of a stored comment."""

    comment_id: str
    url: str
    file_name: object  # as in the payload; `storage.attachment_file_name` makes it a valid name
    size: int | None


def run(target: Path, client: Client, now: Callable[[], datetime]) -> None:
    """Download the uploaded file of every stored comment whose file is not stored yet (design D1)."""
    root = Path(target) / ATTACHMENTS_DIR
    names = set(_listdir(root))
    missing = []
    stored = 0
    for attachment in collect(target):
        stem = storage.encode_key(attachment.comment_id)
        if _holds_file(root, names, stem):
            stored += 1
        else:
            missing.append((attachment, stem))
    if not missing:
        return

    log.info("downloading %d attachment files (%d already stored)", len(missing), stored)
    downloaded = failed = size = 0
    # The progress is logged also when the run fails, for example on the network (design D9).
    try:
        for attachment, stem in missing:
            # Checked again: the directory of another comment may have taken this one's place since.
            if _holds_file(root, names, stem):
                continue
            directory = root / stem
            try:
                size += _download(client, attachment, directory)
                downloaded += 1
            except AttachmentSkipped as exc:
                failed += 1
                log.warning("the attachment file of comment %s was not downloaded: %s", attachment.comment_id, exc)
            finally:
                _remove_if_empty(directory)
                if directory.is_dir():
                    names.add(stem)
    finally:
        log.info("downloaded %d attachment files (%.1f MB); %d could not be downloaded", downloaded, size / 1e6, failed)


def _download(client: Client, attachment: Attachment, directory: Path) -> int:
    """Download one file into `directory`, which is created only once the file host sends it.

    Return its size. Raises AttachmentSkipped for a file that a warning reports.
    """
    path = directory / storage.attachment_file_name(attachment.file_name)
    log.debug(
        "downloading the attachment file of comment %s from %s",
        attachment.comment_id,
        url_host(attachment.url) or "a URL without a host",
    )
    return client.download(attachment.url, lambda chunks: storage.write_stream_atomic(path, chunks, attachment.size))


def collect(target: Path) -> list[Attachment]:
    """The uploaded files of all stored comments, one per comment ID (design D2, D3).

    Each comes from the first copy of its comment that describes an uploaded file, in the order
    `sync/`, `completed/comments/`, `archived/`.
    """
    found: dict[str, Attachment] = {}
    for comment in _stored_comments(Path(target)):
        attachment = uploaded_file(comment)
        if attachment is not None:
            found.setdefault(attachment.comment_id, attachment)
    return list(found.values())


def uploaded_file(comment) -> Attachment | None:
    """The uploaded file a stored comment describes, or None for no attachment, a link, or an upload
    that is not complete (design D3)."""
    if not isinstance(comment, dict):
        return None
    comment_id = completed._id_text(comment.get("id"))
    attachment = comment.get("file_attachment")
    if comment_id is None or not isinstance(attachment, dict):
        return None
    url = attachment.get("file_url")
    if not isinstance(url, str) or not url:
        return None
    if attachment.get("upload_state") not in (None, COMPLETED_UPLOAD):
        return None
    resource_type = attachment.get("resource_type")
    if isinstance(resource_type, str) and resource_type in LINK_TYPES:
        return None
    size = attachment.get("file_size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        size = None
    return Attachment(comment_id, url, attachment.get("file_name"), size)


# --- Stored comments -------------------------------------------------------------------------


def _stored_comments(target: Path) -> Iterator:
    """Every stored comment, in the order of design D2, each file in name order."""
    for resource in SYNC_COMMENT_RESOURCES:
        # A live copy before its tombstone, which holds the deletion payload.
        for path in _json_files(
            target / sync.SYNC_DIR / storage.encode_key(resource),
            key=lambda name: (name.endswith(sync.DELETED_SUFFIX), name),
        ):
            yield _read_json(path)
    for path in _json_files(target / completed.COMPLETED_DIR / completed.COMMENTS_DIR):
        yield from _results(path)
    for project in _subdirectories(target / archived.ARCHIVED_DIR):
        for path in _json_files(project / archived.COMMENTS_DIR):
            yield from _results(path)
        yield from _results(project / archived.PROJECT_COMMENTS_FILE)


def _json_files(directory: Path, key=None) -> list[Path]:
    names = [name for name in _listdir(directory) if name.endswith(sync.JSON_SUFFIX) and not name.startswith(".")]
    return [directory / name for name in sorted(names, key=key)]


def _subdirectories(directory: Path) -> list[Path]:
    return [directory / name for name in sorted(_listdir(directory)) if (directory / name).is_dir()]


def _read_json(path: Path):
    data = storage.read_bytes(path)
    try:
        return json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError) as exc:
        raise StorageError(f"{path} is not a valid JSON file: {exc}") from exc


def _results(path: Path) -> list:
    """The comments of a response-shaped file (`{"results": [...]}`), or none if it does not exist."""
    stored = completed.read_stored(path, "results")
    return stored["results"] if stored is not None else []


# --- The attachments directory (design D4) ----------------------------------------------------


def _holds_file(root: Path, names: set[str], stem: str) -> bool:
    """Whether `attachments/<stem>/` holds a file other than a temp file.

    `names` are the entries of `attachments/`. Raises StorageError when the directory of a comment
    ID that differs only in letter case takes its place: on a case-insensitive file system they
    share a directory.
    """
    directory = root / stem
    if stem not in names:
        if directory.exists():
            raise StorageError(
                f"{directory} is the directory of another comment, whose ID differs from {stem!r} only in letter "
                "case; on a case-insensitive file system such IDs share a directory. The file of the comment "
                f"{stem!r} was not stored"
            )
        return False
    return any(not storage.is_temp_name(name) and (directory / name).is_file() for name in _listdir(directory))


def _remove_if_empty(directory: Path) -> None:
    # A rejected or failed download leaves the directory empty; a missing directory says the same.
    try:
        os.rmdir(directory)
    except OSError:
        pass


def _listdir(directory: Path) -> list[str]:
    try:
        return os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise StorageError(f"cannot list {directory}: {exc.strerror or exc}") from exc
