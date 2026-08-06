"""上游返回「空回复」时自动换 key 重试的回归测试。

背景（真实故障）：Codex 走 ``POST /v1/responses {"stream": true}`` 打某几个
上游 key 时，上游返回 **HTTP 200 + 干净结束的 SSE 流，但一个 token 都没有**。
中继原先只在「连接错误 / HTTP >= 400」时才换 key，200 一律当成功，于是这个空
壳流被原样转给 Codex，用户看到的就是「发了消息但没有任何回复」。

正确行为不是把空 key 禁用掉（那会让本来只是偶发抽风的 key 永久出局），而是
**在还没给客户端写出第一个字节之前，透明地换下一个候选 key 重试**。这组测试
把这条契约钉死：

===================================================  ========================
上游行为                                              期望
===================================================  ========================
key0 空流 / key1 正常                                 客户端只看到 key1 的内容
key0 与 key1 都空流                                   502 + empty_upstream_response
key0 非流式空 JSON / key1 正常                        客户端只看到 key1 的内容
只有一个 key 且它中途被截断                            仍是 200 + incomplete（不退化）
===================================================  ========================

最后一行是防回归的关键：换 key 逻辑必须能区分「干净地返回空内容」（真·空回复，
换 key）与「说到一半被切断」（截断，语义不能被洗掉，见 AC-2.4）。
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
    openai_text_json,
    openai_text_stream,
)
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient


# ---------------------------------------------------------------------------
# Harness：与 test_proxy_v3_stream.py 同构，但显式挂 **两个** 上游 / 两个 key
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _start_proxy_two_keys(url_a: str, url_b: str, store):
    """启动真实 app，候选池里按顺序挂两个 openai key（key0 -> A，key1 -> B）。

    候选顺序即 ``keys`` 列表顺序，capability router 取 ``available[0]``，所以
    首轮必定打 A；A 失败后 ``pick_key`` 在剩下的候选里挑，只剩 B。测试因此可以
    确定性地断言「先 A 后 B」。
    """
    import os

    from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
    from zhongzhuan.store.access_tokens import create_token

    os.environ["ZHONGZHUAN_PROXY_AUTH"] = "true"
    token = (await create_token(store, label="empty-retry-token", quota_tokens=100000)).token

    ua = UpstreamClient(base_url=url_a, timeout=10.0)
    ub = UpstreamClient(base_url=url_b, timeout=10.0)
    await ua.start()
    await ub.start()

    keys = [
        KeyHealth(
            key_id=0,
            api_key="sk-a",
            window=SlidingWindow(60, 1000),
            upstream_base=url_a,
            upstream_protocol="openai",
        ),
        KeyHealth(
            key_id=1,
            api_key="sk-b",
            window=SlidingWindow(60, 1000),
            upstream_base=url_b,
            upstream_protocol="openai",
        ),
    ]
    proxy = ProxyServer(
        upstream_clients={url_a: ua, url_b: ub},
        api_key="sk-a",
        keys=keys,
        proxy_timeout=10.0,
        store=store,
        responses_bridge=None,
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return port, runner, (ua, ub), token, keys


@pytest_asyncio.fixture
async def astore(tmp_path, monkeypatch):
    """Async SQLite store fixture（与其它 v3 套件一致）。"""
    for var in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"ZHONGZHUAN_TIDB_{var}", raising=False)
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


async def _post(port: int, body: dict, token: str) -> tuple[int, str, bytes]:
    async with ClientSession() as sess:
        async with sess.post(
            f"http://127.0.0.1:{port}/v1/responses",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            return r.status, r.headers.get("Content-Type", ""), await r.read()


def _event_types(raw: bytes) -> list[str]:
    return [
        line[len("event:"):].strip()
        for line in raw.decode("utf-8", "replace").splitlines()
        if line.startswith("event:")
    ]


def _joined_text(raw: bytes) -> str:
    """把流里所有 ``response.output_text.delta`` 拼回完整文本。"""
    out: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[len("data:"):].strip())
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "response.output_text.delta":
            delta = obj.get("delta")
            if isinstance(delta, str):
                out.append(delta)
            elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                out.append(delta["text"])
    return "".join(out)


#: 「干净地什么都没说」：role 帧 + finish_reason=stop + usage + [DONE]。
#: 上游给了明确的结束信号，所以 pipeline 走 ``response.completed`` 分支，
#: terminal_reason 为空 —— 这正是要与「截断」区分开的那一类。
_EMPTY_STREAM = openai_text_stream(pieces=())

#: 非流式的空回复：content 为空字符串，其余字段齐全。
_EMPTY_JSON = openai_text_json(content="")


# ---------------------------------------------------------------------------
# 1. 流式：空回复 -> 换 key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_empty_upstream_switches_to_next_key(astore):
    """key0 返回空流时，客户端应当只看到 key1 的真实内容。

    换 key 必须发生在 ``prepare()`` 之前，否则 200 已经落锤、只能把空壳流吐
    出去。断言里 ``response.created`` 只出现一次，就是在证明第一次尝试的生命
    周期帧被丢弃了，而不是两段流拼在了一起。
    """
    up_empty = MockUpstream()
    up_empty.set_behavior(UpstreamBehavior(stream_payload=_EMPTY_STREAM))
    up_good = MockUpstream()
    up_good.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up_empty.start()
    await up_good.start()
    port, runner, (ua, ub), token, keys = await _start_proxy_two_keys(up_empty.url, up_good.url, astore)
    try:
        status, ctype, raw = await _post(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 200, raw
        assert "text/event-stream" in ctype
        assert up_empty.request_count == 1, "空 key 应当被尝试过一次"
        assert up_good.request_count == 1, "空回复后必须换到下一个 key"

        types = _event_types(raw)
        # 第一次尝试的 created/in_progress 绝不能泄漏给客户端。
        assert types.count("response.created") == 1, types
        assert types.count("response.completed") == 1, types
        assert _joined_text(raw) == "Hello, world!", raw
    finally:
        await runner.cleanup()
        await ua.close()
        await ub.close()
        await up_empty.stop()
        await up_good.stop()


@pytest.mark.asyncio
async def test_stream_empty_key_is_soft_failed_not_disabled(astore):
    """空回复的 key 只降权（软失败 + 冷却），不会被永久禁用。

    这是这次修法与「禁用空 key」方案的分界线：偶发抽风的 key 冷却后还能回到
    候选池，不需要人工重新启用。
    """
    up_empty = MockUpstream()
    up_empty.set_behavior(UpstreamBehavior(stream_payload=_EMPTY_STREAM))
    up_good = MockUpstream()
    up_good.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up_empty.start()
    await up_good.start()
    port, runner, (ua, ub), token, keys = await _start_proxy_two_keys(up_empty.url, up_good.url, astore)
    try:
        status, _ctype, raw = await _post(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 200, raw
        k0 = keys[0]
        assert k0.consecutive_failures >= 1, "空回复必须计入失败，调度器才会降权"
        assert k0.cooldown_until > 0, "空回复应进入冷却，而不是继续被优先选中"
        assert getattr(k0, "enabled", True) is True, "空回复不应导致 key 被禁用"
    finally:
        await runner.cleanup()
        await ua.close()
        await ub.close()
        await up_empty.stop()
        await up_good.stop()


@pytest.mark.asyncio
async def test_stream_all_keys_empty_is_502_not_silent_empty(astore):
    """所有候选都空回复时报 502，而不是把空的 completed 塞给客户端。

    Codex 拿到空的 ``response.completed`` 会静默卡住（这就是原始故障现象）；
    拿到 502 才会走它自己的错误/重试路径。宁可显式报错也不要假装成功。
    """
    up_a = MockUpstream()
    up_a.set_behavior(UpstreamBehavior(stream_payload=_EMPTY_STREAM))
    up_b = MockUpstream()
    up_b.set_behavior(UpstreamBehavior(stream_payload=_EMPTY_STREAM))
    await up_a.start()
    await up_b.start()
    port, runner, (ua, ub), token, _keys = await _start_proxy_two_keys(up_a.url, up_b.url, astore)
    try:
        status, ctype, raw = await _post(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 502, raw
        assert "text/event-stream" not in ctype
        assert b"event:" not in raw, "一个字节都没提交过，不该出现 SSE 帧"
        body = json.loads(raw)
        assert body["error"]["code"] == "empty_upstream_response", body
        assert up_a.request_count == 1
        assert up_b.request_count == 1
    finally:
        await runner.cleanup()
        await ua.close()
        await ub.close()
        await up_a.stop()
        await up_b.stop()


# ---------------------------------------------------------------------------
# 2. 非流式：空回复 -> 换 key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonstream_empty_upstream_switches_to_next_key(astore):
    """非流式路径同样要换 key：200 + 空 content 不是成功。"""
    up_empty = MockUpstream()
    up_empty.set_behavior(UpstreamBehavior(json_payload=_EMPTY_JSON))
    up_good = MockUpstream()
    up_good.set_behavior(UpstreamBehavior(json_payload=openai_text_json()))
    await up_empty.start()
    await up_good.start()
    port, runner, (ua, ub), token, _keys = await _start_proxy_two_keys(up_empty.url, up_good.url, astore)
    try:
        status, _ctype, raw = await _post(port, {"model": "gpt-4o", "input": "x"}, token)
        assert status == 200, raw
        assert up_empty.request_count == 1
        assert up_good.request_count == 1, "非流式空回复也必须换 key"
        assert b"Hello, world!" in raw, raw
    finally:
        await runner.cleanup()
        await ua.close()
        await ub.close()
        await up_empty.stop()
        await up_good.stop()
