"""Helpers for the attachment probes R8 and W9 of add-attachment-downloads (design D11).

The probes are a spike: they answer the open questions in that change's proposal and write what
they observe to `tests/contract/findings/`. Nothing here is used by the tool. Whatever proves worth
keeping becomes a regular contract test in the tasks of that change, and this module is removed.

Findings hold booleans, counts, types, statuses, field and header names, and host names only, never
an ID, URL path, query string, file name or file content. `Secrets` collects every string the
probes see in a payload, and `check_findings` refuses a findings document that contains one of them
or a string of another form.

The offline test `tests/test_contract_probes.py` checks these helpers and runs R8 against fakes.
"""

import json
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from todoist_takeout import archived, completed
from todoist_takeout.api import BASE_URL, USER_AGENT, Client, cursor_pages
from todoist_takeout.errors import ApiError

# R8 limits, so that the probe stays within a few minutes and well inside the rate limit.
SEARCH_MONTHS = 3
MAX_TASKS = 30
MAX_ARCHIVED_PROJECTS = 10
MAX_FILES = 5
# One more than the tool will follow (design D5), to see whether a chain ends after 5 hops.
MAX_HOPS = 6
CHUNK = 64 * 1024
TIMEOUT = 120.0

UPLOADS = "uploads"
COMMENTS = completed.COMMENTS
COMMENTS_PAGE_LIMIT = 100
# Field names of the sync response that may point to a file (design D11).
FILE_WORDS = ("url", "file", "image")
# Words in a query parameter name that suggest a signed or expiring URL. Only the booleans are kept.
QUERY_HINTS = {
    "expiry": ("expir", "exp"),
    "signature": ("sig", "signature", "hmac"),
    "credential": ("credential", "key", "token", "x-amz", "policy"),
}
# Response headers recorded by name only.
RATE_LIMIT_WORDS = ("ratelimit", "rate-limit", "retry-after")
# Attachment fields whose values are recorded, since they are enumerations or MIME types.
ENUM_FIELDS = ("resource_type", "upload_state", "file_type")
ENUM_VALUE = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,40}(/[a-z0-9][a-z0-9_.+-]{0,40})?$")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
FIELD_NAME = re.compile(r"^[A-Za-z0-9_.:/+~ <>-]{0,80}$")
# Shorter strings of a payload are not treated as secrets; they would match field names.
MIN_SECRET_LENGTH = 5


class ProbeError(Exception):
    """A probe could not continue, for example because a command it depends on was rejected."""


def require(value, what: str):
    if not value:
        raise ProbeError(f"cannot continue: {what}")
    return value


# --- URLs --------------------------------------------------------------------


def host_of(url) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def is_todoist_url(url) -> bool:
    """The host rule of design D5: https, host todoist.com or *.todoist.com, no user info, port 443."""
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme.lower() == "https"
        and (host == "todoist.com" or host.endswith(".todoist.com"))
        and "@" not in parts.netloc
        and port in (None, 443)
    )


