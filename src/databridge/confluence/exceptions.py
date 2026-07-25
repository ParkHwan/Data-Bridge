"""Errors raised by the Confluence ingestion integration."""

from __future__ import annotations


class ConfluenceError(Exception):
    """Base error for Confluence ingestion."""

    def __init__(self, message: str, *, page_id: str | None = None) -> None:
        self.page_id = page_id
        super().__init__(message)


class ConfluenceAuthError(ConfluenceError):
    """Authentication or authorization failed."""


class ConfluenceAPIError(ConfluenceError):
    """The Confluence API returned an unsuccessful response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        page_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, page_id=page_id)


class ConfluenceRateLimitError(ConfluenceAPIError):
    """The Confluence API rate limit remained exhausted after retries."""

    def __init__(self, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("Confluence rate limit exceeded", status_code=429)


class ADFParseError(ConfluenceError):
    """An ADF payload could not be decoded or rendered."""


class EmptyBodyError(ConfluenceError):
    """A page has no usable ADF body and must not replace stored chunks."""


class BatchSafetyError(ConfluenceError):
    """A batch safety limit or consistency check failed."""


class BatchAlreadyRunningError(ConfluenceError):
    """Another ingestion run currently owns the database advisory lock."""
