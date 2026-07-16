"""Upstream HTTP client: httpx.AsyncClient wrapper."""
from __future__ import annotations

from typing import AsyncIterator

import httpx


def _sanitize_url(url: str) -> str:
    """Remove backticks and other common formatting artifacts from URLs."""
    return url.replace("`", "").replace('"', '').strip()


class UpstreamClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        base_url = _sanitize_url(base_url)
        self.base_url = base_url.rstrip("/")
        # Extract path prefix from base_url (e.g., "/v1" from "https://api.example.com/v1")
        # to avoid duplicating it when the request path also starts with the same prefix.
        from urllib.parse import urlparse
        self._base_path = urlparse(self.base_url).path.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            # Use a custom transport with a generous keepalive window.
            #
            # Why: httpx's default keepalive_expiry is 5.0s. Between user turns
            # (typing, thinking, reading output), idle gaps routinely exceed
            # 5s — after which httpx closes the pooled connection and the next
            # request has to redo DNS (227ms on this VPS) + TCP + TLS handshake
            # from scratch. Raising to 120s lets idle connections survive
            # between turns, eliminating repeated handshake cost.
            #
            # DNS itself is handled by the OS resolver. If upstream DNS is
            # consistently slow, the proper fix is at the OS level
            # (/etc/resolv.conf -> 1.1.1.1 / 8.8.8.8) rather than in-process.
            transport = httpx.AsyncHTTPTransport(
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=50,
                    keepalive_expiry=120.0,
                ),
            )
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                trust_env=False,
                transport=transport,
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
