import email.message
import gzip
import http.client
import io
import json
import logging
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from todoist_takeout import __version__, api, storage
from todoist_takeout.errors import (
    ApiError,
    AttachmentRefused,
    AttachmentUnusable,
    AuthError,
    NetworkError,
    PlanRestricted,
    RateLimitExhausted,
    SizeMismatch,
    UnexpectedHost,
)

TOKEN = "0123456789abcdef0123456789abcdef01234567"


# --- 3.1 urllib transport against a local server ------------------------------


class _Handler(BaseHTTPRequestHandler):
    # Set per test: (status, headers, body) for the next response.
    response = (200, {}, b"{}")
    received = []

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
        status, headers, body = type(self).response
        headers = dict(headers)
        # Announce more bytes than are sent, so that the client sees the body cut off.
        missing = int(headers.pop("X-Test-Truncate", 0))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body) + missing))
        if missing:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if missing:
            self.close_connection = True

    def log_message(self, format, *args):
        pass


@pytest.fixture
def server():
    _Handler.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/"
    httpd.shutdown()
    httpd.server_close()


def test_transport_sends_headers_and_form(server, caplog):
    httpd, base_url = server
    _Handler.response = (200, {"Content-Type": "application/json"}, b'{"ok": true}')
    transport = api.UrllibTransport(TOKEN, base_url=base_url)

    with caplog.at_level(logging.DEBUG):
        status, _headers, body = transport.send("POST", "sync", {"sync_token": "*", "resource_types": '["all"]'})

    assert (status, body) == (200, b'{"ok": true}')
    [(method, path, headers, raw)] = _Handler.received
    assert (method, path) == ("POST", "/api/v1/sync")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept-Encoding"] == "gzip"
    assert headers["User-Agent"] == f"todoist-takeout/{__version__}"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(raw.decode("ascii")) == {"sync_token": ["*"], "resource_types": ['["all"]']}
    assert any("POST /api/v1/sync" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert TOKEN not in repr(transport)


def test_transport_sends_get_parameters_as_query(server, caplog):
    _httpd, base_url = server
    _Handler.response = (200, {"Content-Type": "application/json"}, b'{"items": []}')
    transport = api.UrllibTransport(TOKEN, base_url=base_url)
    params = {"since": "2026-09-01T00:00:00.000000Z", "object_event_types": '["item:completed"]'}

    with caplog.at_level(logging.DEBUG):
        status, _headers, body = transport.send("GET", "tasks/completed/by_completion_date", params)

    assert (status, body) == (200, b'{"items": []}')
    [(method, path, headers, raw)] = _Handler.received
    parts = urllib.parse.urlsplit(path)
    assert (method, parts.path) == ("GET", "/api/v1/tasks/completed/by_completion_date")
    assert urllib.parse.parse_qs(parts.query) == {key: [value] for key, value in params.items()}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert raw == b""
    assert "Content-Length" not in headers and "Content-Type" not in headers
    messages = [r.getMessage() for r in caplog.records]
    assert "GET /api/v1/tasks/completed/by_completion_date" in messages
    assert all("?" not in m and "2026-09" not in m for m in messages)


def test_transport_keeps_a_query_already_in_the_path(server):
    _httpd, base_url = server
    api.UrllibTransport(TOKEN, base_url=base_url).send("GET", "comments?task_id=t1", {"limit": "100"})
    [(_method, path, _headers, _raw)] = _Handler.received
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(path).query) == {"task_id": ["t1"], "limit": ["100"]}


def test_get_errors_name_the_method_without_the_query(server):
    _httpd, base_url = server
    _Handler.response = _error(400, tag="INVALID_ARGUMENT_VALUE", text="Invalid argument value")
    client = api.Client(api.UrllibTransport(TOKEN, base_url=base_url), sleep=lambda s: None)
    with pytest.raises(ApiError) as info:
        client.get("tasks/completed/by_completion_date", {"since": "2026-09-01T00:00:00.000000Z"})
    assert str(info.value).startswith("GET /tasks/completed/by_completion_date failed: Invalid argument value")
    assert "2026-09" not in str(info.value)


def test_transport_decodes_gzip(server):
    _httpd, base_url = server
    payload = json.dumps({"content": "Größe 🍎"}, ensure_ascii=False).encode("utf-8")
    _Handler.response = (200, {"Content-Encoding": "gzip"}, gzip.compress(payload))
    _status, _headers, body = api.UrllibTransport(TOKEN, base_url=base_url).send("POST", "sync", {})
    assert body == payload


def corrupt_gzip(payload: bytes) -> bytes:
    data = bytearray(gzip.compress(payload))
    data[10] = 0xFF  # the first deflate block gets a reserved type: gzip.decompress raises zlib.error
    return bytes(data)


def test_transport_wraps_a_corrupt_gzip_body(server):
    _httpd, base_url = server
    _Handler.response = (200, {"Content-Encoding": "gzip"}, corrupt_gzip(b'{"items": []}' * 20))
    with pytest.raises(api.TransportError) as info:
        api.UrllibTransport(TOKEN, base_url=base_url).send("POST", "sync", {})
    assert info.value.retryable


