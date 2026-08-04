"""T26: real ``ProxyServer.app()`` integration tests for v3 create.

These tests boot the production aiohttp app (six Responses routes + catch-all)
and drive a real ``POST /v1/responses`` through the single v2/v3 fork point
against the programmable mock upstream.  They prove the create path executes a
REAL upstream request, persists a retrievable terminal resource, honours
capability routing (hosted tools -> standard error, never a fake 200) and keeps
tenant isolation at the store boundary.

Honest labeling: with the mock upstream these are ``mock回放`` results, not a
live-OpenAI ``真机`` run (T38).
"""

from __future__ import annotations

import json
import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from support.mock_responses_upstream import (
    MockUpstream,
    UpstreamBehavior,
    openai_error_json,
    openai_text_json,
)
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.upstream import UpstreamClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _start_proxy(upstream_url: str, store, *, token: str = "") -> tuple[int, web.AppRunner, str]:
    """Boot the production app against ``upstream_url``; return (port, runner, token).

    ``ZHONGZHUAN_PROXY_AUTH`` is enabled so the access-token middleware runs —
    the same tenant boundary the v3 workspace derives from.  A token is minted
    unless the caller supplies one.
    """
    import os

    os.environ["ZHONGZHUAN_PROXY_AUTH"] = "true"
    if not token:
        from zhongzhuan.store.access_tokens import create_token

        token = (await create_token(store, label="it-token", quota_tokens=100000)).token
    upstream = UpstreamClient(base_url=upstream_url, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(
        upstream_clients={upstream_url: upstream},
        api_key="sk-upstream",
        keys=[],
        proxy_timeout=5.0,
        store=store,
        responses_bridge=None,  # default enabled
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return port, runner, upstream, token


@pytest_asyncio.fixture
async def astore(tmp_path, monkeypatch):
    """Async SQLite store fixture (mirrors conftest)."""
    monkeypatch.delenv("ZHONGZHUAN_TIDB_HOST", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PORT", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_USER", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PASSWORD", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_DATABASE", raising=False)
    from zhongzhuan.config import default_config
    from zhongzhuan.store.store import create_store

    cfg = default_config()
    cfg.storage.backend = "sqlite"
    cfg.storage.db_path = str(tmp_path / "test.db")
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    s = await create_store(cfg)
    try:
        yield s
    finally:
        await s.close()


async def _create(port: int, body: dict, token: str) -> tuple[int, dict]:
    async with ClientSession() as sess:
        async with sess.post(
            f"http://127.0.0.1:{port}/v1/responses",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            data=json.dumps(body),
        ) as resp:
            payload = await resp.json()
            return resp.status, payload


@pytest.mark.asyncio
async def test_v3_create_reaches_real_upstream_and_persists(astore):
    """Non-stream create: real upstream call, unified id, terminal retrieve."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="hi v3")))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, obj = await _create(port, {"model": "gpt-4o", "input": "hi"}, token)
        assert status == 200, obj
        assert obj["object"] == "response"
        rid = obj["id"]
        assert rid.startswith("resp_")
        # The upstream received a REAL chat-completions request (translated).
        assert up.request_count >= 1
        req = up.requests[0]
        assert req.path == "/v1/chat/completions"
        assert req.headers.get("Authorization") == "Bearer sk-upstream"

        # The stored resource is retrievable under OUR id, terminal state.
        from zhongzhuan.store.access_tokens import get_token_by_value

        at = await get_token_by_value(astore, token)
        assert at is not None
        ws = f"token:{at.id}"
        rs = ResponseStore(astore)
        rec = await rs.get_response(rid, workspace_id=ws)
        assert rec is not None
        assert rec.status == "completed"
        assert rec.output
        assert rec.usage

        # GET retrieve through the real app returns the completed resource.
        async with ClientSession() as sess:
            async with sess.get(
                f"http://127.0.0.1:{port}/v1/responses/{rid}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["id"] == rid
                assert body["status"] == "completed"
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_v3_create_store_false_does_not_persist(astore):
    """store=false: real upstream call but no retrievable resource."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="no-store")))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, obj = await _create(port, {"model": "gpt-4o", "input": "hi", "store": False}, token)
        assert status == 200, obj
        rid = obj["id"]
        assert rid.startswith("resp_")
        # retrieve -> typed 404
        async with ClientSession() as sess:
            async with sess.get(
                f"http://127.0.0.1:{port}/v1/responses/{rid}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                assert resp.status == 404
                body = await resp.json()
                assert body["error"]["code"] == "not_found"
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_v3_create_hosted_tool_no_executor_is_standard_400(astore):
    """A hosted tool with no executor must NEVER become a fake 200."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, obj = await _create(
            port,
            {
                "model": "gpt-4o",
                "input": "search the web",
                "tools": [{"type": "web_search", "name": "web_search"}],
            },
            token,
        )
        assert status == 400, obj
        # Wire code from responses_errors mapping (ErrorClass -> spec).
        assert obj["error"]["code"] == "unsupported_tool"
        assert obj["error"]["param"] == "tools[0].type"
        # The upstream must NOT have been called.
        assert up.request_count == 0
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_v3_upstream_400_passthrough(astore):
    """Upstream 4xx is returned to the client without a fake 200."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(status=400, error_body=openai_error_json(message="bad input")))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, obj = await _create(port, {"model": "gpt-4o", "input": "x"}, token)
        assert status == 400, obj
        assert "bad input" in obj["error"]["message"]
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_v3_create_chain_error_is_standard(astore):
    """A broken previous_response_id is a standard error, never a downgrade."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, obj = await _create(
            port,
            {"model": "gpt-4o", "input": "follow up", "previous_response_id": "resp_missing"},
            token,
        )
        assert status == 400, obj
        assert "previous_response_id" in obj["error"].get("param", "")
        assert up.request_count == 0
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_v3_retrieve_cross_token_tenant_404(astore):
    """token A creates; token B (different workspace) must get a typed 404."""
    # Insert two access tokens so auth middleware injects distinct workspace ids.
    from zhongzhuan.store.access_tokens import create_token

    tok_a = await create_token(astore, label="tenant-a", quota_tokens=10000)
    tok_b = await create_token(astore, label="tenant-b", quota_tokens=10000)
    assert tok_a.token != tok_b.token

    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="tenant")))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        # token A creates (store=true default)
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/responses",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok_a.token}"},
                data=json.dumps({"model": "gpt-4o", "input": "hi"}),
            ) as resp:
                assert resp.status == 200
                rid = (await resp.json())["id"]

        # token B cannot retrieve it: typed 404, no cross-tenant oracle.
        async with ClientSession() as sess:
            async with sess.get(
                f"http://127.0.0.1:{port}/v1/responses/{rid}",
                headers={"Authorization": f"Bearer {tok_b.token}"},
            ) as resp:
                assert resp.status == 404
                body = await resp.json()
                assert body["error"]["code"] == "not_found"

        # token A can still retrieve it.
        async with ClientSession() as sess:
            async with sess.get(
                f"http://127.0.0.1:{port}/v1/responses/{rid}",
                headers={"Authorization": f"Bearer {tok_a.token}"},
            ) as resp:
                assert resp.status == 200
                assert (await resp.json())["id"] == rid
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()
