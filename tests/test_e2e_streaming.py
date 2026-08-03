"""End-to-end test mimicking a real chat completion flow."""
import asyncio
import json
import socket
import sys

import pytest
import pytest_asyncio
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


async def make_upstream_that_returns_sse():
    """Mock upstream that returns proper SSE for streaming."""
    async def handler(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        print(f"[upstream] recv: {body!r}")
        print(f"[upstream] path: {request.path}")
        print(f"[upstream] auth: {request.headers.get('Authorization')}")
        # Check model name was swapped
        try:
            obj = json.loads(body)
            print(f"[upstream] model in body: {obj.get('model')}")
        except Exception:
            pass

        is_stream = False
        try:
            obj = json.loads(body)
            is_stream = obj.get("stream", False)
        except Exception:
            pass

        if is_stream:
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            await resp.write(b'data: {"id":"x","choices":[{"delta":{"content":"hello"}}]}\n\n')
            await resp.write(b'data: {"id":"x","choices":[{"delta":{"content":" world"}}]}\n\n')
            await resp.write(b'data: [DONE]\n\n')
            await resp.write_eof()
            return resp
        else:
            return web.json_response({
                "id": "x",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            })

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", runner


@pytest.mark.asyncio
async def test_e2e_streaming():
    upstream_url, upstream_runner = await make_upstream_that_returns_sse()
    try:
        upstream = UpstreamClient(base_url=upstream_url, timeout=10.0)
        await upstream.start()
        keys = [
            KeyHealth(
                key_id=1, api_key="sk-test", window=SlidingWindow(60, 1000),
                rpm_limit=1000, upstream_base=upstream_url,
                upstream_model="real-model-name", model_name="my-custom-name",
            ),
        ]
        proxy = ProxyServer(
            upstream_clients={upstream_url: upstream},
            keys=keys, proxy_timeout=10.0,
        )
        port = _free_port()
        proxy_runner = web.AppRunner(proxy.app())
        await proxy_runner.setup()
        site = web.TCPSite(proxy_runner, "127.0.0.1", port)
        await site.start()
        try:
            async with ClientSession() as sess:
                # Test 1: non-stream
                print("\n=== Test 1: non-stream ===")
                async with sess.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={"model": "my-custom-name", "messages": [{"role": "user", "content": "hi"}]},
                ) as resp:
                    body = await resp.json()
                    print(f"non-stream resp: {resp.status} {body}")
                    assert resp.status == 200

                # Test 2: stream
                print("\n=== Test 2: stream ===")
                async with sess.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                    json={"model": "my-custom-name", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                ) as resp:
                    print(f"stream resp status: {resp.status}")
                    print(f"stream resp headers: {dict(resp.headers)}")
                    chunks = []
                    async for line in resp.content:
                        chunks.append(line)
                        if len(chunks) > 20:
                            break
                    print(f"stream chunks: {chunks!r}")
                    assert resp.status == 200
                    assert any(b"hello" in c for c in chunks), f"no 'hello' in {chunks}"
        finally:
            await proxy_runner.cleanup()
            await upstream.close()
    finally:
        await upstream_runner.cleanup()