def test_transport_wraps_a_cut_off_error_body(server):
    _httpd, base_url = server
    _Handler.response = (503, {"X-Test-Truncate": "100"}, b'{"error": "Service unav')
    with pytest.raises(api.TransportError) as info:
        api.UrllibTransport(TOKEN, base_url=base_url).send("POST", "sync", {})
    assert info.value.retryable and "HTTP 503" in str(info.value)


def test_corrupt_responses_are_retried_until_the_last_attempt(server):
    _httpd, base_url = server
    _Handler.response = (200, {"Content-Encoding": "gzip"}, corrupt_gzip(b'{"items": []}' * 20))
    client = api.Client(api.UrllibTransport(TOKEN, base_url=base_url), sleep=lambda delay: None, jitter=lambda: 0.0)
    with pytest.raises(NetworkError, match="gave up after 5 attempts"):
        client.sync("*")
    assert len(_Handler.received) == api.MAX_ATTEMPTS


def test_transport_returns_error_statuses(server):
    _httpd, base_url = server
    error = b'{"error": "Invalid argument value", "error_tag": "INVALID_ARGUMENT_VALUE"}'
    _Handler.response = (400, {"Content-Encoding": "gzip"}, gzip.compress(error))
    status, _headers, body = api.UrllibTransport(TOKEN, base_url=base_url).send("POST", "sync", {})
    assert (status, body) == (400, error)


def test_transport_does_not_follow_redirects(server):
    _httpd, base_url = server
    _Handler.response = (302, {"Location": "http://example.invalid/steal"}, b"")
    status, _headers, _body = api.UrllibTransport(TOKEN, base_url=base_url).send("POST", "sync", {})
    assert status == 302
    assert len(_Handler.received) == 1


def test_transport_wraps_connection_errors(server):
    httpd, base_url = server
    httpd.shutdown()
    httpd.server_close()
    with pytest.raises(api.TransportError) as info:
        api.UrllibTransport(TOKEN, base_url=base_url, timeout=5).send("POST", "sync", {})
    assert info.value.retryable
    assert TOKEN not in str(info.value)


def test_transport_uses_verified_tls_by_default():
    transport = api.UrllibTransport(TOKEN)
    [handler] = [h for h in transport._opener.handlers if isinstance(h, api.urllib.request.HTTPSHandler)]
    context = handler._context
    assert context.verify_mode == api.ssl.CERT_REQUIRED
    assert context.check_hostname
    assert transport._base_url == "https://api.todoist.com/api/v1/"
    assert transport._timeout == 300


# --- 3.2 retries and rate limits with a scripted transport --------------------


