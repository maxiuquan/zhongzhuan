"""Pre-commit failure transparent retry tests (2026-10-18).

背景（线上实证，token101/claude-opus-5-5）：上游审核拒绝以「HTTP 200 + 一串
空 content 帧 + 顶层 error 帧（甚至裸 JSON 行，无 data: 前缀）」的合法 SSE
形态出现。旧逻辑照单转发并记 200 完成 0/0 token，客户端收到空流后无声结束
对话。修复后：首个有效负载之前上游字节只持有不转发；拒绝/空完成/首字节前
截断按 pre-commit 失败透明换 key/轮重试（上限 _MAX_PRECOMMIT_RETRIES），
超限显式 SSE error 帧收场。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.handler import _MAX_PRECOMMIT_RETRIES, _scan_upstream_sse
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.upstream import UpstreamClient


# ---------------------------------------------------------------------------
# 单元测试：_scan_upstream_sse
# ---------------------------------------------------------------------------


class TestScanUpstreamSse:
    def test_detects_payload_in_openai_frame(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
        assert p is True and e is None

    def test_payload_split_across_chunks(self):
        buf = bytearray()
        p1, _ = _scan_upstream_sse(buf, b'data: {"choices":[{"delta":{"con')
        assert p1 is False
        p2, _ = _scan_upstream_sse(buf, b'tent":"hi"}}]}\n\n')
        assert p2 is True

    def test_role_only_empty_content_is_not_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n')
        assert p is False and e is None

    def test_bare_json_error_line_no_data_prefix(self):
        buf = bytearray()
        line = json.dumps(
            {"error": {"message": "The request was rejected by the upstream service.", "type": "invalid_request_error"}}
        ).encode()
        p, e = _scan_upstream_sse(buf, line + b"\n\n")
        assert p is False
        assert e is not None and "rejected" in e

    def test_data_prefixed_error_frame(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b'data: {"error":{"message":"boom","type":"server_error"}}\n\n')
        assert p is False and e == "boom"

    def test_error_string_inside_content_not_misdetected(self):
        buf = bytearray()
        chunk = (
            b'data: {"choices":[{"delta":{"content":"raise ValueError(\\"error\\") "}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"try: pass\\nexcept Exception as error: pass"}}]}\n\n'
        )
        p, e = _scan_upstream_sse(buf, chunk)
        assert p is True and e is None

    def test_anthropic_text_delta_is_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n')
        assert p is True and e is None

    def test_anthropic_tool_partial_json_is_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(
            buf,
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"f","input":{}}}\n\n',
        )
        assert p is True and e is None
        buf2 = bytearray()
        p2, _ = _scan_upstream_sse(
            buf2,
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}\n\n',
        )
        assert p2 is True

    def test_responses_native_text_delta_is_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(
            buf,
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hello"}\n\n',
        )
        assert p is True and e is None

    def test_tool_calls_is_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(
            buf,
            b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
        )
        assert p is True and e is None

    def test_usage_only_frame_is_not_payload(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n')
        assert p is False and e is None

    def test_done_and_comments_ignored(self):
        buf = bytearray()
        p, e = _scan_upstream_sse(buf, b": keepalive\n\ndata: [DONE]\n\n")
        assert p is False and e is None


# ---------------------------------------------------------------------------
# 集成测试：拒绝帧重试 / 空完成重试
# ---------------------------------------------------------------------------


def _rejection_sse() -> bytes:
    """token101 拒绝流的 1:1 复刻：空 content 帧 + 裸 JSON 错误行。"""
    frames = b""
    for _ in range(11):
        frames += b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
    frames += json.dumps(
        {
            "error": {
                "message": "The request was rejected by the upstream service. Check the input and tool history.",
                "type": "invalid_request_error",
                "param": "",
                "code": "invalid_request_error",
            }
        }
    ).encode() + b"\n\n"
    return frames


_SUCCESS_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"OK"}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}\n\n'
    b"data: [DONE]\n\n"
)

_EMPTY_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)

MODEL = "juhe/claude-opus-5-5"


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _mk_proxy(upstream_url: str) -> ProxyServer:
    upstream = UpstreamClient(base_url=upstream_url, timeout=10.0)
    k = KeyHealth(
        key_id=360100,
        api_key="sk-test",
        window=SlidingWindow(60, 1000),
        upstream_base=upstream_url,
        model_name=MODEL,
        model_id=1,
    )
    return ProxyServer(upstream_clients={upstream_url: upstream}, keys=[k], proxy_timeout=10.0), upstream, k


async def _run_proxy(proxy: ProxyServer, body: dict) -> tuple[int, bytes, float]:
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
                json=body,
            ) as resp:
                raw = await resp.read()
                status = resp.status
        return status, raw, time.monotonic() - start
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_rejection_frame_then_success_retries(monkeypatch):
    """前 2 次拒绝（持流不透传），第 3 次成功 → 客户端拿到内容，无错误帧。"""
    hits = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["n"] += 1
        if hits["n"] <= 2:
            return web.Response(status=200, body=_rejection_sse(), headers={"Content-Type": "text/event-stream"})
        return web.Response(status=200, body=_SUCCESS_SSE, headers={"Content-Type": "text/event-stream"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        proxy, upstream, _k = _mk_proxy(upstream_url)
        await upstream.start()
        try:
            status, raw, elapsed = await _run_proxy(
                proxy, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
            )
        finally:
            await upstream.close()
        assert status == 200
        assert b'"content":"OK"' in raw, raw[:500]
        assert b"rejected by the upstream service" not in raw  # 拒绝帧不得透传
        assert hits["n"] == 3, f"expected exactly 3 upstream hits, got {hits['n']}"
        assert elapsed < 60.0, f"took {elapsed:.1f}s"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_persistent_rejection_fails_with_explicit_error():
    """持续拒绝：_MAX_PRECOMMIT_RETRIES 次后显式 error 帧 + [DONE] 收场。"""
    hits = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["n"] += 1
        return web.Response(status=200, body=_rejection_sse(), headers={"Content-Type": "text/event-stream"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        proxy, upstream, _k = _mk_proxy(upstream_url)
        await upstream.start()
        try:
            status, raw, elapsed = await _run_proxy(
                proxy, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
            )
        finally:
            await upstream.close()
        assert status == 200  # SSE 头已落锤，错误以帧承载
        assert b"event: error" in raw, raw[:500]
        assert b"content audit window" in raw, raw[:500]
        assert b"data: [DONE]" in raw
        assert hits["n"] == _MAX_PRECOMMIT_RETRIES, hits["n"]
        assert elapsed < 90.0, f"took {elapsed:.1f}s (must not hang to hard deadline)"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_empty_completion_retries_then_error():
    """空完成（有帧无内容无 usage）：同样按 pre-commit 失败处理。"""
    hits = {"n": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["n"] += 1
        return web.Response(status=200, body=_EMPTY_SSE, headers={"Content-Type": "text/event-stream"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        proxy, upstream, _k = _mk_proxy(upstream_url)
        await upstream.start()
        try:
            status, raw, elapsed = await _run_proxy(
                proxy, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
            )
        finally:
            await upstream.close()
        assert status == 200
        assert b"event: error" in raw
        assert b"empty completions" in raw
        assert hits["n"] == _MAX_PRECOMMIT_RETRIES, hits["n"]
        assert elapsed < 90.0
    finally:
        await runner.cleanup()
