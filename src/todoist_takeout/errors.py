"""Exception hierarchy. The CLI maps each class to an exit status (design D3)."""


class TakeoutError(Exception):
    """Base class for errors the tool reports as a one-line message."""

    exit_code = 1


class UsageError(TakeoutError):
    """The command line is invalid."""

    exit_code = 2


class ConfigError(TakeoutError):
    """The configuration is invalid: missing token, invalid target, unsupported format version."""

    exit_code = 2


class RunError(TakeoutError):
    """The run failed after it started working with the API or the target directory."""

    exit_code = 1


class StorageError(RunError):
    """A file in the target directory could not be written or would be overwritten wrongly."""


class LockHeld(RunError):
    """Another run holds the lock on the target directory."""


class NetworkError(RunError):
    """A request failed at the network level after all retries, or could not be sent securely."""


class RateLimitExhausted(RunError):
    """The API kept rate-limiting requests beyond the run's total wait limit."""


class ApiError(RunError):
    """The API answered with an error. Carries the fields of the API's error body."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        error: str | None = None,
        error_tag: str | None = None,
        error_code: int | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.error = error
        self.error_tag = error_tag
        self.error_code = error_code


class AuthError(ApiError):
    """The API rejected the token."""


class PlanRestricted(ApiError):
    """The request needs a feature that the user's plan does not include."""
