"""Upstream HTTP client: httpx.AsyncClient wrapper."""

from __future__ import annotations

from typing import AsyncIterator

import httpx
from httpx import Timeout as HttpxTimeout

from ..config.timeouts import DEFAULT_TIMEOUT_POLICY, TimeoutPolicy

# Legacy default kept for the deprecated ``timeout: float`` call shape.
LEGACY_WRITE_TIMEOUT: float = 30.0


def _sanitize_url(url: str) -> str:
    """Remove backticks and other common formatting artifacts from URLs."""
    return url.replace("`", "").replace('"', "").strip()


def _legacy_httpx_timeout(timeout: float, connect_timeout: float) -> HttpxTimeout:
    """Map the deprecated single ``timeout`` float onto httpx.

    The float is the read budget, i.e. it plays the role of both
    ``read_idle_seconds`` and ``total_seconds`` in the six-layer policy.  It is
    intentionally *not* routed through :class:`TimeoutPolicy` so that callers
    passing small values (tests, scripts) keep working unchanged - the 300s
    floors only apply to configuration-driven policies.
    """
    return HttpxTimeout(
        timeout,  # overall read timeout (for slow AI model responses)
        connect=connect_timeout,  # connect timeout
        pool=connect_timeout,  # pool timeout
        write=LEGACY_WRITE_TIMEOUT,
    )


def _policy_httpx_timeout(policy: TimeoutPolicy) -> HttpxTimeout:
    """Map the six-layer :class:`TimeoutPolicy` onto httpx's four knobs."""
    return HttpxTimeout(
        policy.read_timeout_seconds,  # max(first_token, read_idle)
        connect=policy.connect_seconds,
        pool=policy.pool_seconds,
        write=policy.write_seconds,
    )


class UpstreamClient:
    """httpx.AsyncClient wrapper with a layered timeout policy.

    Two call shapes are supported:

    * ``UpstreamClient(base_url, timeouts=policy)`` - preferred (T01).
    * ``UpstreamClient(base_url, timeout=30.0)`` - deprecated single float,
      kept for backwards compatibility with existing callers and tests.

    When neither is given the process-wide default policy is used, which means
    a 600s first-token budget instead of the old 30s.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float | None = None,
        connect_timeout: float = 15.0,
        *,
        timeouts: TimeoutPolicy | None = None,
    ) -> None:
        base_url = _sanitize_url(base_url)
        self.base_url = base_url.rstrip("/")
        # Extract path prefix from base_url (e.g., "/v1" from "https://api.example.com/v1")
        # to avoid duplicating it when the request path also starts with the same prefix.
        from urllib.parse import urlparse

        self._base_path = urlparse(self.base_url).path.rstrip("/")

        if timeouts is not None:
            self.timeouts: TimeoutPolicy = timeouts
            self.total_seconds: float = timeouts.total_seconds
            self._timeout = _policy_httpx_timeout(timeouts)
        elif timeout is not None:
            # Deprecated shape: the float is the read budget AND the total budget.
            self.timeouts = DEFAULT_TIMEOUT_POLICY
            self.total_seconds = float(timeout)
            self._timeout = _legacy_httpx_timeout(float(timeout), connect_timeout)
        else:
            self.timeouts = DEFAULT_TIMEOUT_POLICY
            self.total_seconds = DEFAULT_TIMEOUT_POLICY.total_seconds
            self._timeout = _policy_httpx_timeout(DEFAULT_TIMEOUT_POLICY)

        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                trust_env=False,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=50, keepalive_expiry=60.0),
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
            path = path[len(self._base_path) :] or "/"
        return await self._client.request(
            method,
            path,
            headers=headers,
            content=content,
            params=params,
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
            path = path[len(self._base_path) :] or "/"
        async with self._client.stream(method, path, headers=headers, content=content, params=params) as resp:
            yield resp