def url_facts(url) -> dict:
    """Scheme, host and booleans about the query of a URL; never its path or query."""
    if not isinstance(url, str):
        return {"present": url is not None, "type": json_type(url)}
    parts = urllib.parse.urlsplit(url)
    names = [name.lower() for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return {
        "scheme": parts.scheme.lower() if parts.scheme in ("http", "https", "HTTP", "HTTPS") else "<other>",
        "host": host_of(url),
        "todoist_host": is_todoist_url(url),
        "has_query": bool(parts.query),
        "query_hints": {hint: any(word in name for name in names for word in words)
                        for hint, words in QUERY_HINTS.items()},
    }


# --- Payloads ----------------------------------------------------------------


def json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "<other>"


def enum_value(value):
    """The value of an enumeration-like field, or its type when it does not look like one."""
    if isinstance(value, str) and ENUM_VALUE.match(value):
        return value
    return f"<{json_type(value)}>"


def attachment_facts(attachment) -> dict:
    """What a `file_attachment` is, without any of its values other than enumerations and hosts."""
    if not isinstance(attachment, dict):
        return {"type": json_type(attachment)}
    facts = {
        "type": "object",
        "keys": sorted(str(key) for key in attachment),
        "value_types": {str(key): json_type(value) for key, value in sorted(attachment.items())},
    }
    for name in ENUM_FIELDS:
        if name in attachment:
            facts[name] = enum_value(attachment[name])
    for key, value in sorted(attachment.items()):
        if isinstance(value, str) and urllib.parse.urlsplit(value).scheme.lower() in ("http", "https"):
            facts.setdefault("urls", {})[str(key)] = url_facts(value)
    size = attachment.get("file_size")
    facts["file_size_is_count"] = isinstance(size, int) and not isinstance(size, bool) and size >= 0
    return facts


def summarize(attachments) -> list[dict]:
    """Distinct attachment facts with how often each occurs, most frequent first."""
    counts = Counter(json.dumps(attachment_facts(a), sort_keys=True) for a in attachments)
    return [{"count": count, "facts": json.loads(facts)} for facts, count in counts.most_common()]


def comment_facts(comments) -> dict:
    comments = [c for c in comments or [] if isinstance(c, dict)]
    return {
        "comments": len(comments),
        "without_field": sum(1 for c in comments if "file_attachment" not in c),
        "null": sum(1 for c in comments if c.get("file_attachment") is None),
        "deleted_with_attachment": sum(1 for c in comments if c.get("is_deleted") and c.get("file_attachment")),
        "attachments": summarize(c["file_attachment"] for c in comments if c.get("file_attachment") is not None),
    }


def file_fields(response) -> list[str]:
    """`<resource>: <field>` for every field name in a response that contains a word of FILE_WORDS.

    Only the top-level key and the field's own name are kept: the keys in between can be IDs.
    """
    found: set[str] = set()

    def walk(resource, value):
        if isinstance(value, dict):
            for key, inner in value.items():
                key = str(key)
                if FIELD_NAME.match(key) and re.fullmatch(r"[a-z_][a-z0-9_]*", key) and \
                        any(word in key for word in FILE_WORDS):
                    found.add(f"{resource}: {key}")
                walk(resource, inner)
        elif isinstance(value, list):
            for inner in value:
                walk(resource, inner)

    if isinstance(response, dict):
        for resource, value in response.items():
            walk(str(resource), value)
    return sorted(found)


def is_upload(attachment) -> bool:
    """A candidate for an uploaded file: an object with a `file_url` and no incomplete upload."""
    return (
        isinstance(attachment, dict)
        and isinstance(attachment.get("file_url"), str)
        and attachment.get("upload_state", "completed") == "completed"
    )


# --- Secrets -----------------------------------------------------------------


class Secrets:
    """Every string seen in a payload, which no findings document may contain."""

    def __init__(self):
        self.values: set[str] = set()

    def add(self, value) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ENUM_FIELDS:
                    continue
                self.add(inner)
        elif isinstance(value, list):
            for inner in value:
                self.add(inner)
        elif isinstance(value, (str, bytes)):
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            self._add_text(text)
            if text.lower().startswith(("http://", "https://")):
                parts = urllib.parse.urlsplit(text)
                self._add_text(parts.path)
                self._add_text(parts.query)
                for name, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
                    self._add_text(val)
                self._add_text(urllib.parse.unquote(parts.path.rsplit("/", 1)[-1]))
        elif isinstance(value, int) and not isinstance(value, bool):
            if abs(value) >= 10 ** 6:  # numeric IDs, not counts
                self._add_text(str(value))

    def _add_text(self, text: str) -> None:
        if len(text) >= MIN_SECRET_LENGTH:
            self.values.add(text)


def check_findings(findings, secrets: Secrets) -> list[str]:
    """Problems that make a findings document unsafe to write; empty when it is safe."""
    problems: list[str] = []

    def check_text(text: str, where: str) -> None:
        if not FIELD_NAME.match(text) and not HOST.match(text):
            problems.append(f"{where}: a string of unexpected form")
            return
        for secret in secrets.values:
            if secret in text:
                problems.append(f"{where}: a value seen in a payload")
                return

    def walk(value, where: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                check_text(str(key), f"{where}/<key>")
                walk(inner, f"{where}/{key}" if FIELD_NAME.match(str(key)) else f"{where}/<key>")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                walk(inner, f"{where}[{i}]")
        elif isinstance(value, str):
            check_text(value, where)
        elif not (value is None or isinstance(value, (bool, int, float))):
            problems.append(f"{where}: a value of type {type(value).__name__}")

    walk(findings, "")
    return problems


# --- Raw HTTP ----------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Answer:
    """One HTTP response: status, headers, and the body, kept or only measured."""

    status: int
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    size: int = 0
    decoded_size: int | None = None

    def header(self, name: str):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class RawHttp:
    """Single HTTPS requests without redirects, for what the tool's transport cannot do yet.

    A body is kept in memory when `keep_body` is set, else written to a temporary file in `workdir`,
    measured and deleted again. A `Content-Encoding: gzip` body is decoded only to measure it.
    """

    def __init__(self, workdir: Path, timeout: float = TIMEOUT):
        self.workdir = Path(workdir)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
        )

    def request(self, method: str, url: str, *, token: str | None = None, headers: dict | None = None,
                data: bytes | None = None, keep_body: bool = False) -> Answer:
        headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            answer = Answer(response.status if hasattr(response, "status") else response.code,
                            dict(response.headers.items()))
            gzip = (answer.header("Content-Encoding") or "").lower() == "gzip"
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip else None
            decoded = 0
            kept = bytearray()
            with tempfile.TemporaryFile(dir=self.workdir) as sink:
                while chunk := response.read(CHUNK):
                    answer.size += len(chunk)
                    if decoder is not None:
                        decoded += len(decoder.decompress(chunk))
                    if keep_body:
                        kept.extend(chunk)
                    else:
                        sink.write(chunk)
            if decoder is not None:
                answer.decoded_size = decoded + len(decoder.flush())
            if keep_body:
                answer.body = bytes(kept)
        return answer


def multipart(fields: dict[str, str], file_field: str, file_name: str, content: bytes,
              content_type: str) -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type header."""
    boundary = f"todoist-takeout-{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --- File requests -----------------------------------------------------------


def hop_facts(url: str, answer: Answer | None, *, token_sent: bool) -> dict:
    facts = {"scheme": url_facts(url)["scheme"], "host": host_of(url), "todoist_host": is_todoist_url(url),
             "has_query": url_facts(url)["has_query"], "token_sent": token_sent}
    if answer is not None:
        facts["status"] = answer.status
    return facts


def fetch_chain(http, url: str, *, token: str | None, file_size=None, accept_encoding: str = "identity") -> dict:
    """Request a file and follow its redirects by hand, recording each hop's status and host.

    The token goes with the first request only, and only to a Todoist host (design D5). A hop to a
    Todoist host that answers 401 or 403 without the token is asked once more with it, to learn
    whether any hop needs it. A redirect to plain HTTP is recorded and not followed.
    """
    headers = {"Accept-Encoding": accept_encoding}
    first_token = token if token is not None and is_todoist_url(url) else None
    hops: list[dict] = []
    current, send_token = url, first_token
    while True:
        answer = http.request("GET", current, token=send_token, headers=headers)
        hop = hop_facts(current, answer, token_sent=send_token is not None)
        if send_token is None and token is not None and hops and is_todoist_url(current) and answer.status in (401, 403):
            hop["status_with_token"] = http.request("GET", current, token=token, headers=headers).status
        hops.append(hop)
        location = answer.header("Location")
        if not (300 <= answer.status < 400 and location):
            break
        current, send_token = urllib.parse.urljoin(current, location), None
        if urllib.parse.urlsplit(current).scheme.lower() != "https":
            hops.append(hop_facts(current, None, token_sent=False) | {"followed": False})
            return {"hops": hops, "ended": "redirect to a URL that is not https"}
        if len(hops) >= MAX_HOPS:
            return {"hops": hops, "ended": f"more than {MAX_HOPS - 1} redirects"}

    result = {"hops": hops, "ended": f"HTTP {answer.status}"}
    if 200 <= answer.status < 300:
        expected = file_size if isinstance(file_size, int) and not isinstance(file_size, bool) else None
        length = answer.header("Content-Length")
        stored = answer.decoded_size if answer.decoded_size is not None else answer.size
        result.update(
            content_length_present=length is not None,
            content_length_matches=None if expected is None or length is None else length.strip() == str(expected),
            content_encoding=enum_value((answer.header("Content-Encoding") or "none").lower()),
            content_type=enum_value((answer.header("Content-Type") or "none").split(";")[0].strip().lower()),
            bytes_match_file_size=None if expected is None else stored == expected,
            raw_bytes_match_file_size=None if expected is None else answer.size == expected,
            rate_limit_headers=sorted({k.lower() for k in answer.headers if any(w in k.lower() for w in RATE_LIMIT_WORDS)}),
        )
    else:
        result["rate_limit_headers"] = sorted(
            {k.lower() for k in answer.headers if any(w in k.lower() for w in RATE_LIMIT_WORDS)})
    return result


def probe_file(http, attachment: dict, token: str, *, try_gzip: bool = False) -> dict:
    """The facts of one uploaded file: requested without the token, with it, and optionally with gzip."""
    url, size = attachment["file_url"], attachment.get("file_size")
    facts = {"url": url_facts(url), "without_token": fetch_chain(http, url, token=None, file_size=size)}
    if is_todoist_url(url):
        facts["with_token"] = fetch_chain(http, url, token=token, file_size=size)
        if try_gzip:
            facts["with_token_accepting_gzip"] = fetch_chain(http, url, token=token, file_size=size,
                                                             accept_encoding="gzip")
    return facts


def pick_files(entries: list[tuple[str, dict]], limit: int = MAX_FILES) -> list[tuple[str, dict]]:
    """Up to `limit` uploads, one comment each, different `resource_type`s first."""
    seen_ids: set[str] = set()
    by_type: dict[str, list] = {}
    for comment, source in entries:
        attachment = comment.get("file_attachment")
        comment_id = str(comment.get("id"))
        if not is_upload(attachment) or comment_id in seen_ids:
            continue
        seen_ids.add(comment_id)
        by_type.setdefault(str(attachment.get("resource_type")), []).append((comment, source))
    picked: list = []
    while len(picked) < limit and any(by_type.values()):
        for queue in by_type.values():
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


# --- Runs --------------------------------------------------------------------


class Sections:
    """Runs each probe so that one failing probe does not stop the others.

    An API error is an answer and is recorded as such. Any other exception is recorded by its type
    as a crash (its message may hold data), and `crashed` lists the probes it happened in, so that
    the test can fail at the end.
    """

    def __init__(self):
        self.crashed: list[str] = []

    @contextmanager
    def run(self, findings: dict, name: str):
        entry = findings.setdefault(name, {})
        try:
            yield entry
        except ApiError as exc:
            entry["api_error"] = {"http_status": exc.http_status, "error_tag": exc.error_tag,
                                  "error_code": exc.error_code}
        except ProbeError as exc:
            entry["crashed"] = str(exc)
            self.crashed.append(name)
        except Exception as exc:  # noqa: BLE001 - recorded, and the test fails at the end
            entry["crashed"] = type(exc).__name__
            self.crashed.append(name)


def comments_of(client: Client, **params) -> list[dict]:
    comments: list[dict] = []
    for page in cursor_pages(client, COMMENTS, {k: str(v) for k, v in params.items()}, COMMENTS_PAGE_LIMIT):
        comments.extend(c for c in page.get("results") or [] if isinstance(c, dict))
    return comments


def comment_by_id(client: Client, comment_id: str) -> dict:
    return json.loads(client.get(f"{COMMENTS}/{urllib.parse.quote(str(comment_id), safe='')}", {}))


def _has_comments(task) -> bool:
    count = task.get("note_count") if isinstance(task, dict) else None
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def probe_read(client: Client, http, token: str, now: datetime, secrets: Secrets) -> tuple[dict, list[str]]:
    """R8: read-only. Return the findings and the probes that crashed."""
    findings: dict = {}
    sections = Sections()
    entries: list[tuple[dict, str]] = []

    with sections.run(findings, "sync") as entry:
        response = json.loads(client.sync("*"))
        for resource in ("notes", "project_notes"):
            notes = response.get(resource) or []
            secrets.add(notes)
            entry[resource] = comment_facts(notes)
            entries.extend((note, f"sync {resource}") for note in notes if isinstance(note, dict))
        entry["file_fields"] = file_fields(response)

    with sections.run(findings, "completed") as entry:
        month = completed.month_of(now)
        tasks: list[dict] = []
        for _ in range(SEARCH_MONTHS):
            since, until = completed.month_window(month)
            page = json.loads(client.get(completed.BY_COMPLETION_DATE, {"since": since, "until": until,
                                                                        "limit": "200"}))
            secrets.add(page)
            tasks.extend(t for t in page.get("items") or [] if _has_comments(t))
            month = completed.month_of(completed.month_start(month) - timedelta(days=1))
        notes = []
        for task in tasks[:MAX_TASKS]:
            notes.extend(comments_of(client, task_id=task["id"]))
        secrets.add(notes)
        entry.update(tasks_with_comments=len(tasks), tasks_read=min(len(tasks), MAX_TASKS), **comment_facts(notes))
        entries.extend((note, "completed") for note in notes)

    with sections.run(findings, "archived") as entry:
        page = json.loads(client.get(archived.ARCHIVED_PROJECTS, {"limit": "200"}))
        secrets.add(page)
        projects = [p for p in page.get("results") or [] if isinstance(p, dict) and "id" in p]
        notes, tasks_read = [], 0
        for project in projects[:MAX_ARCHIVED_PROJECTS]:
            notes.extend(comments_of(client, project_id=project["id"]))
            full = json.loads(client.get(archived.full_path(str(project["id"])), {}))
            secrets.add(full)
            for task in [t for t in full.get("tasks") or [] if _has_comments(t)]:
                if tasks_read >= MAX_TASKS:
                    break
                tasks_read += 1
                notes.extend(comments_of(client, task_id=task["id"]))
        secrets.add(notes)
        entry.update(projects=len(projects), projects_read=min(len(projects), MAX_ARCHIVED_PROJECTS),
                     tasks_read=tasks_read, **comment_facts(notes))
        entries.extend((note, "archived") for note in notes)

    with sections.run(findings, "files") as entry:
        picked = pick_files(entries)
        entry["uploads_found"] = len({str(c.get("id")) for c, _ in entries if is_upload(c.get("file_attachment"))})
        entry["probed"] = []
        for i, (comment, source) in enumerate(picked):
            attachment = comment["file_attachment"]
            facts = {"source": source, "resource_type": enum_value(attachment.get("resource_type")),
                     **probe_file(http, attachment, token, try_gzip=i == 0)}
            fresh = comment_by_id(client, comment["id"])
            secrets.add(fresh)
            fresh_attachment = fresh.get("file_attachment") if isinstance(fresh, dict) else None
            facts["fresh_comment"] = {
                "found": isinstance(fresh_attachment, dict),
                "file_url_equal": isinstance(fresh_attachment, dict)
                and fresh_attachment.get("file_url") == attachment.get("file_url"),
            }
            entry["probed"].append(facts)

    return findings, sections.crashed