class FakeTransport:
    """Replays a script of responses; an exception in the script is raised instead."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def send(self, method, path, form):
        self.requests.append((method, path, dict(form)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _error(status, tag=None, code=None, text="Something failed", extra=None, headers=None):
    body = {"error": text, "error_tag": tag, "error_code": code, "http_code": status, "error_extra": extra or {}}
    return status, headers or {}, json.dumps(body).encode()


OK = (200, {}, b'{"sync_token": "t1"}')


def _client(transport, **kwargs):
    sleeps = []
    client = api.Client(transport, sleep=sleeps.append, jitter=lambda: 0.0, **kwargs)
    return client, sleeps


def test_429_waits_for_retry_after_in_body(caplog):
    transport = FakeTransport(_error(429, extra={"retry_after": 30}, headers={"Retry-After": "5"}), OK)
    client, sleeps = _client(transport)
    with caplog.at_level(logging.INFO):
        assert client.post("sync", {}) == OK[2]
    assert sleeps == [30]
    assert len(transport.requests) == 2
    assert transport.requests[0] == transport.requests[1]
    assert any("rate-limited" in r.getMessage() for r in caplog.records)


def test_429_falls_back_to_retry_after_header():
    transport = FakeTransport(_error(429, headers={"retry-after": "12"}), OK)
    client, sleeps = _client(transport)
    client.post("sync", {})
    assert sleeps == [12]


def test_429_falls_back_to_default_wait():
    transport = FakeTransport((429, {}, b"Too Many Requests"), OK)
    client, sleeps = _client(transport)
    client.post("sync", {})
    assert sleeps == [api.DEFAULT_RATE_LIMIT_WAIT]


def test_429_adds_jitter():
    transport = FakeTransport(_error(429, extra={"retry_after": 30}), OK)
    sleeps = []
    api.Client(transport, sleep=sleeps.append, jitter=lambda: 0.5).post("sync", {})
    assert sleeps == [30.5]


def test_429_wait_cap_raises_rate_limit_exhausted():
    transport = FakeTransport(*[_error(429, extra={"retry_after": 400})] * 3)
    client, sleeps = _client(transport, max_rate_limit_wait=900)
    with pytest.raises(RateLimitExhausted) as info:
        client.post("sync", {})
    assert sleeps == [400, 400]  # a third wait would reach 1200 s
    assert "rate-limit" in str(info.value)
    assert client.rate_limit_waited == 800


def test_429_cap_is_shared_across_requests():
    transport = FakeTransport(_error(429, extra={"retry_after": 500}), OK, _error(429, extra={"retry_after": 500}))
    client, _sleeps = _client(transport, max_rate_limit_wait=900)
    client.post("sync", {})
    with pytest.raises(RateLimitExhausted):
        client.post("sync", {})


def test_5xx_is_retried_with_backoff():
    transport = FakeTransport((503, {}, b""), OK)
    client, sleeps = _client(transport)
    assert client.post("sync", {}) == OK[2]
    assert sleeps == [1]


def test_network_errors_retry_five_times_then_fail():
    transport = FakeTransport(*[api.TransportError("cannot reach api.todoist.com: refused")] * 5)
    client, sleeps = _client(transport)
    with pytest.raises(NetworkError) as info:
        client.post("sync", {})
    assert sleeps == [1, 2, 4, 8]
    assert len(transport.requests) == 5
    assert "refused" in str(info.value)


def test_network_error_then_success():
    transport = FakeTransport(api.TransportError("timed out"), api.TransportError("timed out"), OK)
    client, sleeps = _client(transport)
    assert client.post("sync", {}) == OK[2]
    assert sleeps == [1, 2]


def test_persistent_5xx_raises_api_error():
    transport = FakeTransport(*[_error(502, text="Bad gateway")] * 5)
    client, _sleeps = _client(transport)
    with pytest.raises(ApiError) as info:
        client.post("sync", {})
    assert info.value.http_status == 502
    assert len(transport.requests) == 5


def test_certificate_errors_are_not_retried():
    transport = FakeTransport(api.TransportError("cannot verify the TLS certificate", retryable=False))
    client, sleeps = _client(transport)
    with pytest.raises(NetworkError):
        client.post("sync", {})
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 403, 404, 410])
def test_other_4xx_are_not_retried(status):
    transport = FakeTransport(_error(status))
    client, sleeps = _client(transport)
    with pytest.raises(ApiError):
        client.post("sync", {})
    assert sleeps == []
    assert len(transport.requests) == 1


# --- 3.3 error classification -------------------------------------------------


def _raised(response):
    with pytest.raises(ApiError) as info:
        _client(FakeTransport(response))[0].post("sync", {})
    return info.value


def test_401_is_auth_error():
    error = _raised(_error(401, tag="UNAUTHORIZED", code=477, text="Unauthorized"))
    assert type(error) is AuthError
    assert "TODOIST_API_TOKEN" in str(error)
    assert "Unauthorized" in str(error)


def test_401_without_json_body_is_auth_error():
    error = _raised((401, {}, b"Unauthorized"))
    assert type(error) is AuthError
    assert "TODOIST_API_TOKEN" in str(error)


@pytest.mark.parametrize("tag", ["AUTH_INVALID_TOKEN", "AUTHENTICATION_ERROR", "UNAUTHORIZED"])
def test_403_with_auth_tag_is_auth_error(tag):
    assert type(_raised(_error(403, tag=tag))) is AuthError


def test_403_with_plan_tag_is_plan_restricted():
    error = _raised(_error(403, tag="PREMIUM_ONLY", code=32, text="Premium only feature"))
    assert type(error) is PlanRestricted
    assert error.error_tag == "PREMIUM_ONLY"


def test_other_403_is_plain_api_error():
    assert type(_raised(_error(403, tag="FORBIDDEN", text="Forbidden"))) is ApiError


def test_api_error_carries_and_reports_fields():
    error = _raised(_error(400, tag="INVALID_ARGUMENT_VALUE", code=20, text="Invalid argument value"))
    assert type(error) is ApiError
    assert (error.http_status, error.error, error.error_tag, error.error_code) == (
        400,
        "Invalid argument value",
        "INVALID_ARGUMENT_VALUE",
        20,
    )
    for part in ["Invalid argument value", "INVALID_ARGUMENT_VALUE", "20", "HTTP 400"]:
        assert part in str(error)


def test_error_messages_do_not_contain_the_token(server):
    _httpd, base_url = server
    for status, tag in [(401, "UNAUTHORIZED"), (403, "AUTH_INVALID_TOKEN"), (403, "PREMIUM_ONLY"), (400, None)]:
        _Handler.response = _error(status, tag=tag)
        client = api.Client(api.UrllibTransport(TOKEN, base_url=base_url), sleep=lambda s: None)
        with pytest.raises(ApiError) as info:
            client.sync("*")
        assert TOKEN not in str(info.value)


def test_get_is_sent_as_get_and_retried_like_post():
    transport = FakeTransport((503, {}, b""), _error(429, extra={"retry_after": 3}), OK)
    client, sleeps = _client(transport)
    assert client.get("activities", {"limit": "100"}) == OK[2]
    assert sleeps == [1, 3]
    assert transport.requests == [("GET", "activities", {"limit": "100"})] * 3


def test_rate_limit_message_names_the_method():
    transport = FakeTransport(_error(429, extra={"retry_after": 400}))
    client, _sleeps = _client(transport, max_rate_limit_wait=100)
    with pytest.raises(RateLimitExhausted, match=r"rate-limiting GET /activities;"):
        client.get("activities", {})


def test_post_errors_still_name_post():
    assert str(_raised(_error(400))).startswith("POST /sync failed:")


# --- 3.4 sync call -------------------------------------------------------------


def test_sync_posts_token_and_all_resource_types_and_returns_body_unchanged():
    body = b'{"sync_token":"abc",  "full_sync": true, "items": [{"id": "1", "n": 1.0}]}'
    transport = FakeTransport((200, {}, body))
    client, _sleeps = _client(transport)
    assert client.sync("prev-token") is body
    assert transport.requests == [("POST", "sync", {"sync_token": "prev-token", "resource_types": '["all"]'})]


# --- pagination -----------------------------------------------------------------


def _page(**body):
    return 200, {}, json.dumps(body).encode()


def _cursor_walk(*pages, params=None):
    transport = FakeTransport(*pages)
    client, _sleeps = _client(transport)
    return list(api.cursor_pages(client, "activities", params or {"x": "1"}, 100)), transport.requests


def test_cursor_pages_single_page_without_cursor():
    pages, requests = _cursor_walk(_page(results=[{"id": 1}]))
    assert pages == [{"results": [{"id": 1}]}]
    assert requests == [("GET", "activities", {"x": "1", "limit": "100"})]


def test_cursor_pages_follows_next_cursor():
    pages, requests = _cursor_walk(
        _page(results=[{"id": 1}], next_cursor="c1"),
        _page(results=[{"id": 2}], next_cursor="c2"),
        _page(results=[{"id": 3}]),
    )
    assert [p["results"][0]["id"] for p in pages] == [1, 2, 3]
    assert [r[2].get("cursor") for r in requests] == [None, "c1", "c2"]
    assert all(r[2]["x"] == "1" and r[2]["limit"] == "100" for r in requests)


def test_cursor_pages_stops_at_null_cursor():
    pages, requests = _cursor_walk(_page(results=[], next_cursor="c1"), _page(results=[], next_cursor=None))
    assert len(pages) == len(requests) == 2


def test_cursor_pages_rejects_a_repeated_cursor():
    transport = FakeTransport(
        _page(results=[], next_cursor="c1"), _page(results=[], next_cursor="c2"), _page(results=[], next_cursor="c1")
    )
    client, _sleeps = _client(transport)
    with pytest.raises(ApiError, match="cursor 'c1' again"):
        list(api.cursor_pages(client, "activities", {}, 100))
    assert len(transport.requests) == 3


def test_pages_must_be_json_objects():
    client, _sleeps = _client(FakeTransport((200, {}, b"[]")))
    with pytest.raises(ApiError, match="not a JSON object"):
        list(api.cursor_pages(client, "activities", {}, 100))


def test_offset_pages_ends_on_a_short_page():
    transport = FakeTransport(_page(items=[{"id": "a"}, {"id": "b"}]), _page(items=[{"id": "c"}], projects={}))
    client, _sleeps = _client(transport)
    pages = list(api.offset_pages(client, "tasks/completed", {"annotate_items": "true"}, 2))
    assert [len(p["items"]) for p in pages] == [2, 1]
    assert [r[2] for r in transport.requests] == [
        {"annotate_items": "true", "limit": "2", "offset": "0"},
        {"annotate_items": "true", "limit": "2", "offset": "2"},
    ]


def test_offset_pages_ends_after_a_full_page_followed_by_an_empty_one():
    transport = FakeTransport(
        _page(items=[{"id": "a"}, {"id": "b"}]), _page(items=[{"id": "c"}, {"id": "d"}]), _page(items=[])
    )
    client, _sleeps = _client(transport)
    pages = list(api.offset_pages(client, "tasks/completed", {}, 2))
    assert [len(p["items"]) for p in pages] == [2, 2, 0]
    assert [r[2]["offset"] for r in transport.requests] == ["0", "2", "4"]


def test_offset_pages_requires_the_list_key():
    client, _sleeps = _client(FakeTransport(_page(results=[])))
    with pytest.raises(ApiError, match="'items'"):
        list(api.offset_pages(client, "tasks/completed", {}, 2))


def test_pages_keep_large_integers_exact():
    event_id = 2**127 + 12345
    pages, _requests = _cursor_walk((200, {}, f'{{"results": [{{"id": {event_id}}}]}}'.encode()))
    assert pages[0]["results"][0]["id"] == event_id


# --- Attachment files: host check (add-attachment-downloads 3.1) ---------------


@pytest.mark.parametrize(
    "url",
    [
        "https://todoist.com/file.pdf",
        "https://files.todoist.com/a/b/file.pdf",
        "https://FILES.Todoist.COM/a/b/file.pdf",
        "https://files.todoist.com:443/a/b/file.pdf",
        "https://a.b.todoist.com/x",
    ],
)
def test_todoist_file_urls_are_allowed(url):
    assert api.is_todoist_file_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://files.todoist.com/a/b/file.pdf",
        "https://todoist.com.example.net/file.pdf",
        "https://evil-todoist.com/file.pdf",
        "https://user@files.todoist.com/file.pdf",
        "https://user:secret@files.todoist.com/file.pdf",
        "https://files.todoist.com:8443/file.pdf",
        "https://files.todoist.com:notaport/file.pdf",
        "https://.todoist.com/file.pdf",
        "https://files.todoist.com./file.pdf",
        "https://files.todoist.com\\@example.net/file.pdf",
        "https://example.com/files.todoist.com/file.pdf",
        "https://example.com/?u=https://files.todoist.com/",
        "ftp://files.todoist.com/file.pdf",
        "//files.todoist.com/file.pdf",
        "files.todoist.com/file.pdf",
        "",
        None,
        42,
    ],
)
def test_other_urls_are_rejected(url):
    assert not api.is_todoist_file_url(url)


# --- Attachment files: transport (add-attachment-downloads 3.2) ----------------

FILE_URL = "https://files.todoist.com/app/n1/preise.pdf"
SIGNED_URL = "https://d1abc.cloudfront.net/app/n1/preise.pdf?Expires=1&Signature=s3cr3t&Key-Pair-Id=K"


class _StubResponse(io.BytesIO):
    def __init__(self, status, headers, body, fail_after=None):
        super().__init__(body)
        self.status = status
        self.headers = headers
        self._fail_after = fail_after

    def read(self, size=-1):
        if self._fail_after is not None and self.tell() >= self._fail_after:
            raise TimeoutError("timed out")
        return super().read(size)


class _StubOpener:
    """Answers like the transport's real opener, which never follows redirects: non-2xx raise HTTPError."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, request.get_header("Authorization"), request.get_header("Accept-encoding")))
        route = self.routes[request.full_url]
        if isinstance(route, Exception):
            raise route
        status, headers, body, *fail_after = route
        message = email.message.Message()
        for name, value in headers.items():
            message[name] = value
        if 200 <= status < 300:
            return _StubResponse(status, message, body, *fail_after)
        raise urllib.error.HTTPError(request.full_url, status, "stub", message, io.BytesIO(body))


