"""Tests for the Codex (desktop) model-discovery endpoint.

GET /v1/api/codex/models  (alias: GET /api/codex/models)
- requires a valid Bearer token (same table as /v1/responses) -> 401 otherwise
- returns {"models": [ModelInfo...]} with every required codex-rs field
"""

import pytest
from aiohttp import web

from zhongzhuan.proxy.server import ProxyServer
from zhongzhuan.store.models import Model


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
    # force a known official-model list (non-fallback)
    async def _slugs():
        return [
            "gpt-5.6-sol",
            "agnes-2.5-flash",
            "glm-5.2",
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
    assert len(body["models"]) == 3
    # alias path identical
    resp2 = await client.get(
        "/v1/api/codex/models", headers={"Authorization": "Bearer zz-goodtoken"}
    )
    assert resp2.status == 200
    assert await resp2.json() == body


def test_model_info_shape():
    info = ProxyServer._build_codex_model_info("gpt-5.6-sol")
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
    assert info["slug"] == "gpt-5.6-sol"
    assert info["display_name"] == "gpt-5.6-sol"
    assert info["use_responses_lite"] is False
    assert info["supports_parallel_tool_calls"] is True
    assert info["truncation_policy"] == {"mode": "bytes", "limit": 200000}
    # empty reasoning -> Codex won't send `reasoning` to upstreams that lack it
    assert info["supported_reasoning_levels"] == []
    assert info["supports_reasoning_summaries"] is False


async def test_codex_model_slugs_excludes_fallback_and_disabled(monkeypatch):
    """The live query must surface only enabled, non-fallback (official) models."""
    official = Model(
        name="gpt-5.6-sol", upstream_base="x", upstream_model="y",
        enabled=True, is_fallback=False,
    )
    fallback = Model(
        name="oc-glm-5.2-free", upstream_base="x", upstream_model="y",
        enabled=True, is_fallback=True,
    )
    disabled_official = Model(
        name="glm-5.2", upstream_base="x", upstream_model="y",
        enabled=False, is_fallback=False,
    )

    async def _fake_list(_store):
        return [official, fallback, disabled_official]

    monkeypatch.setattr("zhongzhuan.store.models.list_models", _fake_list)
    s = ProxyServer(upstream_clients={}, store=object())  # truthy, non-None
    names = await s._codex_model_slugs()
    assert names == ["gpt-5.6-sol"]


# ----------------------------------------------------------------------
# /v1/models UA-based routing: Codex clients get the codex catalog shape,
# everyone else keeps the original OpenAI `{"object":"list","data":[...]}`.
# ----------------------------------------------------------------------

def _make_models_app(server):
    app = web.Application()
    app.router.add_get("/v1/models", server._list_models)
    return app


async def test_models_route_codex_ua_returns_codex_catalog(aiohttp_client, monkeypatch):
    s = ProxyServer(upstream_clients={}, store=object())
    async def _slugs():
        return ["gpt-5.6-sol", "glm-5.2"]
    monkeypatch.setattr(s, "_codex_model_slugs", _slugs)
    client = await aiohttp_client(_make_models_app(s))

    # "Codex Desktop/0.146.0"
    resp = await client.get("/v1/models", headers={"User-Agent": "Codex Desktop/0.146.0"})
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert "models" in body
    assert [m["slug"] for m in body["models"]] == ["gpt-5.6-sol", "glm-5.2"]

    # "codex-tui/0.x" also matches (case-insensitive)
    resp2 = await client.get("/v1/models", headers={"User-Agent": "codex-tui/0.5"})
    assert resp2.status == 200
    assert "models" in await resp2.json()


async def test_models_route_non_codex_ua_returns_openai_list(aiohttp_client, monkeypatch):
    s = ProxyServer(upstream_clients={}, store=object())
    s.models = [{"name": "gpt-5.6-sol"}, {"name": "glm-5.2"}]
    s.groups = []
    client = await aiohttp_client(_make_models_app(s))

    # No UA at all -> OpenAI format
    resp = await client.get("/v1/models")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body.get("object") == "list"
    assert [d["id"] for d in body["data"]] == ["gpt-5.6-sol", "glm-5.2"]

    # A normal OpenAI-compatible client UA -> OpenAI format
    resp2 = await client.get(
        "/v1/models", headers={"User-Agent": "OpenAI/Python 1.0"}
    )
    assert resp2.status == 200
    assert (await resp2.json()).get("object") == "list"


# ----------------------------------------------------------------------
# Codex client detection (pure predicate — no aiohttp_client fixture needed)
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, headers, expected",
    [
        # Every Codex User-Agent flavour seen in the wild.
        ("/v1/models", {"User-Agent": "codex_cli_rs/0.147.0 (Windows 11; x86_64)"}, True),
        ("/v1/models", {"User-Agent": "Codex-Desktop/26.803.1 (Windows 11; x86_64)"}, True),
        ("/v1/models", {"User-Agent": "Codex Desktop/26.803.1"}, True),
        ("/v1/models", {"User-Agent": "codex-tui/0.5"}, True),
        ("/v1/models", {"User-Agent": "CODEX/1.0"}, True),
        # UA rewritten/dropped, but codex-rs still stamps `originator`.
        ("/v1/models", {"User-Agent": "reqwest/0.12", "originator": "codex_cli_rs"}, True),
        # Codex model discovery always carries ?client_version=
        ("/v1/models?client_version=26.803.1", {"User-Agent": "unknown/1.0"}, True),
        # Non-Codex clients keep the OpenAI shape.
        ("/v1/models", {"User-Agent": "OpenAI/Python 1.0"}, False),
        ("/v1/models", {"User-Agent": "curl/8.5.0"}, False),
        ("/v1/models", {}, False),
    ],
)
def test_is_codex_client(path, headers, expected):
    from aiohttp.test_utils import make_mocked_request

    s = ProxyServer(upstream_clients={}, store=object())
    req = make_mocked_request("GET", path, headers=headers)
    assert s._is_codex_client(req) is expected
