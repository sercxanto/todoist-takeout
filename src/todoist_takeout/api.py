"""Talks to the Todoist API v1: HTTP transport, retries, rate limits and error classification.

The token is set as the `Authorization` header in two places: `UrllibTransport._request` for API
requests, and `UrllibTransport.download` for the first request of an attachment file, only when its
URL is HTTPS on a Todoist host. No message or log record ever contains it: requests are logged by
method, host and path only, never with a query string.
"""

import gzip
import http.client
import json
import logging
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Protocol, TypeVar

from todoist_takeout import __version__
from todoist_takeout.errors import (
    ApiError,
    AttachmentRefused,
    AuthError,
    NetworkError,
    PlanRestricted,
    RateLimitExhausted,
    UnexpectedHost,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1/"
TIMEOUT = 300.0
USER_AGENT = f"todoist-takeout/{__version__}"

MAX_ATTEMPTS = 5
DEFAULT_RATE_LIMIT_WAIT = 60.0
DEFAULT_MAX_RATE_LIMIT_WAIT = 900.0
MIN_RATE_LIMIT_WAIT = 1.0
MAX_JITTER = 1.0

# The API documents no error tag for plan restrictions. `PREMIUM_ONLY` is unverified; the contract
# tests replace or extend it as they observe real tags. An unknown 403 fails loudly.
PLAN_RESTRICTED_TAGS = frozenset({"PREMIUM_ONLY"})
# Tags of a 403 that means the token itself is not accepted.
AUTH_ERROR_TAGS = frozenset({"UNAUTHORIZED", "AUTHENTICATION_ERROR"})
AUTH_ERROR_TAG_PREFIX = "AUTH_"

TOKEN_HINT = "check that TODOIST_API_TOKEN holds a valid personal API token"

# Attachment files (design D5, D6).
MAX_REDIRECTS = 5
CHUNK_SIZE = 64 * 1024
# todoist.com or any subdomain of it, as `urlsplit` reports a host: lower-case, without a port.
_TODOIST_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*todoist\.com")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

T = TypeVar("T")


# A corrupt deflate stream raises zlib.error, which is not an OSError.
_DECODE_ERRORS = (OSError, EOFError, zlib.error)
_READ_ERRORS = (*_DECODE_ERRORS, http.client.HTTPException)


class TransportError(Exception):
    """A request could not be completed at the network level.

    `retryable` is False for failures that a retry cannot fix, such as a certificate that cannot be
    verified.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RedirectNotFollowed(Exception):
    """A redirect of an attachment request points somewhere the tool does not follow it to."""

    def __init__(self, message: str, *, http_status: int):
        super().__init__(message)
        self.http_status = http_status


class Transport(Protocol):
    def send(self, method: str, path: str, params: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        """Send one request. Return status, headers and the body after transfer decoding.

        `params` is the form body of a POST request and the query string of a GET request. HTTP
        error statuses are returned, not raised. Network failures raise TransportError.
        """

    def download(self, url: str, write: Callable[[Iterator[bytes]], T]) -> tuple[int, Mapping[str, str], T | bytes]:
        """GET an attachment file. For a 2xx, pass the decoded body to `write` and return its result.

        Any other status is returned with the start of its body. A redirect that is not followed
        raises RedirectNotFollowed; network failures, also while the body is read, raise TransportError.
        """


def is_todoist_file_url(url) -> bool:
    """Whether `url` may be requested for an attachment file, with the token: HTTPS on a Todoist host.

    The host is `todoist.com` or ends in `.todoist.com`, compared as a whole host name, never as a
    substring of the URL. A URL with user info or a port other than 443 does not qualify.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and "@" not in parts.netloc
        and port in (None, 443)
        and parts.hostname is not None
        and _TODOIST_HOST.fullmatch(parts.hostname) is not None
    )


def url_host(url) -> str | None:
    """The host of `url` in lower case, or None if it has none."""
    try:
        return urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return None


