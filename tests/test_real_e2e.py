"""E2E test: start the real proxy server with a real API key and make a request through it."""
import asyncio
import json
import socket
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aiohttp import ClientSession, web

# Build a minimal config so we don't need the admin UI
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.upstream import UpstreamClient
from zhongzhuan.store import Store


API_KEY = os.environ.get("AGNES_API_KEY", "")
UPSTREAM = "https://apihub.agnes-ai.com/"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def main():
    if not API_KEY:
        print("Set AGNES_API_KEY env var")
        return

    # Create a temp store and seed it
    store = Store(":memory:")
    # Apply schema
    from zhongzhuan.store.schema import SCHEMA
    conn = store.connect()
    conn.executescript(SCHEMA)
    # Insert a model
    from zhongzhuan.store.models import create_model
    from zhongzhuan.store.keys import create_key, ApiKey
    model = create_model(store, type("M", (), {
        "name": "agens",
        "upstream_base": UPSTREAM,
        "upstream_model": "agnes-2.0-flash",
        "rpm_limit": 60,
        "tpm_limit": 100000,
        "enabled": True,
        "weight": 1,
    })())

    # Insert a key
    k = ApiKey(id=None, model_id=model.id, label="test", key_value=API_KEY)
    create_key(store, k)

    # Build upstream client and key health
    upstream = UpstreamClient(base_url=UPSTREAM, timeout=30.0)
    await upstream.start()
    health = KeyHealth(
        key_id=k.id, api_key=API_KEY,
        window=SlidingWindow(60, 60), rpm_limit=60,
        upstream_base=UPSTREAM,
        upstream_model="agnes-2.0-flash",
        model_name="agens",
    )

    proxy = ProxyServer(
        upstream_clients={UPSTREAM: upstream},
        keys=[health],
        proxy_timeout=30.0,
        store=store,
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"[proxy] listening on 127.0.0.1:{port}")

    try:
        async with ClientSession() as sess:
            # Test 1: non-stream
            print("\n=== Test 1: non-stream ===")
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                json={"model": "agens", "messages": [{"role": "user", "content": "Hello, please respond with a short greeting."}]},
                timeout=30.0,
            ) as resp:
                text = await resp.text()
                print(f"status={resp.status}")
                print(f"headers={dict(resp.headers)}")
                print(f"body={text[:800]}")
                assert resp.status == 200, f"non-stream failed: {text}"

            # Test 2: stream
            print("\n=== Test 2: stream ===")
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                json={"model": "agens", "stream": True, "messages": [{"role": "user", "content": "say hi"}]},
                timeout=30.0,
            ) as resp:
                print(f"status={resp.status}")
                chunks = []
                async for line in resp.content:
                    chunks.append(line)
                    if len(chunks) > 8:
                        break
                print(f"first chunks: {[c[:80] for c in chunks[:5]]}")
                assert resp.status == 200, "stream failed"
                assert any(b"data:" in c for c in chunks), f"no SSE in {chunks}"
                assert any(b"[DONE]" in c for c in chunks), f"no [DONE] in {chunks}"

            print("\n=== ALL TESTS PASSED ===")
    finally:
        await runner.cleanup()
        await upstream.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