def _stub_transport(routes):
    transport = api.UrllibTransport(TOKEN)
    transport._opener = _StubOpener(routes)
    return transport, transport._opener


def _collect(chunks):
    return b"".join(chunks)


def test_download_sends_the_token_to_a_todoist_host():
    transport, opener = _stub_transport({FILE_URL: (200, {}, b"%PDF-1.7")})
    status, _headers, body = transport.download(FILE_URL, _collect)
    assert (status, body) == (200, b"%PDF-1.7")
    assert opener.requests == [(FILE_URL, f"Bearer {TOKEN}", "identity")]


def test_download_follows_a_redirect_to_a_storage_host_without_the_token():
    transport, opener = _stub_transport(
        {
            FILE_URL: (302, {"Location": SIGNED_URL}, b""),
            SIGNED_URL: (200, {"Content-Length": "8"}, b"%PDF-1.7"),
        }
    )
    assert transport.download(FILE_URL, _collect)[2] == b"%PDF-1.7"
    assert opener.requests == [(FILE_URL, f"Bearer {TOKEN}", "identity"), (SIGNED_URL, None, "identity")]


def test_download_sends_no_token_on_a_redirect_back_to_a_todoist_host():
    other = "https://files.todoist.com/app/n1/other.pdf"
    transport, opener = _stub_transport({FILE_URL: (301, {"Location": other}, b""), other: (200, {}, b"x")})
    transport.download(FILE_URL, _collect)
    assert [auth for _url, auth, _enc in opener.requests] == [f"Bearer {TOKEN}", None]


