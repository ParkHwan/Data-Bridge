"""Small asynchronous client for the Confluence Cloud REST v2 API."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

from databridge.confluence.exceptions import (
    ConfluenceAPIError,
    ConfluenceAuthError,
    ConfluenceRateLimitError,
)
from databridge.confluence.models import Page, PageListResponse, Space, SpaceListResponse


class _RetryAfterWait:
    def __call__(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, ConfluenceRateLimitError) and exc.retry_after is not None:
            return max(0.0, exc.retry_after)
        attempt_number = int(retry_state.attempt_number)
        return float(min(2 ** max(attempt_number - 1, 0), 10))


class ConfluenceClient:
    """Authenticated async client exposing only the endpoints used by the batch."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        email: str = "",
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not api_token:
            raise ValueError("api_token must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        site = base_url.rstrip("/")
        self._api_base = f"{site}/wiki/api/v2"
        self._headers = {
            "Authorization": self._authorization(email=email, api_token=api_token),
            "Accept": "application/json",
        }
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _authorization(*, email: str, api_token: str) -> str:
        if not email:
            return f"Bearer {api_token}"
        raw = base64.b64encode(f"{email}:{api_token}".encode()).decode("ascii")
        return f"Basic {raw}"

    async def __aenter__(self) -> ConfluenceClient:
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _active_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ConfluenceClient must be used as an async context manager")
        return self._client

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                return max(0.0, parsed.timestamp() - datetime.now(parsed.tzinfo).timestamp())
            except (TypeError, ValueError, OverflowError):
                return None

    async def _request(
        self, method: str, path: str, *, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=_RetryAfterWait(),
            retry=retry_if_exception_type((httpx.TransportError, ConfluenceRateLimitError)),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await self._active_client().request(method, path, params=params)
                if response.status_code == 401:
                    raise ConfluenceAuthError("Confluence authentication failed")
                if response.status_code == 429:
                    raise ConfluenceRateLimitError(retry_after=self._retry_after(response))
                if response.status_code >= 400:
                    raise ConfluenceAPIError(
                        f"Confluence API returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                value = response.json()
                if not isinstance(value, dict):
                    raise ConfluenceAPIError("Confluence API returned a non-object response")
                return cast(dict[str, Any], value)
        raise RuntimeError("retry loop terminated without a response")

    async def get_space_by_key(self, space_key: str) -> Space:
        raw = await self._request("GET", "/spaces", params={"keys": space_key, "limit": 1})
        response = SpaceListResponse.model_validate(raw)
        if not response.results:
            raise ConfluenceAPIError(f"Confluence space not found: {space_key}", status_code=404)
        space = response.results[0]
        if space.key != space_key:
            raise ConfluenceAPIError(f"Confluence returned an unexpected space: {space.key}")
        return space

    async def get_page(self, page_id: str, *, body_format: str = "atlas_doc_format") -> Page:
        raw = await self._request("GET", f"/pages/{page_id}", params={"body-format": body_format})
        return Page.model_validate(raw)

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/folders/{folder_id}")

    async def iter_folder_descendants(
        self, folder_id: str, *, batch_size: int = 50, depth: int = 10
    ) -> AsyncIterator[Page]:
        if batch_size < 1 or depth < 1:
            raise ValueError("batch_size and depth must be positive")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": batch_size, "depth": depth}
            if cursor:
                params["cursor"] = cursor
            raw = await self._request("GET", f"/folders/{folder_id}/descendants", params=params)
            response = PageListResponse.model_validate(raw)
            for item in response.results:
                yield item
            next_link = response.links.next if response.links else None
            if not next_link:
                return
            values = parse_qs(urlparse(next_link).query).get("cursor", [])
            next_cursor = values[0] if values else None
            if not next_cursor or next_cursor in seen_cursors:
                raise ConfluenceAPIError("Confluence pagination returned an invalid cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
