"""Tests for the Codex (desktop) model-discovery endpoint.

GET /v1/api/codex/models  (alias: GET /api/codex/models)
- requires a valid Bearer token (same table as /v1/responses) -> 401 otherwise
- returns {"models": [ModelInfo...]} with every required codex-rs field
"""

import pytest
from aiohttp import web

from zhongzhuan.proxy.server import ProxyServer


class _FakeToken:
    def __init__(self, ok=True):
        self._ok = ok

    def check_quota(self, model=""):
        return (self._ok, "")


class _FakeStore:
    """Truthy stub so the handler doesn't early-401 on store is None."""


@pytest.fixture
def server():
    s = ProxyServer(upstream_clients={}, store=_FakeStore())
    return s


def _make_app(server):
    app = web.Application()
    app.router.add_get("/v1/api/codex/models", server._codex_models)
    app.router.add_get("/api/codex/models", server._codex_models)
    return app


async def test_no_token_returns_401(server, aiohttp_client, monkeypatch):
    async def _good(store, token):
        return _FakeToken()

    monkeypatch.setattr(
        "zhongzhuan.store.access_tokens.get_token_by_value", _good
    )
    client = await aiohttp_client(_make_app(server))
    for path in ("/v1/api/codex/models", "/api/codex/models"):
        resp = await client.get(path)
        assert resp.status == 401, (path, resp.status)
        body = await resp.json()
        assert body["error"]["type"] == "unauthorized"


async def test_bad_token_returns_401(server, aiohttp_client, monkeypatch):
    async def _none(store, token):
        return None

    monkeypatch.setattr(
        "zhongzhuan.store.access_tokens.get_token_by_value", _none
    )
    client = await aiohttp_client(_make_app(server))
    resp = await client.get(
        "/v1/api/codex/models", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status == 401


async def test_valid_token_returns_models(server, aiohttp_client, monkeypatch):
    async def _good(store, token):
        return _FakeToken(ok=True)

    monkeypatch.setattr(
        "zhongzhuan.store.access_tokens.get_token_by_value", _good
    )
    # force a known 6-model list
    async def _slugs():
        return [
            "oc-glm-5.2-free",
            "oc-glm-5.1-free",
            "oc-kimi-k2.7-code-free",
            "oc-deepseek-v4-flash-free",
            "oc-mimo-v2.5-free",
            "oc-qwen3-coder-free",
        ]

    monkeypatch.setattr(server, "_codex_model_slugs", _slugs)
    client = await aiohttp_client(_make_app(server))
    resp = await client.get(
        "/api/codex/models?client_version=0.146.0",
        headers={"Authorization": "Bearer zz-goodtoken"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert "models" in body
    assert len(body["models"]) == 6
    # alias path identical
    resp2 = await client.get(
        "/v1/api/codex/models", headers={"Authorization": "Bearer zz-goodtoken"}
    )
    assert resp2.status == 200
    assert await resp2.json() == body


def test_model_info_shape():
    info = ProxyServer._build_codex_model_info("oc-glm-5.2-free")
    required = {
        "slug",
        "display_name",
        "description",
        "supported_reasoning_levels",
        "shell_type",
        "visibility",
        "supported_in_api",
        "priority",
        "availability_nux",
        "upgrade",
        "base_instructions",
        "supports_reasoning_summaries",
        "support_verbosity",
        "default_verbosity",
        "apply_patch_tool_type",
        "truncation_policy",
        "supports_parallel_tool_calls",
        "experimental_supported_tools",
        "use_responses_lite",
    }
    assert set(info.keys()) == required
    assert info["slug"] == "oc-glm-5.2-free"
    assert info["display_name"] == "glm-5.2-free"
    assert info["use_responses_lite"] is False
    assert info["supports_parallel_tool_calls"] is True
    assert info["truncation_policy"] == {"mode": "bytes", "limit": 200000}
    # empty reasoning -> Codex won't send `reasoning` to upstreams that lack it
    assert info["supported_reasoning_levels"] == []
    assert info["supports_reasoning_summaries"] is False