def _loggable_url(url: str) -> str:
    # A signed URL carries its credential in the query string, so only the host and path are shown.
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname}{parts.path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Following a redirect would send the token to wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """The real transport: HTTPS with certificate verification, using only the standard library."""

    def __init__(self, token: str, *, base_url: str = BASE_URL, timeout: float = TIMEOUT):
        self._token = token
        self._base_url = base_url
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
        )

    def __repr__(self) -> str:
        return f"UrllibTransport(base_url={self._base_url!r})"

    def send(self, method, path, params):
        url = urllib.parse.urljoin(self._base_url, path)
        # Only the path is logged: a query may carry content such as dates or IDs.
        log.debug("%s %s", method, urllib.parse.urlsplit(url).path)
        try:
            status, headers, body = self._request(method, url, params)
            if headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
        except urllib.error.HTTPError as exc:
            # Raised for every non-2xx status. It still carries the response.
            try:
                with exc:
                    status, headers, body = exc.code, exc.headers, exc.read()
            except _READ_ERRORS as read_exc:
                raise TransportError(
                    f"request failed while reading the HTTP {exc.code} response: {type(read_exc).__name__}: {read_exc}"
                ) from read_exc
            try:
                if headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
            except _DECODE_ERRORS:
                body = b""
        except urllib.error.URLError as exc:
            raise _unreachable(url, exc) from exc
        except _READ_ERRORS as exc:
            # Timeouts, dropped connections, truncated or corrupt gzip bodies.
            raise TransportError(f"request failed: {type(exc).__name__}: {exc}") from exc
        return status, headers, body

    def download(self, url, write):
        # The token goes with the first request only. Every redirect is followed without it, also one
        # back to a Todoist host: the file host signs the URL it redirects to (design D5).
        token = self._token if is_todoist_file_url(url) else None
        redirects = 0
        while True:
            log.debug("GET %s", _loggable_url(url))
            headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            with self._open_file(url, headers) as (status, response_headers, response):
                if 300 <= status < 400:
                    url = self._redirect_target(url, status, response_headers, redirects)
                    token = None
                    redirects += 1
                    continue
                if 200 <= status < 300:
                    return status, response_headers, write(_file_chunks(response, response_headers))
                try:
                    body = response.read(CHUNK_SIZE)
                except _READ_ERRORS:
                    body = b""
                return status, response_headers, body

    def _redirect_target(self, url: str, status: int, headers: Mapping[str, str], redirects: int) -> str:
        location = _header(headers, "Location")
        if status not in _REDIRECT_STATUSES or not location:
            raise RedirectNotFollowed(f"HTTP {status} without a redirect to follow", http_status=status)
        target = urllib.parse.urljoin(url, location.strip())
        if urllib.parse.urlsplit(target).scheme != "https":
            raise RedirectNotFollowed(f"HTTP {status} redirect to a URL that is not HTTPS", http_status=status)
        if redirects >= MAX_REDIRECTS:
            raise RedirectNotFollowed(f"HTTP {status} after {MAX_REDIRECTS} redirects", http_status=status)
        log.debug("HTTP %d redirect to %s", status, _loggable_url(target))
        return target

    @contextmanager
    def _open_file(self, url: str, headers: Mapping[str, str]):
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            response = self._opener.open(request, timeout=self._timeout)
            status = response.status
        except urllib.error.HTTPError as exc:
            # Raised for every non-2xx status, redirects included. It still carries the response.
            response, status = exc, exc.code
        except urllib.error.URLError as exc:
            raise _unreachable(url, exc) from exc
        except _READ_ERRORS as exc:
            raise TransportError(f"request failed: {type(exc).__name__}: {exc}") from exc
        with response:
            yield status, response.headers, response

    def _request(self, method, url, params):
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        }
        encoded = urllib.parse.urlencode(params)
        if method == "GET":
            data = None
            if encoded:
                url = f"{url}{'&' if urllib.parse.urlsplit(url).query else '?'}{encoded}"
        else:
            data = encoded.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        with self._opener.open(request, timeout=self._timeout) as response:
            return response.status, response.headers, response.read()


def _unreachable(url: str, exc: urllib.error.URLError) -> TransportError:
    reason = exc.reason
    host = urllib.parse.urlsplit(url).hostname
    if isinstance(reason, ssl.SSLCertVerificationError):
        return TransportError(
            f"cannot verify the TLS certificate of {host}: {reason.verify_message or reason}", retryable=False
        )
    return TransportError(f"cannot reach {host}: {reason}")


