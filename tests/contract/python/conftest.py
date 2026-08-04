"""T37 Python SDK contract tests: shared fixtures (R-P1-49).

Wiring strategy
---------------
The SDK contract tests talk to the **real** ``ProxyServer.app()`` -- the
production aiohttp app with the six exact Responses routes registered before
the ``/v1/{tail:.*}`` catch-all (T38 / R-P1-28).  A ``MockUpstream``
(``tests/support/mock_responses_upstream``) stands in for OpenAI, so every
call exercises the genuine v2/v3 fork in ``ProxyHandler.__call__``, the
production upstream chain (capability routing -> scheduler -> translator ->
terminal persistence) and the store-backed resource endpoints -- while
keeping the tests on the official SDK surface (zero vendor-specific code,
criterion ①) over real HTTP on a random localhost port (zero real network).

Honest labeling: with the mock upstream these are ``mock回放`` results, not a
live-OpenAI ``真机`` run (T38).

Every call goes through the **official** ``openai`` SDK
(:class:`openai.AsyncOpenAI`), never through raw ``httpx``/``aiohttp`` calls.
The only injected pieces are the mock upstream (test scaffolding) and the
access-token row the auth middleware derives the tenant boundary from.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator

import pytest_asyncio
from aiohttp import web

from support.mock_responses_upstream import MockUpstream, UpstreamBehavior, openai_text_json
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store
from zhongzhuan.upstream import UpstreamClient

#: Test-only ``Authorization`` token.  Auth middleware is enabled so the
#: tenant boundary (``token:{id}``) matches the real app.
TEST_API_KEY = "sk-contract-test"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest_asyncio.fixture
async def sdk_env(tmp_path, monkeypatch) -> AsyncIterator[dict]:
    """Stand up the real ``ProxyServer.app()`` on a random port.

    Yields:
        dict with ``client`` (``AsyncOpenAI``), ``base_url`` and ``store`` /
        ``rs`` (for tests that want to inspect persistence directly).
    """
    import os

    from openai import AsyncOpenAI

    from zhongzhuan.config import default_config

    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "true")
    monkeypatch.delenv("ZHONGZHUAN_TIDB_HOST", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PORT", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_USER", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PASSWORD", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_DATABASE", raising=False)

    cfg = default_config()
    cfg.storage.backend = "sqlite"
    cfg.storage.db_path = str(tmp_path / "contract.db")
    cfg.storage.sqlite_db_path = cfg.storage.db_path
    cfg.tidb = None
    store = await create_store(cfg)
    rs = ResponseStore(store)

    # A valid access token: the auth middleware injects ``token_id`` and the
    # v3 workspace is ``token:{token_id}`` (tenant boundary).
    from zhongzhuan.store.access_tokens import create_token

    at = await create_token(store, label="contract", quota_tokens=1_000_000)
    token = at.token
    assert token

    # Mock upstream stands in for OpenAI: default behavior is a deterministic
    # non-stream chat-completions response (the create path translates
    # Responses -> Chat).  Streaming tests configure their own behavior.
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            json_payload=openai_text_json(content="hello from contract mock"),
            stream_payload=None,
        )
    )
    await up.start()

    upstream = UpstreamClient(base_url=up.url, timeout=10.0)
    await upstream.start()

    proxy = ProxyServer(
        upstream_clients={up.url: upstream},
        api_key="sk-upstream",
        keys=[],
        proxy_timeout=10.0,
        store=store,
        responses_bridge=None,  # default enabled
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    base_url = f"http://127.0.0.1:{port}/v1"
    client = AsyncOpenAI(base_url=base_url, api_key=token, max_retries=0, timeout=30.0)

    try:
        yield {
            "client": client,
            "base_url": base_url,
            "store": store,
            "rs": rs,
            "token": token,
            "upstream": up,
        }
    finally:
        await client.close()
        await runner.cleanup()
        await upstream.close()
        await up.stop()
        await store.close()
