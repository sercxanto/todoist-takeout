"""Offline checks of the attachment probe helpers in tests/contract/probes.py.

The main check runs probe R8 against a fake API and fake file hosts whose payloads are full of
recognizable IDs, URL paths, query strings, file names and file contents, and asserts that none of
them reaches the findings.
"""

import gzip
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fake_api import FakeApi

from todoist_takeout import archived, completed
from todoist_takeout.api import Client

TOKEN = "secret-token-value"
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

# Values that must never appear in findings.
NOTE_ID = "6XSecretNote0001"
TASK_ID = "6XSecretTask0001"
PROJECT_ID = "6XSecretProj0001"
FILE_NAME = "Steuerbescheid geheim.pdf"
FILE_PATH = "/u/7781234/AbCdEfGhIjKl/Steuerbescheid%20geheim.pdf"
QUERY = "Expires=1760000000&Signature=c2VjcmV0c2lnbmF0dXJl"
CONTENT = b"contents of the secret file"
PAYLOAD_SECRETS = [NOTE_ID, TASK_ID, PROJECT_ID, FILE_NAME, FILE_PATH, QUERY, "c2VjcmV0c2lnbmF0dXJl", "AbCdEfGhIjKl",
                   "secret comment text"]
# Never in a payload: the token, a redirect target and the file's bytes.
OTHER_SECRETS = [TOKEN, "storage-path-0001", CONTENT.decode()]


class FakeHttp:
    """Answers file requests from a map of URL to a function of (token, headers) → Answer."""

    def __init__(self, probes, routes):
        self.probes = probes
        self.routes = routes
        self.requests = []

    def request(self, method, url, *, token=None, headers=None, data=None, keep_body=False):
        self.requests.append((method, url, token, dict(headers or {})))
        return self.routes[url](token, headers or {})


def answer(probes, status, headers=None, body=b""):
    result = probes.Answer(status, dict(headers or {}), size=len(body))
    if (result.header("Content-Encoding") or "").lower() == "gzip":
        result.decoded_size = len(gzip.decompress(body))
    return result


def redirect(probes, location):
    return lambda token, headers: answer(probes, 302, {"Location": location})


def attachment(url, **extra):
    return {"resource_type": "file", "file_name": FILE_NAME, "file_size": len(CONTENT), "file_type": "application/pdf",
            "file_url": url, "upload_state": "completed", **extra}


def note(note_id, file_attachment, **extra):
    return {"id": note_id, "item_id": TASK_ID, "content": "secret comment text", "file_attachment": file_attachment,
            "is_deleted": False, **extra}


@pytest.mark.parametrize("url", [
    "https://todoist.com/x", "https://files.todoist.com/x", "https://FILES.Todoist.COM/x",
    "https://files.todoist.com:443/x",
])
def test_todoist_urls(probes, url):
    assert probes.is_todoist_url(url)


@pytest.mark.parametrize("url", [
    "http://files.todoist.com/x", "https://todoist.com.example.net/x", "https://evil-todoist.com/x",
    "https://user@files.todoist.com/x", "https://files.todoist.com:8443/x", "files.todoist.com/x", None,
])
def test_other_urls(probes, url):
    assert not probes.is_todoist_url(url)


def test_url_facts_keep_host_and_query_hints_only(probes):
    facts = probes.url_facts(f"https://files.todoist.com{FILE_PATH}?{QUERY}")
    assert facts == {"scheme": "https", "host": "files.todoist.com", "todoist_host": True, "has_query": True,
                     "query_hints": {"expiry": True, "signature": True, "credential": False}}


def test_attachment_facts_keep_enumerations_and_types(probes):
    facts = probes.attachment_facts(attachment(f"https://files.todoist.com{FILE_PATH}", image="https://cdn.example.net/t"))
    assert facts["resource_type"] == "file" and facts["upload_state"] == "completed"
    assert facts["file_type"] == "application/pdf"
    assert facts["value_types"]["file_name"] == "string"
    assert facts["urls"]["image"]["host"] == "cdn.example.net"
    assert facts["file_size_is_count"] is True
    assert probes.enum_value("Steuerbescheid geheim.pdf") == "<string>"
    assert probes.attachment_facts(None) == {"type": "null"}


