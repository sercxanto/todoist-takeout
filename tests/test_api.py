import gzip
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from todoist_takeout import __version__, api
from todoist_takeout.errors import (
    ApiError,
    AuthError,
    NetworkError,
    PlanRestricted,
    RateLimitExhausted,
)

TOKEN = "0123456789abcdef0123456789abcdef01234567"


# --- 3.1 urllib transport against a local server ------------------------------


class _Handler(BaseHTTPRequestHandler):
    # Set per test: (status, headers, body) for the next response.
    response = (200, {}, b"{}")
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append((self.path, dict(self.headers), self.rfile.read(length)))
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
    [(path, headers, raw)] = _Handler.received
    assert path == "/api/v1/sync"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept-Encoding"] == "gzip"
    assert headers["User-Agent"] == f"todoist-takeout/{__version__}"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(raw.decode("ascii")) == {"sync_token": ["*"], "resource_types": ['["all"]']}
    assert any("POST /api/v1/sync" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert TOKEN not in repr(transport)


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


# --- 3.4 sync call -------------------------------------------------------------


def test_sync_posts_token_and_all_resource_types_and_returns_body_unchanged():
    body = b'{"sync_token":"abc",  "full_sync": true, "items": [{"id": "1", "n": 1.0}]}'
    transport = FakeTransport((200, {}, body))
    client, _sleeps = _client(transport)
    assert client.sync("prev-token") is body
    assert transport.requests == [("POST", "sync", {"sync_token": "prev-token", "resource_types": '["all"]'})]
