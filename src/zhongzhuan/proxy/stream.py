"""SSE streaming pass-through."""
from __future__ import annotations

from aiohttp import web

from ..upstream import UpstreamClient


async def stream_proxy(
    request: web.Request,
    upstream: UpstreamClient,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> web.StreamResponse:
    """Stream upstream SSE response to client."""
    resp = web.StreamResponse(status=200, reason="OK")
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    await resp.prepare(request)

    try:
        async for chunk in upstream.stream(
            request.method, path, headers=headers, content=body,
        ):
            async for data, _ in chunk.aiter_raw():
                if data:
                    await resp.write(data)
    except Exception:
        pass
    finally:
        await resp.write_eof()

    return resp