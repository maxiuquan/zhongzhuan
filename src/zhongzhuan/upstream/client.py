"""Upstream HTTP client: httpx.AsyncClient wrapper."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
from httpx import Timeout as HttpxTimeout

from ..config.timeouts import DEFAULT_TIMEOUT_POLICY, TimeoutPolicy

#: Soft ceiling above which an upstream client's retained pool is considered
#: leaked/accumulated and subject to a forced idle flush (P0#2). Mirrors the
#: keepalive cap configured on the httpx client below.
_LEAK_POOL_CEILING: int = 64

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
        url = self._resolve_url(path)
        return await self._client.request(
            method,
            url,
            headers=headers,
            content=content,
            params=params,
        )

    def _resolve_url(self, path: str) -> str:
        """把请求路径解析为**绝对 URL**（显式拼接，不依赖 httpx base_url 合并）。

        httpx 的 ``base_url`` 合并对以 ``/`` 开头的 path 做**绝对路径替换**（整段
        覆盖 base 的 path），对 base 含多段前缀的 key 会丢前缀：
        ``base='https://host/api/agents/v1'`` + ``path='/v1/responses'`` ->
        ``https://host/v1/responses``（2026-08-15 实测 p0/deepseek-v4-flash 因此
        打到错误路径、被上游 Cloudflare 404/403 拦截）。相对 path 则是**直接拼接**
        （``.../api/agents/v1`` + ``v1/responses`` -> ``.../api/agents/v1/responses``）。

        修复：path 转相对 + base 尾段与 path 首段去重（``.../v1`` 与 ``/v1/...``
        不再重复成 ``/v1/v1/``），显式拼出绝对 URL 交给 httpx。
        """
        base_r = self.base_url.rstrip("/")
        path_l = (path or "").lstrip("/")
        base_last = self._base_path.rsplit("/", 1)[-1] if self._base_path else ""
        if base_last and path_l.startswith(base_last + "/"):
            path_l = path_l[len(base_last) :].lstrip("/")
        return base_r + "/" + path_l if path_l else base_r

    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Yield an httpx streaming response.

        The ``async with`` context manager guarantees that the underlying
        connection is returned to the pool when the body exits normally, but
        when the consumer ``break``s or an exception propagates through the
        generator, Python may defer ``aclose()`` until garbage collection —
        by which time the peer has already sent FIN and the socket is stuck
        in CLOSE-WAIT, leaking a pool slot indefinitely.

        Fix (2026-09-02): wrap the httpx stream in an explicit
        ``try/finally`` with ``aclose()``.  Even if the consumer breaks
        out of the ``async for`` loop, the generator's ``finally`` block
        runs immediately during ``aclose()``, ensuring the connection is
        released back to the pool *now*, not at GC time.
        """
        if self._client is None:
            await self.start()
        assert self._client is not None
        url = self._resolve_url(path)
        resp = await self._client.send(
            self._client.build_request(method, url, headers=headers, content=content, params=params),
            stream=True,
        )
        try:
            yield resp
        finally:
            await resp.aclose()

    # ---- P0#2: connection-pool self-heal (long-uptime leak detection) ----
    #
    # Even with the ``really-close`` fix above, a process that runs for days
    # can still accumulate stray CLOSE-WAIT sockets if some path ever leaves a
    # response un-:meth:`aclose`\\d.  These methods let a periodic background
    # sweep introspect the underlying httpcore pool and force a full client
    # rebuild once the retained pool visibly exceeds the healthy ceiling.
    # Introspection is deliberately defensive: probing private transport
    # internals must degrade to "no leak" instead of raising on a new httpx.

    def _pool_connections(self) -> list:
        """Best-effort list of retained httpcore connections, or ``[]``.

        httpx >=0.28 keeps its multiplexer pool at ``client._transport._pool``
        (an ``AsyncConnectionPool``) whose ``connections`` property exposes the
        live sockets.  Any version/attribute drift returns an empty list, which
        disables auto-rebuild without breaking request serving.
        """
        client = self._client
        if client is None:
            return []
        transport = getattr(client, "_transport", None)
        pool = getattr(transport, "_pool", None)
        conns = getattr(pool, "connections", None)
        return list(conns) if isinstance(conns, list) else []

    def retained_connection_count(self) -> int:
        """Number of connections currently held by the upstream pool."""
        return len(self._pool_connections())

    def is_idle(self) -> bool:
        """True if no pooled connection is actively serving a request.

        A connection is considered busy unless it reports itself idle or
        already closed.  Used to guarantee we never rebuild a client while
        requests are mid-flight on it.
        """
        for conn in self._pool_connections():
            try:
                busy = not (bool(conn.is_idle()) or bool(conn.is_closed()))
            except Exception:  # noqa: BLE001 - introspection drift
                busy = True
            if busy:
                return False
        return True

    async def rebuild(self) -> None:
        """Drop the underlying httpx client so the next request opens a fresh one.

        Closing the old client tears down every retained socket (releasing any
        CLOSE-WAIT that the pool kept alive).  Must only be called when
        :meth:`is_idle` is true, otherwise in-flight requests would be killed.
        """
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