def test_token_goes_to_the_first_todoist_hop_only(probes):
    first = f"https://files.todoist.com{FILE_PATH}"
    storage = f"https://storage.example.net/storage-path-0001?{QUERY}"
    back = "https://files.todoist.com/again"
    http = FakeHttp(probes, {
        first: redirect(probes, storage),
        storage: redirect(probes, back),
        back: lambda token, headers: answer(probes, 200 if token else 403, {"Content-Length": "27"}, CONTENT),
    })
    chain = probes.fetch_chain(http, first, token=TOKEN, file_size=len(CONTENT))
    sent = [(url, token) for _, url, token, _ in http.requests]
    assert sent == [(first, TOKEN), (storage, None), (back, None), (back, TOKEN)]
    assert [hop["host"] for hop in chain["hops"]] == ["files.todoist.com", "storage.example.net", "files.todoist.com"]
    assert chain["hops"][2] | {} == {"scheme": "https", "host": "files.todoist.com", "todoist_host": True,
                                    "has_query": False, "token_sent": False, "status": 403, "status_with_token": 200}
    assert all(headers["Accept-Encoding"] == "identity" for *_, headers in http.requests)


def test_no_token_to_a_foreign_first_host(probes):
    url = "https://cdn.example.net/file"
    http = FakeHttp(probes, {url: lambda token, headers: answer(probes, 200, {}, CONTENT)})
    chain = probes.fetch_chain(http, url, token=TOKEN, file_size=len(CONTENT))
    assert http.requests[0][2] is None
    assert chain["bytes_match_file_size"] is True and chain["content_length_present"] is False


def test_redirect_to_plain_http_is_not_followed(probes):
    first, plain = "https://files.todoist.com/a", "http://files.todoist.com/a"
    http = FakeHttp(probes, {first: redirect(probes, plain)})
    chain = probes.fetch_chain(http, first, token=TOKEN)
    assert len(http.requests) == 1
    assert chain["ended"] == "redirect to a URL that is not https" and chain["hops"][1]["followed"] is False


def test_redirect_chains_end_after_the_hop_limit(probes):
    urls = [f"https://files.todoist.com/{i}" for i in range(10)]
    http = FakeHttp(probes, {url: redirect(probes, urls[i + 1]) for i, url in enumerate(urls[:-1])})
    chain = probes.fetch_chain(http, urls[0], token=TOKEN)
    assert len(http.requests) == probes.MAX_HOPS
    assert chain["ended"] == f"more than {probes.MAX_HOPS - 1} redirects"


def test_gzip_bodies_are_compared_after_decoding(probes):
    url = "https://files.todoist.com/z"
    body = gzip.compress(CONTENT)
    http = FakeHttp(probes, {url: lambda token, headers: answer(
        probes, 200, {"Content-Encoding": "gzip", "Content-Type": "application/pdf; x=y", "X-RateLimit-Remaining": "9"},
        body)})
    chain = probes.fetch_chain(http, url, token=TOKEN, file_size=len(CONTENT), accept_encoding="gzip")
    assert chain["content_encoding"] == "gzip" and chain["content_type"] == "application/pdf"
    assert chain["bytes_match_file_size"] is True and chain["raw_bytes_match_file_size"] is False
    assert chain["rate_limit_headers"] == ["x-ratelimit-remaining"]


def test_check_findings_refuses_payload_values_and_odd_strings(probes):
    secrets = probes.Secrets()
    secrets.add(note(NOTE_ID, attachment(f"https://files.todoist.com{FILE_PATH}?{QUERY}")))
    assert probes.check_findings({"host": "files.todoist.com", "type": "image/png", "n": 3, "ok": True}, secrets) == []
    for leaked in (NOTE_ID, FILE_NAME, FILE_PATH, "c2VjcmV0c2lnbmF0dXJl", f"x {NOTE_ID}"):
        assert probes.check_findings({"value": leaked}, secrets), leaked
    assert probes.check_findings({NOTE_ID: 1}, secrets)
    assert probes.check_findings({"value": "line\nbreak"}, secrets)
    assert probes.check_findings({"value": b"bytes"}, secrets)


