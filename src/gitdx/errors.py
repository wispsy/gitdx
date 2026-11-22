"""Expected repository and format failures."""


class GitdxError(Exception):
    """Base exception for failures callers can report without a traceback."""


class InvalidObject(GitdxError, ValueError):
    """An object is malformed or inconsistent with its advertised identity."""


class UnsupportedFormat(GitdxError, ValueError):
    """The requested repository or storage feature is not implemented."""
