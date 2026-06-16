"""Upstream HTTP client: httpx.AsyncClient wrapper."""
from __future__ import annotations

from typing import AsyncIterator

import httpx


class UpstreamClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Extract path prefix from base_url (e.g., "/v1" from "https://api.example.com/v1")
        # to avoid duplicating it when the request path also starts with the same prefix.
        from urllib.parse import urlparse
        self._base_path = urlparse(self.base_url).path.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                trust_env=False,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None
        # If base_url has a path prefix (e.g., "/v1"), strip it from the
        # request path so that httpx's base_url merging produces the correct URL.
        # Example: base_url="https://api.example.com/v1", path="/v1/chat/completions"
        #   -> strip "/v1" -> "/chat/completions"
        #   -> httpx produces: https://api.example.com/v1/chat/completions
        if self._base_path and path.startswith(self._base_path):
            path = path[len(self._base_path):] or "/"
        return await self._client.request(
            method, path, headers=headers, content=content, params=params,
        )

    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict | None = None,
    ) -> AsyncIterator[httpx.Response]:
        if self._client is None:
            await self.start()
        assert self._client is not None
        if self._base_path and path.startswith(self._base_path):
            path = path[len(self._base_path):] or "/"
        async with self._client.stream(
            method, path, headers=headers, content=content, params=params
        ) as resp:
            yield resp