def test_r8_findings_hold_no_account_data(probes):
    upload_url = f"https://files.todoist.com{FILE_PATH}?{QUERY}"
    storage = "https://storage.example.net/storage-path-0001?sig=AbCdEfGhIjKl"
    image_url = "https://files.todoist.com/u/7781234/AbCdEfGhIjKl/image.png"
    link = {"resource_type": "url", "url": "https://example.com/secret comment text", "title": FILE_NAME}
    notes = [
        note(NOTE_ID, attachment(upload_url)),
        note("6XSecretNote0002", None),
        note("6XSecretNote0003", link),
        note("6XSecretNote0004", attachment(image_url, resource_type="image", upload_state="pending")),
    ]
    sync = {"sync_token": "t", "notes": notes, "project_notes": [],
            "user": {"id": "7781234", "avatar_big": "https://dcff.cloudfront.net/7781234-big.jpg", "image_id": "AbCdEfGhIjKl"},
            "day_orders": {TASK_ID: 3}}
    api = FakeApi({"*": json.dumps(sync).encode()})
    since, until = completed.month_window(completed.month_of(NOW))
    api.answer(completed.BY_COMPLETION_DATE, {"since": since},
               {"items": [{"id": TASK_ID, "note_count": 1}, {"id": "6XSecretTask0002", "note_count": 0}]})
    api.cursor_pages(completed.COMMENTS, {"task_id": TASK_ID},
                     [{"results": [note("6XSecretNote0005", attachment(upload_url))], "next_cursor": None}])
    api.cursor_pages(archived.ARCHIVED_PROJECTS, {}, [{"results": [{"id": PROJECT_ID}], "next_cursor": None}])
    api.answer(archived.full_path(PROJECT_ID), {}, {"tasks": [{"id": "6XSecretTask0003", "note_count": 2}]})
    api.answer(f"comments/{NOTE_ID}", {}, note(NOTE_ID, attachment(f"https://files.todoist.com{FILE_PATH}?other=1")))
    api.answer("comments/6XSecretNote0005", {}, note("6XSecretNote0005", attachment(upload_url)))
    http = FakeHttp(probes, {
        upload_url: lambda token, headers: (answer(probes, 302, {"Location": storage}) if token
                                            else answer(probes, 401)),
        storage: lambda token, headers: answer(probes, 200, {"Content-Length": str(len(CONTENT))}, CONTENT),
    })
    secrets = probes.Secrets()
    client = Client(api, sleep=lambda delay: None, jitter=lambda: 0.0)

    findings, crashed = probes.probe_read(client, http, TOKEN, NOW, secrets)

    assert crashed == []
    assert probes.check_findings(findings, secrets) == []
    text = json.dumps(findings)
    for secret in PAYLOAD_SECRETS + OTHER_SECRETS:
        assert secret not in text, secret
    for secret in PAYLOAD_SECRETS:
        assert any(secret in value for value in secrets.values), secret

    assert findings["sync"]["notes"]["comments"] == 4 and findings["sync"]["notes"]["null"] == 1
    assert "user: image_id" in findings["sync"]["file_fields"]
    assert findings["completed"]["tasks_with_comments"] == 1 and findings["completed"]["comments"] == 1
    assert findings["archived"]["projects"] == 1 and findings["archived"]["tasks_read"] == 1
    files = findings["files"]
    assert files["uploads_found"] == 2 and len(files["probed"]) == 2
    probed, second = files["probed"]
    assert [p["source"] for p in files["probed"]] == ["sync notes", "completed"]
    assert "with_token_accepting_gzip" in probed and "with_token_accepting_gzip" not in second
    assert probed["without_token"]["ended"] == "HTTP 401"
    assert probed["with_token"]["ended"] == "HTTP 200"
    assert [hop["host"] for hop in probed["with_token"]["hops"]] == ["files.todoist.com", "storage.example.net"]
    assert probed["with_token"]["bytes_match_file_size"] is True
    assert probed["fresh_comment"] == {"found": True, "file_url_equal": False}
    assert second["fresh_comment"] == {"found": True, "file_url_equal": True}
    assert all(token in (None, TOKEN) for _, url, token, _ in http.requests)
    assert not any(token for _, url, token, _ in http.requests if url == storage)


def test_multipart_body(probes):
    body, header = probes.multipart({"file_name": "a.txt"}, "file", "a.txt", b"abc", "text/plain")
    boundary = header.split("boundary=")[1]
    assert header.startswith("multipart/form-data; ")
    assert body.startswith(f"--{boundary}\r\n".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="file"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nabc\r\n' in body


def test_raw_http_measures_bodies_and_leaves_no_file(probes, tmp_path):
    body = gzip.compress(CONTENT)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the name http.server calls
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = probes.RawHttp(tmp_path).request("GET", f"http://127.0.0.1:{server.server_port}/f")
    finally:
        server.shutdown()
        server.server_close()
    assert result.status == 200 and result.size == len(body) and result.decoded_size == len(CONTENT)
    assert result.body is None
    assert list(tmp_path.iterdir()) == []
