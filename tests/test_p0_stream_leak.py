"""P0 回归测试：CLOSE-WAIT 上游连接泄漏（根因 A）与长跑自愈（根因 B 的 P0#2）。

- P0#1：``UpstreamClient.stream()`` 在消费方提前 ``break``/主动 ``aclose()``
  生成器时，必须仍把底层 httpx 响应 ``aclose()`` 掉，绝不把连接留在 pool。
- P0#2：队列扫描 ``_sweep_upstream_pools`` 只在「保留连接数超过上限 **且**
  client 空闲」时重建，绝不打断在途请求。
"""

import asyncio

import pytest
from aiohttp import web

from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.upstream.client import UpstreamClient, _LEAK_POOL_CEILING


@pytest.fixture
async def mock_stream_server():
    """A streaming endpoint that yields chunks slowly, so an early-break
    consumer leaves the body unread."""
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        try:
            for _ in range(100):
                await resp.write(b"data: x\n\n")
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            pass
        return resp

    app = web.Application()
    app.router.add_post("/v1/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.asyncio
async def test_stream_break_still_closes_response(mock_stream_server: str):
    """P0#1：break out of the async-for must still aclose() the underlying resp."""
    client = UpstreamClient(base_url=mock_stream_server, timeout=5.0)
    await client.start()
    try:
        gen = client.stream("POST", "/v1/stream")
        resp = await gen.__anext__()
        assert resp.status_code == 200

        # Track whether the yielded response really gets closed.
        closed_by_generator = asyncio.Event()
        original_aclose = resp.aclose

        async def spied_aclose():
            try:
                await original_aclose()
            finally:
                closed_by_generator.set()

        resp.aclose = spied_aclose
        # Break out without draining the body: the generator's finally must fire.
        await gen.aclose()
        await asyncio.wait_for(closed_by_generator.wait(), timeout=5.0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sweep_rebuilds_only_when_idle_and_over_ceiling():
    """P0#2：只有「超过上限 + 空闲」才重建；在途/未超限一律跳过。"""
    class FakeClient:
        def __init__(self, retained: int, idle: bool):
            self._client = object()  # non-None so the sweep inspects it
            self.retained = retained
            self.idle = idle
            self.rebuilt = 0

        def retained_connection_count(self) -> int:
            return self.retained

        def is_idle(self) -> bool:
            return self.idle

        async def rebuild(self):
            self.rebuilt += 1

    # 用未初始化(仅设 _client_cache)的 handler 实例，直接调类方法。
    handler = object.__new__(ProxyHandler)

    async def run(client, key="https://fake") -> FakeClient:
        handler._client_cache = {key: client}
        await ProxyHandler._sweep_upstream_pools(handler, _LEAK_POOL_CEILING)
        return client

    # 空闲 + 超限 → 重建
    c1 = await run(FakeClient(_LEAK_POOL_CEILING + 1, idle=True))
    assert c1.rebuilt == 1

    # 超限但在途 → 不重建
    c2 = await run(FakeClient(_LEAK_POOL_CEILING + 5, idle=False))
    assert c2.rebuilt == 0

    # 未超限（不管是否空闲）→ 不重建
    c3 = await run(FakeClient(_LEAK_POOL_CEILING, idle=True))
    assert c3.rebuilt == 0