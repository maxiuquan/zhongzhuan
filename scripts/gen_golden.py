"""Generate legacy golden fixtures (T08).

Runs the *real* proxy handler against a programmable mock upstream and captures
the exact SSE bytes the proxy returns to the client for each legacy link ×
scenario combination.  The captured bytes are normalized (random ids / timestamps
→ stable placeholders) and written to ``tests/golden/legacy/*.sse``.

Usage::

    python scripts/gen_golden.py

The script is deterministic *given* the stable fixture payloads in
``tests/support/mock_responses_upstream.py`` (fixed ids like ``chatcmpl-fixture0001``).
Only proxy-generated ids/timestamps are normalized away, so the golden files are
byte-for-byte reproducible on any machine.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

# Make the repo importable when run from a checkout.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient

# --- test support (deterministic payloads + mock upstream) -----------------
sys.path.insert(0, str(ROOT / "tests"))
from support.mock_responses_upstream import (  # noqa: E402
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
from support.sse_assert import normalize_for_golden  # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "golden" / "legacy"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _capture(
    proxy_url: str, path: str, headers: dict, body: dict,
) -> tuple[int, bytes]:
    """POST a request to the proxy and return (status, raw body bytes)."""
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            f"{proxy_url}{path}",
            headers=headers,
            json=body,
            timeout=60.0,
        ) as resp:
            return resp.status, await resp.read()


async def _run_case(
    proxy: ProxyServer,
    proxy_url: str,
    *,
    path: str,
    body: dict,
    filename: str,
    expect_status: int = 200,
) -> None:
    """Run one case and write the normalized SSE bytes to the golden file.

    The golden file is the *normalized* raw response body.  The expected HTTP
    status is recorded in a sibling ``.status`` file so the golden test can
    assert both the status and the byte-for-byte body.
    """
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    status, raw = await _capture(proxy_url, path, headers, body)
    assert status == expect_status, (
        f"expected {expect_status}, got {status}: {raw[:300]!r}"
    )
    normalized = normalize_for_golden(raw)
    out = GOLDEN_DIR / filename
    out.write_bytes(normalized)
    (GOLDEN_DIR / (filename + ".status")).write_text(str(status))
    print(f"  wrote {filename} (status={status}, {len(raw)} -> {len(normalized)} bytes)")


async def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Chat -> Chat (OpenAI passthrough, no translation) -----------------
    print("=== Chat -> Chat ===")
    async with MockUpstream() as up:
        up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
        client = UpstreamClient(base_url=up.url, timeout=10.0)
        await client.start()
        proxy = ProxyServer(
            upstream_clients={up.url: client},
            api_key="sk-test",
            proxy_timeout=10.0,
        )
        port = _free_port()
        runner = web.AppRunner(proxy.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        proxy_url = f"http://127.0.0.1:{port}"
        try:
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2chat_text_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(stream_payload=openai_tool_stream()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": True,
                      "messages": [{"role": "user", "content": "weather?"}],
                      "tools": [{"type": "function", "function": {
                          "name": "get_weather", "parameters": {"type": "object"}}}],
                      "tool_choice": "auto"},
                filename="chat2chat_tool_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(json_payload=openai_text_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": False,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2chat_text_json.sse",
            )
            up.set_behavior(UpstreamBehavior(status=400, error_body=openai_error_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": False,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2chat_error_json.sse",
                expect_status=400,
            )
        finally:
            await runner.cleanup()
            await client.close()

    # ---- Chat -> Anthropic (OpenAI client -> Anthropic upstream) -----------
    print("=== Chat -> Anthropic ===")
    async with MockUpstream() as up:
        up.set_behavior(UpstreamBehavior(stream_payload=anthropic_text_stream()))
        client = UpstreamClient(base_url=up.url, timeout=10.0)
        await client.start()
        proxy = ProxyServer(
            upstream_clients={up.url: client},
            api_key="sk-test",
            proxy_timeout=10.0,
        )
        port = _free_port()
        runner = web.AppRunner(proxy.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        proxy_url = f"http://127.0.0.1:{port}"
        try:
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2anthropic_text_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(stream_payload=anthropic_tool_stream()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": True,
                      "messages": [{"role": "user", "content": "weather?"}],
                      "tools": [{"type": "function", "function": {
                          "name": "get_weather", "parameters": {"type": "object"}}}],
                      "tool_choice": "auto"},
                filename="chat2anthropic_tool_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(json_payload=anthropic_text_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": False,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2anthropic_text_json.sse",
            )
            up.set_behavior(UpstreamBehavior(status=429, error_body=anthropic_error_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/chat/completions",
                body={"model": "x", "stream": False,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="chat2anthropic_error_json.sse",
                expect_status=429,
            )
        finally:
            await runner.cleanup()
            await client.close()

    # ---- Anthropic -> Chat (Anthropic client -> OpenAI upstream) -----------
    print("=== Anthropic -> Chat ===")
    async with MockUpstream() as up:
        up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
        client = UpstreamClient(base_url=up.url, timeout=10.0)
        await client.start()
        proxy = ProxyServer(
            upstream_clients={up.url: client},
            api_key="sk-test",
            proxy_timeout=10.0,
        )
        port = _free_port()
        runner = web.AppRunner(proxy.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        proxy_url = f"http://127.0.0.1:{port}"
        try:
            await _run_case(
                proxy, proxy_url,
                path="/v1/messages",
                body={"model": "x", "stream": True, "max_tokens": 1024,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="anthropic2chat_text_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(stream_payload=openai_tool_stream()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/messages",
                body={"model": "x", "stream": True, "max_tokens": 1024,
                      "messages": [{"role": "user", "content": "weather?"}],
                      "tools": [{"type": "function", "function": {
                          "name": "get_weather", "parameters": {"type": "object"}}}],
                      "tool_choice": "auto"},
                filename="anthropic2chat_tool_stream.sse",
            )
            up.set_behavior(UpstreamBehavior(json_payload=openai_text_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/messages",
                body={"model": "x", "stream": False, "max_tokens": 1024,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="anthropic2chat_text_json.sse",
            )
            up.set_behavior(UpstreamBehavior(status=400, error_body=openai_error_json()))
            await _run_case(
                proxy, proxy_url,
                path="/v1/messages",
                body={"model": "x", "stream": False, "max_tokens": 1024,
                      "messages": [{"role": "user", "content": "hi"}]},
                filename="anthropic2chat_error_json.sse",
                expect_status=400,
            )
        finally:
            await runner.cleanup()
            await client.close()

    print("\nDone. Golden files written to", GOLDEN_DIR)


if __name__ == "__main__":
    asyncio.run(main())