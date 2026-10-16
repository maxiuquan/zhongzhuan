"""全候选 invalid 快速失败回归测试（2026-10-17）。

背景（线上实证）：vsllm/claude-opus-5-5 的唯一 key 360017 被上游 401
"Invalid token" 标 invalid 后，in-flight 流式请求后续每轮 ``all 0 key(s)
failed this round, retrying in 30s`` 空转——候选池空了但循环不停，一直挂到
900s 硬死线才收场（客户端白等 15 分钟）。

根因：``scheduler.score()`` 对 invalid/冷却 key 返 None → ``pick_key`` 返
None → 轮尾既不触发熔断（无异常）、也不触发 degraded（请求开始时 key 还
健康）→ 落入既有退避循环。invalid 是 permanent 状态，等时间不会自愈。

修复：``_stream_proxy`` 轮尾检测 ``reason_for_exhaustion(candidates) ==
"all_invalid"`` → 立即写 SSE error 帧（``all keys invalid (401/402/
permanent), awaiting manual recovery``）并终止，不再退避重试。

本测试：单 key 渠道 + 上游恒返 401。断言：
1. 请求秒级结束（旧行为会挂 900s）；
2. 上游恰好被打 1 次（invalid 后不再重试）；
3. 客户端收到明确的 ``all keys invalid`` 错误帧。
"""

from __future__ import annotations

import socket
import time

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.upstream import UpstreamClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
async def upstream_401():
    """上游对所有 /v1/chat/completions 返回 401 Invalid token。"""
    hits = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["count"] += 1
        return web.Response(
            status=401,
            text='{"error":{"code":"","message":"Invalid token (request id: test)","type":"new_api_error"}}',
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


@pytest.mark.asyncio
async def test_stream_all_invalid_fails_fast(upstream_401):
    """唯一 key 被 401 标 invalid 后，请求必须秒级失败，不得空转到硬死线。"""
    base_url, hits = upstream_401
    upstream = UpstreamClient(base_url=base_url, timeout=5.0)
    await upstream.start()

    model = "vsllm/claude-opus-5-5"
    k = KeyHealth(
        key_id=360017,
        api_key="sk-1",
        window=SlidingWindow(60, 1000),
        upstream_base=base_url,
        model_name=model,
        model_id=1,
    )
    proxy = ProxyServer(
        upstream_clients={base_url: upstream},
        keys=[k],
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
                json={"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as resp:
                raw = await resp.read()
                status = resp.status
        elapsed = time.monotonic() - start

        # 流式响应状态码始终是 200（SSE 头已落锤），错误以 SSE 帧承载。
        assert status == 200, (status, raw)
        # 必须显式告知客户端：所有 key 已 invalid、等管理端恢复，而非空转超时。
        assert b"event: error" in raw, raw
        assert b"all keys invalid" in raw, raw
        # invalid 不自愈：上游只应被打 1 次（旧行为会每 30s 空转重试）。
        assert hits["count"] == 1, f"upstream hit {hits['count']} times, expected exactly 1"
        # 旧行为挂 900s；修复后秒级收场。
        assert elapsed < 10.0, f"request hung for {elapsed:.1f}s, expected < 10s"
    finally:
        await runner.cleanup()
        await upstream.close()
