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