def test_download_resolves_a_relative_location():
    transport, opener = _stub_transport(
        {FILE_URL: (307, {"Location": "/moved/preise.pdf"}, b""), "https://files.todoist.com/moved/preise.pdf": (200, {}, b"x")}
    )
    assert transport.download(FILE_URL, _collect)[2] == b"x"
    assert opener.requests[1][:2] == ("https://files.todoist.com/moved/preise.pdf", None)


@pytest.mark.parametrize("location", ["http://storage.example.net/preise.pdf", "ftp://files.todoist.com/x", "http://files.todoist.com/x"])
def test_download_does_not_follow_a_redirect_that_is_not_https(location):
    transport, opener = _stub_transport({FILE_URL: (302, {"Location": location}, b"")})
    with pytest.raises(api.RedirectNotFollowed, match="not HTTPS") as info:
        transport.download(FILE_URL, _collect)
    assert info.value.http_status == 302
    assert len(opener.requests) == 1


def _redirect_chain(hops):
    urls = [FILE_URL] + [f"https://hop{i}.example.net/f" for i in range(1, hops + 1)]
    routes = {url: (302, {"Location": nxt}, b"") for url, nxt in zip(urls, urls[1:])}
    routes[urls[-1]] = (200, {}, b"done")
    return routes


def test_download_follows_five_redirects():
    transport, opener = _stub_transport(_redirect_chain(5))
    assert transport.download(FILE_URL, _collect)[2] == b"done"
    assert len(opener.requests) == 6
    assert [auth for _url, auth, _enc in opener.requests] == [f"Bearer {TOKEN}"] + [None] * 5


def test_download_stops_at_the_sixth_redirect():
    transport, opener = _stub_transport(_redirect_chain(6))
    with pytest.raises(api.RedirectNotFollowed, match="after 5 redirects"):
        transport.download(FILE_URL, _collect)
    assert len(opener.requests) == 6


