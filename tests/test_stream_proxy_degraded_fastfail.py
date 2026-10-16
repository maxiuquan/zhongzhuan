"""降级流式快速失败回归测试。

背景（真实故障，2026-08-22）：``juhe/mimo-v2.5-pro`` 组内所有 key 都处于冷却
状态，``_resolve_candidates`` 返回空，``__call__`` 降级选中一把冷却最短的 key
（300168）强行试一次。这把 key 对上游持续返回 554，而 ``_stream_proxy`` 的
重试循环会反复重试同一把降级 key，导致请求一直挂到 15 分钟的墙钟硬死线才
返回 504，Pi Agent 后台出现大量 ``stream hard deadline 900s exceeded``。

修复：``_stream_proxy`` 在整轮失败后检查 ``degraded_mode``；如果是降级模式，
说明唯一的降级 key 已经失败、组内没有其他可用候选，应立即失败返回，而不是
进入退避/死线循环。

2026-10-16 更新（单 key 渠道激进重试）：degraded 失败后若该 key 的新冷却
剩余 ≤ 30s（单 key 渠道瞬时失败封顶 10s），等到期**透明重试一次**再快速
失败——所以现在的契约是「恰好重试 2 次、总耗时 ≈ 10s（封顶 30s）内结束」，
而不是旧的一次失败立即 503。

本测试把 key 打冷却并绑定到指定模型，使请求触发降级路径；上游返回 554 后断言
请求在数秒内结束、上游恰好被打 2 次、客户端收到显式 ``all keys cooling`` 错误帧。
"""

from __future__ import annotations

import socket
import time

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import STATE_RATE_LIMITED, KeyHealth, SlidingWindow
from zhongzhuan.proxy.retry import mark_rate_limited
from zhongzhuan.upstream import UpstreamClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
async def upstream_554():
    """上游对所有 /v1/chat/completions 返回 554（网关错误），并统计命中次数。"""
    hits = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["n"] += 1
        return web.Response(status=554, text="upstream gateway error")

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
async def test_stream_proxy_degraded_key_fails_fast(upstream_554):
    """全组冷却的降级 key 失败后：等待重试一次（有界），随后立即失败，不得挂到硬死线。"""
    base_url, hits = upstream_554
    upstream = UpstreamClient(base_url=base_url, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(
        upstream_clients={base_url: upstream},
        api_key="sk-1",
        proxy_timeout=5.0,
    )

    model = "juhe/mimo-v2.5-pro"
    # 构造一个绑定到目标模型且处于 rate-limited（冷却中）的 key，这样
    # _resolve_candidates 为空，触发 _resolve_degraded 降级路径。
    k = KeyHealth(
        key_id=300168,
        api_key="sk-1",
        window=SlidingWindow(60, 1000),
        upstream_base=base_url,
        model_name=model,
        model_id=1,
    )
    mark_rate_limited(k, retry_after=3600)
    assert k.status == STATE_RATE_LIMITED
    assert k.cooldown_until > time.time()

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
        # 必须显式告知客户端：所有 key 都在冷却，而不是挂到死线才 504。
        assert b"event: error" in raw, raw
        assert b"all keys cooling" in raw, raw
        # 2026-10-16 契约：首次失败后冷却剩余 10s（单 key 封顶档），等待重试
        # 恰好一次 → 上游共被打 2 次；随后二次失败快速失败。
        assert hits["n"] == 2, f"expected exactly 2 upstream attempts, got {hits['n']}"
        # 旧行为会挂 15 分钟（900s）；等待重试一次后应仍以秒级（~10s）结束。
        assert 5.0 <= elapsed < 25.0, f"degraded request took {elapsed:.1f}s, expected ~10s (bounded)"
    finally:
        await runner.cleanup()
        await upstream.close()
