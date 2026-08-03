"""Test against real AgnesAI to see if we get 502 there too."""
import asyncio
import json
import os
import socket
import sys
import time

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


async def call_agnes_direct():
    """Call AgnesAI directly to see the real response."""
    # We need an API key. Get from env or ask user
    api_key = os.environ.get("AGNES_API_KEY", "")
    if not api_key:
        print("Set AGNES_API_KEY env var to test against real AgnesAI")
        return

    print("\n=== Direct call to AgnesAI (no proxy) ===")
    async with ClientSession() as sess:
        for model_name in ["agnes-2.0-flash", "agnes-1.5-flash"]:
            for stream in [False, True]:
                print(f"\n[direct] model={model_name} stream={stream}")
                try:
                    async with sess.post(
                        "https://apihub.agnes-ai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model_name, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
                        timeout=30.0,
                    ) as resp:
                        print(f"[direct] status={resp.status}")
                        if stream:
                            chunks = []
                            async for line in resp.content:
                                chunks.append(line)
                                if len(chunks) > 5:
                                    break
                            print(f"[direct] chunks={chunks[:3]!r}")
                        else:
                            text = await resp.text()
                            print(f"[direct] body={text[:300]}")
                except Exception as e:
                    print(f"[direct] error: {type(e).__name__}: {e}")


async def call_via_proxy(api_key: str):
    """Test calling AgnesAI via the proxy."""
    print("\n=== Call via zhongzhuan proxy ===")
    upstream = UpstreamClient(base_url="https://apihub.agnes-ai.com/v1", timeout=30.0)
    await upstream.start()
    keys = [
        KeyHealth(
            key_id=1, api_key=api_key, window=SlidingWindow(60, 1000),
            rpm_limit=1000, upstream_base="https://apihub.agnes-ai.com/v1",
            upstream_model="agnes-2.0-flash", model_name="agens",
        ),
    ]
    proxy = ProxyServer(
        upstream_clients={"https://apihub.agnes-ai.com/v1": upstream},
        keys=keys, proxy_timeout=30.0,
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            for stream in [False, True]:
                print(f"\n[proxy] model=agens stream={stream}")
                try:
                    async with sess.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        headers={"Content-Type": "application/json"},
                        json={"model": "agens", "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
                        timeout=30.0,
                    ) as resp:
                        print(f"[proxy] status={resp.status}")
                        if stream:
                            chunks = []
                            async for line in resp.content:
                                chunks.append(line)
                                if len(chunks) > 5:
                                    break
                            print(f"[proxy] chunks={chunks[:3]!r}")
                        else:
                            text = await resp.text()
                            print(f"[proxy] body={text[:300]}")
                except Exception as e:
                    print(f"[proxy] error: {type(e).__name__}: {e}")
    finally:
        await runner.cleanup()
        await upstream.close()


async def main():
    api_key = os.environ.get("AGNES_API_KEY", "")
    if not api_key:
        print("No AGNES_API_KEY env var set.")
        print("Usage: $env:AGNES_API_KEY='sk-xxx'; python tests/test_real_agnes.py")
        return
    await call_agnes_direct()
    await call_via_proxy(api_key)


if __name__ == "__main__":
    asyncio.run(main())
