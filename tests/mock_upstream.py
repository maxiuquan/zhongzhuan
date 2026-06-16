"""Test mock upstream server."""
from __future__ import annotations

import sys
from aiohttp import web


async def chat_completions(request: web.Request) -> web.Response:
    auth = request.headers.get("Authorization", "")
    return web.json_response({
        "id": "mock",
        "model": "x",
        "choices": [{"message": {"role": "assistant", "content": f"echo; auth={auth}"}}],
    })


async def completions(request: web.Request) -> web.Response:
    return web.json_response({
        "id": "mock", "model": "x",
        "choices": [{"text": "echo", "index": 0}],
    })


async def embeddings(request: web.Request) -> web.Response:
    return web.json_response({
        "data": [{"embedding": [0.0] * 128, "index": 0}],
        "model": "x",
    })


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/completions", completions)
    app.router.add_post("/v1/embeddings", embeddings)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    return app


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
    print(f"Mock upstream on http://127.0.0.1:{port}")
    web.run_app(make_app(), port=port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())