@pytest.mark.parametrize("status, headers", [(302, {}), (300, {"Location": SIGNED_URL}), (304, {})])
def test_download_does_not_follow_other_3xx(status, headers):
    transport, opener = _stub_transport({FILE_URL: (status, headers, b"")})
    with pytest.raises(api.RedirectNotFollowed):
        transport.download(FILE_URL, _collect)
    assert len(opener.requests) == 1


def test_download_sends_no_token_to_a_foreign_first_url():
    url = "https://example.com/preise.pdf"
    transport, opener = _stub_transport({url: (200, {}, b"x")})
    transport.download(url, _collect)
    assert opener.requests == [(url, None, "identity")]


def test_download_returns_error_statuses_with_the_start_of_the_body():
    transport, _opener = _stub_transport({FILE_URL: (404, {}, b"Not Found")})
    status, _headers, body = transport.download(FILE_URL, lambda chunks: pytest.fail("write called"))
    assert (status, body) == (404, b"Not Found")


def test_download_streams_in_blocks():
    body = bytes(range(256)) * 1024  # 256 KiB
    transport, _opener = _stub_transport({FILE_URL: (200, {}, body)})
    sizes = []

    def write(chunks):
        data = b""
        for chunk in chunks:
            sizes.append(len(chunk))
            data += chunk
        return data

    assert transport.download(FILE_URL, write)[2] == body
    assert sizes == [api.CHUNK_SIZE] * 4


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "GZIP"])
def test_download_decodes_gzip_sent_anyway(encoding):
    payload = "Größe 🍎\n".encode("utf-8") * 10000
    transport, _opener = _stub_transport({FILE_URL: (200, {"Content-Encoding": encoding}, gzip.compress(payload))})
    assert transport.download(FILE_URL, _collect)[2] == payload


