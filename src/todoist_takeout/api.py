"""Talks to the Todoist API v1: HTTP transport, retries, rate limits and error classification.

The token is set as the `Authorization` header in exactly one place, `UrllibTransport._request`.
No message or log record ever contains it: requests are logged by method and path only.
"""

import gzip
import http.client
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Protocol

from todoist_takeout import __version__
from todoist_takeout.errors import (
    ApiError,
    AuthError,
    NetworkError,
    PlanRestricted,
    RateLimitExhausted,
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


class TransportError(Exception):
    """A request could not be completed at the network level.

    `retryable` is False for failures that a retry cannot fix, such as a certificate that cannot be
    verified.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Transport(Protocol):
    def send(self, method: str, path: str, form: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        """Send one request. Return status, headers and the body after transfer decoding.

        HTTP error statuses are returned, not raised. Network failures raise TransportError.
        """


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

    def send(self, method, path, form):
        url = urllib.parse.urljoin(self._base_url, path)
        log.debug("%s %s", method, urllib.parse.urlsplit(url).path)
        try:
            status, headers, body = self._request(method, url, form)
            if headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
        except urllib.error.HTTPError as exc:
            # Raised for every non-2xx status. It still carries the response.
            with exc:
                status, headers, body = exc.code, exc.headers, exc.read()
            try:
                if headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
            except (OSError, EOFError):
                body = b""
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise TransportError(
                    f"cannot verify the TLS certificate of {urllib.parse.urlsplit(url).hostname}: "
                    f"{reason.verify_message or reason}",
                    retryable=False,
                ) from exc
            raise TransportError(f"cannot reach {urllib.parse.urlsplit(url).hostname}: {reason}") from exc
        except (OSError, EOFError, http.client.HTTPException) as exc:
            # Timeouts, dropped connections, truncated or corrupt gzip bodies.
            raise TransportError(f"request failed: {type(exc).__name__}: {exc}") from exc
        return status, headers, body

    def _request(self, method, url, form):
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form).encode("ascii"),
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
        )
        with self._opener.open(request, timeout=self._timeout) as response:
            return response.status, response.headers, response.read()


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

    def post(self, path: str, form: Mapping[str, str]) -> bytes:
        attempt = 1
        while True:
            try:
                status, headers, body = self._transport.send("POST", path, form)
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
                self._wait_for_rate_limit(path, headers, _error_body(body))
                continue
            if status >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise _classify(path, status, body, suffix=f" (gave up after {attempt} attempts)")
                self._backoff(attempt, f"HTTP {status}")
                attempt += 1
                continue
            raise _classify(path, status, body)

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = 2 ** (attempt - 1) + self._jitter() * MAX_JITTER
        log.info("request failed (%s); retrying in %.0f s (attempt %d of %d)", reason, delay, attempt + 1, MAX_ATTEMPTS)
        self._sleep(delay)

    def _wait_for_rate_limit(self, path: str, headers: Mapping[str, str], error: dict) -> None:
        delay = max(_retry_after(headers, error), MIN_RATE_LIMIT_WAIT) + self._jitter() * MAX_JITTER
        if self.rate_limit_waited + delay > self._max_rate_limit_wait:
            raise RateLimitExhausted(
                f"the Todoist API kept rate-limiting POST /{path}; waiting another {delay:.0f} s would "
                f"exceed the limit of {self._max_rate_limit_wait:.0f} s per run "
                f"(waited {self.rate_limit_waited:.0f} s so far)"
            )
        log.info("rate-limited by the Todoist API; waiting %.0f s before retrying", delay)
        self._sleep(delay)
        self.rate_limit_waited += delay


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


def _error_body(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _classify(path: str, status: int, body: bytes, *, suffix: str = "") -> ApiError:
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
    message = f"POST /{path} failed: {text or 'no error message'} ({', '.join(details)}){suffix}"
    fields = {"http_status": status, "error": text, "error_tag": tag, "error_code": code}

    if status == 401 or (
        status == 403 and tag is not None and (tag in AUTH_ERROR_TAGS or tag.startswith(AUTH_ERROR_TAG_PREFIX))
    ):
        return AuthError(f"{message}; {TOKEN_HINT}", **fields)
    if status == 403 and tag in PLAN_RESTRICTED_TAGS:
        return PlanRestricted(message, **fields)
    return ApiError(message, **fields)