def _file_chunks(response, headers: Mapping[str, str]) -> Iterator[bytes]:
    """Yield the body of a file response in blocks, decoding gzip if the host sent it anyway."""
    encoding = (_header(headers, "Content-Encoding") or "identity").strip().lower()
    if encoding in ("gzip", "x-gzip"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "identity":
        decoder = None
    else:
        raise TransportError(f"the file host sent an unsupported Content-Encoding {encoding!r}", retryable=False)
    # http.client's read(n) ends quietly when the connection closes early, so the length is checked here.
    announced = _content_length(headers)
    received = 0
    try:
        while chunk := response.read(CHUNK_SIZE):
            received += len(chunk)
            yield decoder.decompress(chunk) if decoder else chunk
        if announced is not None and received != announced:
            raise TransportError(f"request failed: the file body is cut off after {received} of {announced} bytes")
        if decoder:
            tail = decoder.flush()
            if not decoder.eof:
                raise TransportError("request failed: the gzip body of the file is cut off")
            yield tail
    except _READ_ERRORS as exc:
        # Timeouts, dropped connections, truncated bodies, corrupt gzip data.
        raise TransportError(f"request failed while reading the file: {type(exc).__name__}: {exc}") from exc


def _content_length(headers: Mapping[str, str]) -> int | None:
    # With chunked transfer encoding, http.client detects a cut-off body itself.
    if _header(headers, "Transfer-Encoding") is not None:
        return None
    value = _header(headers, "Content-Length")
    try:
        length = int(value) if value is not None else None
    except ValueError:
        return None
    return length if length is not None and length >= 0 else None


class Client:
    """Sends requests through a transport, with retries, rate-limit waits and error classification.

    `sleep` and `jitter` (a function returning a float in [0, 1)) are injected so that tests never
    wait. The rate-limit wait cap applies to the sum of all rate-limit waits of this client.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        max_rate_limit_wait: float = DEFAULT_MAX_RATE_LIMIT_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self._transport = transport
        self._max_rate_limit_wait = max_rate_limit_wait
        self._sleep = sleep
        self._jitter = jitter
        self.rate_limit_waited = 0.0

    def sync(self, sync_token: str) -> bytes:
        """Request a sync of all resource types and return the response body unchanged."""
        return self.post("sync", {"sync_token": sync_token, "resource_types": '["all"]'})

    def get(self, path: str, params: Mapping[str, str]) -> bytes:
        return self.request("GET", path, params)

    def post(self, path: str, form: Mapping[str, str]) -> bytes:
        return self.request("POST", path, form)

    def request(self, method: str, path: str, params: Mapping[str, str]) -> bytes:
        """Send a request, retrying network errors, 5xx and rate limits. Return the body unchanged."""
        return self._with_retries(
            f"{method} /{_path_only(path)}",
            lambda: self._transport.send(method, path, params),
            lambda status, body, suffix: _classify(method, path, status, body, suffix=suffix),
        )

    def download(self, url: str, write: Callable[[Iterator[bytes]], T]) -> T:
        """Request an attachment file, pass its decoded body to `write`, and return what it returns.

        Retries like `request`. A network error while the body is read is a failed attempt, so
        `write` is called again from the start; it must leave nothing behind when its chunks fail.
        Raises UnexpectedHost, without a request, for a URL that is not HTTPS on a Todoist host, and
        AttachmentRefused for a 4xx other than 429 or a redirect that is not followed. A 401 or 403
        here is such a refusal, never an AuthError: the API accepted the token earlier in the run.
        """
        if not is_todoist_file_url(url):
            host = url_host(url)
            if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != "https":
                reason = "its URL does not use HTTPS"
            else:
                reason = f"{host or 'its URL'} is not a Todoist host"
            raise UnexpectedHost(reason, host=host)
        label = f"GET {_loggable_url(url)}"

        def failure(status: int, body: bytes, suffix: str) -> Exception:
            if status >= 500:
                return ApiError(f"{label} failed (HTTP {status}){suffix}", http_status=status)
            return AttachmentRefused(f"{label} was refused (HTTP {status})", http_status=status)

        try:
            return self._with_retries(label, lambda: self._transport.download(url, write), failure)
        except RedirectNotFollowed as exc:
            raise AttachmentRefused(f"{label} was refused: {exc}", http_status=exc.http_status) from exc

    def _with_retries(
        self,
        label: str,
        exchange: Callable[[], tuple[int, Mapping[str, str], T]],
        failure: Callable[[int, bytes, str], Exception],
    ) -> T:
        """Run `exchange` until it returns a 2xx, retrying network errors, 5xx and rate limits.

        `failure` turns a status, body and message suffix into the exception to raise.
        """
        attempt = 1
        while True:
            try:
                status, headers, body = exchange()
            except TransportError as exc:
                if not exc.retryable:
                    raise NetworkError(str(exc)) from exc
                if attempt >= MAX_ATTEMPTS:
                    raise NetworkError(f"{exc} (gave up after {attempt} attempts)") from exc
                self._backoff(attempt, str(exc))
                attempt += 1
                continue

            if 200 <= status < 300:
                return body
            if status == 429:
                self._wait_for_rate_limit(label, headers, _error_body(body))
                continue
            if status >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise failure(status, body, f" (gave up after {attempt} attempts)")
                self._backoff(attempt, f"HTTP {status}")
                attempt += 1
                continue
            raise failure(status, body, "")

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = 2 ** (attempt - 1) + self._jitter() * MAX_JITTER
        log.info("request failed (%s); retrying in %.0f s (attempt %d of %d)", reason, delay, attempt + 1, MAX_ATTEMPTS)
        self._sleep(delay)

    def _wait_for_rate_limit(self, label: str, headers: Mapping[str, str], error: dict) -> None:
        delay = max(_retry_after(headers, error), MIN_RATE_LIMIT_WAIT) + self._jitter() * MAX_JITTER
        if self.rate_limit_waited + delay > self._max_rate_limit_wait:
            raise RateLimitExhausted(
                f"the Todoist API kept rate-limiting {label}; waiting another {delay:.0f} s would "
                f"exceed the limit of {self._max_rate_limit_wait:.0f} s per run "
                f"(waited {self.rate_limit_waited:.0f} s so far)"
            )
        log.info("rate-limited by the Todoist API; waiting %.0f s before retrying", delay)
        self._sleep(delay)
        self.rate_limit_waited += delay


def cursor_pages(client: Client, path: str, params: Mapping[str, str], limit: int) -> Iterator[dict]:
    """Yield the parsed pages of a GET endpoint with cursor pagination.

    Follows `next_cursor` until it is absent or null. A cursor seen before raises ApiError instead
    of looping forever.
    """
    seen: set[str] = set()
    cursor = None
    while True:
        query = {**params, "limit": str(limit)}
        if cursor is not None:
            query["cursor"] = cursor
        page = _parse_page("GET", path, client.get(path, query))
        yield page
        cursor = page.get("next_cursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise ApiError(f"GET /{_path_only(path)} returned the cursor {cursor!r} again", http_status=200)
        seen.add(cursor)


def offset_pages(
    client: Client, path: str, params: Mapping[str, str], limit: int, items_key: str = "items"
) -> Iterator[dict]:
    """Yield the parsed pages of a GET endpoint with offset pagination.

    Advances `offset` by `limit` until a page holds fewer than `limit` entries under `items_key`.
    """
    offset = 0
    while True:
        page = _parse_page("GET", path, client.get(path, {**params, "limit": str(limit), "offset": str(offset)}))
        items = page.get(items_key)
        if not isinstance(items, list):
            raise ApiError(f"GET /{_path_only(path)} returned a page without a list in {items_key!r}", http_status=200)
        yield page
        if len(items) < limit:
            return
        offset += limit


def _parse_page(method: str, path: str, body: bytes) -> dict:
    try:
        page = json.loads(body)
    except ValueError as exc:
        raise ApiError(f"{method} /{_path_only(path)} returned invalid JSON: {exc}", http_status=200) from exc
    if not isinstance(page, dict):
        raise ApiError(f"{method} /{_path_only(path)} returned a page that is not a JSON object", http_status=200)
    return page


def _retry_after(headers: Mapping[str, str], error: dict) -> float:
    extra = error.get("error_extra")
    if isinstance(extra, dict):
        value = extra.get("retry_after")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    header = _header(headers, "Retry-After")
    if header is not None:
        try:
            value = float(header.strip())
        except ValueError:
            pass  # an HTTP date; the default is close enough
        else:
            if value >= 0:
                return value
    return DEFAULT_RATE_LIMIT_WAIT


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def _path_only(path: str) -> str:
    # Messages, like the request log, never show a query string.
    return urllib.parse.urlsplit(path).path


def _error_body(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _classify(method: str, path: str, status: int, body: bytes, *, suffix: str = "") -> ApiError:
    """Turn an error response into the matching ApiError subclass."""
    error = _error_body(body)
    text = error.get("error")
    tag = error.get("error_tag")
    code = error.get("error_code")
    text = text if isinstance(text, str) else None
    tag = tag if isinstance(tag, str) else None
    code = code if isinstance(code, int) and not isinstance(code, bool) else None

    details = [f"HTTP {status}"]
    if tag is not None:
        details.append(f"error_tag {tag}")
    if code is not None:
        details.append(f"error_code {code}")
    message = f"{method} /{_path_only(path)} failed: {text or 'no error message'} ({', '.join(details)}){suffix}"
    fields = {"http_status": status, "error": text, "error_tag": tag, "error_code": code}

    if status == 401 or (
        status == 403 and tag is not None and (tag in AUTH_ERROR_TAGS or tag.startswith(AUTH_ERROR_TAG_PREFIX))
    ):
        return AuthError(f"{message}; {TOKEN_HINT}", **fields)
    if status == 403 and tag in PLAN_RESTRICTED_TAGS:
        return PlanRestricted(message, **fields)
    return ApiError(message, **fields)