def test_download_rejects_a_cut_off_gzip_body():
    data = gzip.compress(b"x" * 100000)
    transport, _opener = _stub_transport({FILE_URL: (200, {"Content-Encoding": "gzip"}, data[: len(data) // 2])})
    with pytest.raises(api.TransportError, match="cut off") as info:
        transport.download(FILE_URL, _collect)
    assert info.value.retryable


def test_download_rejects_a_body_shorter_than_its_content_length():
    transport, _opener = _stub_transport({FILE_URL: (200, {"Content-Length": "20480"}, b"x" * 10240)})
    with pytest.raises(api.TransportError, match="cut off after 10240 of 20480 bytes") as info:
        transport.download(FILE_URL, _collect)
    assert info.value.retryable


def test_download_rejects_an_unsupported_encoding_as_unusable():
    transport, _opener = _stub_transport({FILE_URL: (200, {"Content-Encoding": "br"}, b"...")})
    with pytest.raises(api.FileUnusable, match="'br'"):
        transport.download(FILE_URL, _collect)


@pytest.mark.parametrize("chunk_size", [api.CHUNK_SIZE, "first member"])
def test_download_decodes_every_gzip_member(monkeypatch, chunk_size):
    first, second = gzip.compress(b"first part, "), gzip.compress(b"second part" * 10000)
    if chunk_size == "first member":
        monkeypatch.setattr(api, "CHUNK_SIZE", len(first))  # a block ends exactly where the member ends
    transport, _opener = _stub_transport({FILE_URL: (200, {"Content-Encoding": "gzip"}, first + second)})
    assert transport.download(FILE_URL, _collect)[2] == b"first part, " + b"second part" * 10000


@pytest.mark.parametrize(
    "url, sent",
    [
        ("https://files.todoist.com/app/n1/Rechnung März 2026.pdf",
         "https://files.todoist.com/app/n1/Rechnung%20M%C3%A4rz%202026.pdf"),
        ("https://files.todoist.com/app/n1/Rechnung%20M%C3%A4rz.pdf", "https://files.todoist.com/app/n1/Rechnung%20M%C3%A4rz.pdf"),
        ("https://files.todoist.com/app/n1/a\tb.pdf?x=ä y", "https://files.todoist.com/app/n1/a%09b.pdf?x=%C3%A4%20y"),
        (FILE_URL, FILE_URL),
        (SIGNED_URL, SIGNED_URL),
    ],
)
def test_download_percent_encodes_what_http_client_refuses(url, sent):
    transport, opener = _stub_transport({sent: (200, {}, b"x")})
    assert transport.download(url, _collect)[2] == b"x"
    assert [request[0] for request in opener.requests] == [sent]


def test_download_percent_encodes_a_redirect_target():
    target = "https://d1abc.cloudfront.net/app/n1/März.pdf?Signature=s"
    transport, opener = _stub_transport({FILE_URL: (302, {"Location": target}, b""),
                                         "https://d1abc.cloudfront.net/app/n1/M%C3%A4rz.pdf?Signature=s": (200, {}, b"x")})
    assert transport.download(FILE_URL, _collect)[2] == b"x"
    assert opener.requests[1][1] is None


@pytest.mark.parametrize("error", [http.client.InvalidURL("control character in host"),
                                   UnicodeEncodeError("ascii", "ä", 0, 1, "not ASCII")])
def test_download_turns_a_url_that_cannot_be_sent_into_unusable(error):
    transport, _opener = _stub_transport({FILE_URL: error})
    with pytest.raises(api.FileUnusable, match=type(error).__name__):
        transport.download(FILE_URL, _collect)


def test_download_of_a_url_with_a_control_character_in_the_host_is_unusable():
    # The real opener: http.client refuses the host before it connects.
    with pytest.raises(api.FileUnusable):
        api.UrllibTransport(TOKEN).download("https://files.todoist.com\x01/x.pdf", _collect)


def test_download_wraps_errors_while_reading_the_body():
    transport, _opener = _stub_transport({FILE_URL: (200, {}, b"x" * 200000, 70000)})
    with pytest.raises(api.TransportError, match="TimeoutError") as info:
        transport.download(FILE_URL, _collect)
    assert info.value.retryable


def test_download_wraps_connection_errors():
    transport, _opener = _stub_transport({FILE_URL: urllib.error.URLError(ConnectionRefusedError(111, "refused"))})
    with pytest.raises(api.TransportError, match="cannot reach files.todoist.com") as info:
        transport.download(FILE_URL, _collect)
    assert info.value.retryable


def test_download_logs_neither_the_token_nor_a_query_string(caplog):
    transport, _opener = _stub_transport({FILE_URL: (302, {"Location": SIGNED_URL}, b""), SIGNED_URL: (200, {}, b"x")})
    client = api.Client(transport, sleep=lambda s: None)
    with caplog.at_level(logging.DEBUG):
        client.download(FILE_URL, _collect)
    messages = [r.getMessage() for r in caplog.records]
    assert "GET files.todoist.com/app/n1/preise.pdf" in messages
    assert "HTTP 302 redirect to d1abc.cloudfront.net/app/n1/preise.pdf" in messages
    for message in messages:
        assert TOKEN not in message
        assert "?" not in message and "Signature" not in message and "s3cr3t" not in message


def test_download_errors_hold_neither_the_token_nor_a_query_string():
    transport, _opener = _stub_transport(
        {FILE_URL: (302, {"Location": SIGNED_URL}, b""), SIGNED_URL: (302, {"Location": "http://x.example.net/?sig=s3cr3t"}, b"")}
    )
    with pytest.raises(AttachmentRefused) as info:
        api.Client(transport, sleep=lambda s: None).download(FILE_URL, _collect)
    assert TOKEN not in str(info.value) and "s3cr3t" not in str(info.value) and "?" not in str(info.value)


def test_download_against_a_local_server(server):
    # Plain HTTP and not a Todoist host: the transport sends no token, streams, and asks for identity.
    _httpd, base_url = server
    body = bytes(range(256)) * 600
    _Handler.response = (200, {"Content-Type": "application/pdf"}, body)
    status, headers, data = api.UrllibTransport(TOKEN).download(base_url + "file.pdf", _collect)
    assert (status, data) == (200, body)
    [(method, path, sent, _raw)] = _Handler.received
    assert (method, path) == ("GET", "/api/v1/file.pdf")
    assert "Authorization" not in sent
    assert sent["Accept-Encoding"] == "identity"
    assert sent["User-Agent"] == f"todoist-takeout/{__version__}"


def test_download_of_a_cut_off_body_from_a_local_server(server):
    _httpd, base_url = server
    _Handler.response = (200, {"X-Test-Truncate": "1000"}, b"x" * 5000)
    with pytest.raises(api.TransportError) as info:
        api.UrllibTransport(TOKEN).download(base_url + "file.pdf", _collect)
    assert info.value.retryable


def test_download_does_not_let_urllib_follow_redirects(server):
    # With the real opener, a redirect to plain HTTP is refused before any request goes there.
    _httpd, base_url = server
    _Handler.response = (302, {"Location": "http://example.invalid/steal"}, b"")
    with pytest.raises(api.RedirectNotFollowed):
        api.UrllibTransport(TOKEN).download(base_url + "file.pdf", _collect)
    assert len(_Handler.received) == 1


# --- Attachment files: client (add-attachment-downloads 3.3) -------------------


class FakeFileTransport:
    """Replays a script of file responses. A 2xx body is a list of chunks; an exception in it is raised mid-body."""

    def __init__(self, *script):
        self.script = list(script)
        self.urls = []

    def send(self, method, path, params):
        return 200, {}, b"{}"

    def download(self, url, write):
        self.urls.append(url)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        status, headers, body = step
        if 200 <= status < 300:
            return status, headers, write(_replay(body))
        return status, headers, body


def _replay(chunks):
    for chunk in chunks:
        if isinstance(chunk, Exception):
            raise chunk
        yield chunk


def test_client_download_returns_what_write_returns():
    client, sleeps = _client(FakeFileTransport((200, {}, [b"ab", b"cd"])))
    assert client.download(FILE_URL, _collect) == b"abcd"
    assert sleeps == []


def test_client_download_waits_for_429_and_counts_toward_the_cap():
    transport = FakeFileTransport((429, {"Retry-After": "12"}, b""), (200, {}, [b"x"]))
    client, sleeps = _client(transport)
    assert client.download(FILE_URL, _collect) == b"x"
    assert sleeps == [12]
    assert client.rate_limit_waited == 12
    assert transport.urls == [FILE_URL, FILE_URL]


def test_client_download_shares_the_rate_limit_cap_with_api_requests():
    transport = FakeTransport(_error(429, extra={"retry_after": 500}), OK)
    client, _sleeps = _client(transport, max_rate_limit_wait=900)
    client.post("sync", {})
    client._transport = FakeFileTransport((429, {"Retry-After": "500"}, b""))
    with pytest.raises(RateLimitExhausted, match="files.todoist.com/app/n1/preise.pdf"):
        client.download(FILE_URL, _collect)


def test_client_download_retries_5xx():
    client, sleeps = _client(FakeFileTransport((503, {}, b""), (502, {}, b""), (200, {}, [b"x"])))
    assert client.download(FILE_URL, _collect) == b"x"
    assert sleeps == [1, 2]


def test_client_download_persistent_5xx_fails_the_run():
    client, _sleeps = _client(FakeFileTransport(*[(500, {}, b"")] * 5))
    with pytest.raises(ApiError) as info:
        client.download(FILE_URL, _collect)
    assert not isinstance(info.value, AttachmentRefused)
    assert info.value.http_status == 500
    assert "gave up after 5 attempts" in str(info.value)


def test_client_download_persistent_network_errors_fail_the_run():
    client, sleeps = _client(FakeFileTransport(*[api.TransportError("cannot reach files.todoist.com: refused")] * 5))
    with pytest.raises(NetworkError, match="gave up after 5 attempts"):
        client.download(FILE_URL, _collect)
    assert sleeps == [1, 2, 4, 8]


def test_client_download_restarts_the_file_after_a_network_error_in_the_body(tmp_path):
    transport = FakeFileTransport(
        (200, {}, [b"first half ", api.TransportError("request failed while reading the file: timed out")]),
        (200, {}, [b"first half ", b"second half"]),
    )
    client, sleeps = _client(transport)
    path = tmp_path / "attachments" / "N1" / "preise.pdf"
    size = client.download(FILE_URL, lambda chunks: storage.write_stream_atomic(path, chunks, 22))
    assert size == 22
    assert path.read_bytes() == b"first half second half"
    assert sleeps == [1]
    assert list(path.parent.iterdir()) == [path]


def test_client_download_does_not_retry_a_size_mismatch(tmp_path):
    transport = FakeFileTransport((200, {}, [b"short"]))
    client, _sleeps = _client(transport)
    path = tmp_path / "attachments" / "N1" / "preise.pdf"
    with pytest.raises(SizeMismatch):
        client.download(FILE_URL, lambda chunks: storage.write_stream_atomic(path, chunks, 20480))
    assert len(transport.urls) == 1
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_download_4xx_is_a_refusal_not_an_auth_error(status):
    transport = FakeFileTransport((status, {}, b'{"error": "Unauthorized", "error_tag": "AUTH_INVALID_TOKEN"}'))
    client, sleeps = _client(transport)
    with pytest.raises(AttachmentRefused) as info:
        client.download(FILE_URL, _collect)
    assert not isinstance(info.value, ApiError)
    assert info.value.http_status == status
    assert f"HTTP {status}" in str(info.value)
    assert "TODOIST_API_TOKEN" not in str(info.value)
    assert sleeps == [] and len(transport.urls) == 1


def test_client_download_unusable_file_is_skipped_without_retrying():
    client, sleeps = _client(FakeFileTransport(api.FileUnusable("the file host sent an unsupported Content-Encoding 'br'")))
    with pytest.raises(AttachmentUnusable, match=r"GET files.todoist.com/app/n1/preise.pdf cannot be used: .*'br'"):
        client.download(FILE_URL, _collect)
    assert sleeps == []


def test_client_download_unfollowed_redirect_is_a_refusal():
    client, _sleeps = _client(FakeFileTransport(api.RedirectNotFollowed("HTTP 302 redirect to a URL that is not HTTPS", http_status=302)))
    with pytest.raises(AttachmentRefused, match="not HTTPS") as info:
        client.download(FILE_URL, _collect)
    assert info.value.http_status == 302


@pytest.mark.parametrize(
    "url, host, reason",
    [
        ("https://example.com/preise.pdf", "example.com", "example.com is not a Todoist host"),
        ("http://files.todoist.com/preise.pdf", "files.todoist.com", "does not use HTTPS"),
        ("https://files.todoist.com:8443/preise.pdf", "files.todoist.com", "its URL names the port 8443"),
        ("https://user@files.todoist.com/preise.pdf", "files.todoist.com", "its URL holds user information"),
        ("https://files.todoist.com:99999/preise.pdf", "files.todoist.com", "its URL is not valid"),
        (None, None, "does not use HTTPS"),
    ],
)
def test_client_download_makes_no_request_to_an_unexpected_host(url, host, reason):
    transport = FakeFileTransport()
    client, _sleeps = _client(transport)
    with pytest.raises(UnexpectedHost, match=reason) as info:
        client.download(url, _collect)
    assert info.value.host == host
    assert transport.urls == []
