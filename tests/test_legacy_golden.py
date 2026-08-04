"""Golden fixtures for the legacy protocol links (T08).

Each parameterized case:

1. starts a *real* ProxyServer against a programmable MockUpstream whose
   deterministic payload is defined in ``tests/support/mock_responses_upstream.py``;
2. POSTs the marked request body through the proxy;
3. captures the exact response bytes the proxy returns to the client;
4. normalizes volatile fields (proxy-generated ids / timestamps) with
   :func:`normalize_for_golden`, exactly like the golden generator did;
5. asserts the normalized bytes equal the checked-in ``tests/golden/legacy/*.sse``
   file byte-for-byte, and the HTTP status equals the sibling ``.status`` file.

These fixtures are the byte-level baseline that guards the T13 legacy
relocation: any change to the legacy proxy's wire behavior (even a reordering
of fields or a whitespace change) fails the golden comparison.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient

from support.mock_responses_upstream import (
    MockUpstream,
    UpstreamBehavior,
    anthropic_error_json,
    anthropic_text_json,
    anthropic_text_stream,
    anthropic_tool_stream,
    openai_error_json,
    openai_text_json,
    openai_text_stream,
    openai_tool_stream,
)
from support.sse_assert import normalize_for_golden

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "legacy"

# (fixture, path, behavior, request_body, expected_status)
_CASES: list[tuple[str, str, UpstreamBehavior, dict, int]] = [
    # ---- Chat -> Chat (OpenAI passthrough, no translation) ----
    (
        "chat2chat_text_stream",
        "/v1/chat/completions",
        UpstreamBehavior(stream_payload=openai_text_stream()),
        {"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "chat2chat_tool_stream",
        "/v1/chat/completions",
        UpstreamBehavior(stream_payload=openai_tool_stream()),
        {
            "model": "x",
            "stream": True,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
        },
        200,
    ),
    (
        "chat2chat_text_json",
        "/v1/chat/completions",
        UpstreamBehavior(json_payload=openai_text_json()),
        {"model": "x", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "chat2chat_error_json",
        "/v1/chat/completions",
        UpstreamBehavior(status=400, error_body=openai_error_json()),
        {"model": "x", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        400,
    ),
    # ---- Chat -> Anthropic (OpenAI client -> Anthropic upstream) ----
    (
        "chat2anthropic_text_stream",
        "/v1/chat/completions",
        UpstreamBehavior(stream_payload=anthropic_text_stream()),
        {"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "chat2anthropic_tool_stream",
        "/v1/chat/completions",
        UpstreamBehavior(stream_payload=anthropic_tool_stream()),
        {
            "model": "x",
            "stream": True,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
        },
        200,
    ),
    (
        "chat2anthropic_text_json",
        "/v1/chat/completions",
        UpstreamBehavior(json_payload=anthropic_text_json()),
        {"model": "x", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "chat2anthropic_error_json",
        "/v1/chat/completions",
        UpstreamBehavior(status=429, error_body=anthropic_error_json()),
        {"model": "x", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        429,
    ),
    # ---- Anthropic -> Chat (Anthropic client -> OpenAI upstream) ----
    (
        "anthropic2chat_text_stream",
        "/v1/messages",
        UpstreamBehavior(stream_payload=openai_text_stream()),
        {"model": "x", "stream": True, "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "anthropic2chat_tool_stream",
        "/v1/messages",
        UpstreamBehavior(stream_payload=openai_tool_stream()),
        {
            "model": "x",
            "stream": True,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
        },
        200,
    ),
    (
        "anthropic2chat_text_json",
        "/v1/messages",
        UpstreamBehavior(json_payload=openai_text_json()),
        {"model": "x", "stream": False, "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
        200,
    ),
    (
        "anthropic2chat_error_json",
        "/v1/messages",
        UpstreamBehavior(status=400, error_body=openai_error_json()),
        {"model": "x", "stream": False, "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
        400,
    ),
]

_CASE_IDS = [c[0] for c in _CASES]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest_asyncio.fixture
async def _proxy_harness():
    """Yield a factory that starts proxy+upstream and returns a client."""
    _cleanup: list = []

    async def _make(behavior: UpstreamBehavior) -> tuple[str, str]:
        up = MockUpstream()
        up.set_behavior(behavior)
        url = await up.start()
        client = UpstreamClient(base_url=url, timeout=10.0)
        await client.start()
        proxy = ProxyServer(
            upstream_clients={url: client},
            api_key="sk-test",
            proxy_timeout=10.0,
        )
        port = _free_port()
        runner = web.AppRunner(proxy.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        proxy_url = f"http://127.0.0.1:{port}"
        _cleanup.append((runner, client, up))
        return proxy_url, url

    yield _make

    for runner, client, up in _cleanup:
        try:
            await runner.cleanup()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass
        try:
            await up.stop()
        except Exception:
            pass


@pytest.mark.parametrize("case_id,path,behavior,body,expected_status", _CASES, ids=_CASE_IDS)
@pytest.mark.asyncio
@pytest.mark.golden
async def test_legacy_golden(case_id, path, behavior, body, expected_status, _proxy_harness):
    golden = GOLDEN_DIR / f"{case_id}.sse"
    status_file = GOLDEN_DIR / f"{case_id}.sse.status"
    assert golden.exists(), f"missing golden fixture {golden}"
    assert status_file.exists(), f"missing golden status {status_file}"
    expected_status_from_file = int(status_file.read_text().strip())

    proxy_url, upstream_url = await _proxy_harness(behavior)
    assert expected_status == expected_status_from_file, "test case status must match golden .status file"

    import aiohttp

    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{proxy_url}{path}",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            json=body,
            timeout=60.0,
        ) as resp:
            actual_status = resp.status
            raw = await resp.read()

    assert actual_status == expected_status, (
        f"status mismatch: expected {expected_status}, got {actual_status}: {raw[:300]!r}"
    )
    normalized = normalize_for_golden(raw)
    assert normalized == golden.read_bytes(), f"golden mismatch for {case_id}: bytes differ from {golden.name}"
