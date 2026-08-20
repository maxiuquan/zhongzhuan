"""根因 B 回归测试：流式重试循环必须有墙钟硬死线。

背景（真实故障，2026-08-20/21）：Pi Agent 走 ``POST /v1/chat/completions``
``stream=true`` 打某模型，上游持续返回 554/503/429。``_stream_proxy`` 对这些
**状态码类**失败按设计落入退避重试循环，但对状态码失败的熔断分支（first_exc_type）
不触发，于是循环无限进行、把 SSE 连接永久挂起（零字节）。客户端（Pi）看到的
就是「又断了」。

修复：在「整轮全部失败后、准备再次退避重试」前检查墙钟死线
``STREAM_HARD_DEADLINE_SECONDS``，超时则走与熔断相同的失败路径
（写 SSE error 事件 + ``_log_gate_failure`` 审计 + 返回），快速失败而非挂死。

本测试把死线压到 0.2s，断言：上游持续 554 时，请求在数秒内结束且客户端收到
显式 SSE error 帧，而不再无限重试。
"""

from __future__ import annotations

import socket
import time

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest_asyncio.fixture
async def upstream_554():
    """上游对所有 /v1/chat/completions 返回 554（网关错误）。"""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=554, text="upstream gateway error")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.asyncio
async def test_stream_proxy_perpetual_5xx_hits_hard_deadline(upstream_554, monkeypatch):
    """上游持续 554 时，_stream_proxy 必须在墙钟死线内放弃并给客户端显式错误。"""
    import zhongzhuan.proxy.handler as handler_mod

    # 把死线压到 0.2s 让测试秒级完成（默认 600s）。
    monkeypatch.setattr(handler_mod, "STREAM_HARD_DEADLINE_SECONDS", 0.2)

    upstream = UpstreamClient(base_url=upstream_554, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(
        upstream_clients={upstream_554: upstream},
        api_key="sk-1",
        proxy_timeout=5.0,
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        start = time.monotonic()
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as resp:
                raw = await resp.read()
                status = resp.status
        elapsed = time.monotonic() - start

        # 流式响应始终是 200（SSE 头已落锤），错误以 SSE 帧承载。
        assert status == 200, (status, raw)
        # 关键：客户端必须收到显式错误帧，而不是永远拿不到任何字节。
        assert b"event: error" in raw, raw
        assert b"upstream temporarily unavailable" in raw, raw
        # 关键：必须在死线内结束，绝不能无限重试（旧行为会卡 19+ 分钟）。
        assert elapsed < 15.0, f"request hung for {elapsed:.1f}s, expected < 15s"
    finally:
        await runner.cleanup()
        await upstream.close()
