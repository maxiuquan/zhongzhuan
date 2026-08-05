"""Key CRUD tests."""

import os

os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

import pytest

from zhongzhuan.admin.api_keys import _build_upstream_url, _build_fingerprint_headers
from zhongzhuan.store.models import Model, create_model
from zhongzhuan.store.keys import ApiKey, create_key, list_keys, get_key_cipher, delete_key


@pytest.mark.asyncio
async def test_key_crud(store):
    m = await create_model(store, Model(name="m1", upstream_base="http://x", upstream_model="m1"))
    k = await create_key(store, ApiKey(id=None, model_id=m.id, label="L", key_value="sk-longkey123456"))
    assert k.id and k.id > 0
    rows = await list_keys(store, m.id)
    assert len(rows) == 1
    assert rows[0].key_masked.startswith("sk-l")
    plain = await get_key_cipher(store, k.id)
    assert plain == "sk-longkey123456"
    await delete_key(store, k.id)
    assert (await list_keys(store, m.id)) == []


def test_build_upstream_url_dedups_base_path():
    """测试 Key 连通性时必须与代理主流程产生相同的上游 URL。

    回归：base 已含 /v1 时，旧实现手工拼接 upstream_base + /v1/chat/completions
    会产生 /v1/v1 双重复，导致真实可用的 Key 被误报为失败。
    """
    # base 含 /v1 → 去重，避免 /v1/v1
    assert (
        _build_upstream_url("https://macc.eu.cc/v1", "", "openai")
        == "https://macc.eu.cc/v1/chat/completions"
    )
    assert (
        _build_upstream_url("https://macc.eu.cc/v1", "", "anthropic")
        == "https://macc.eu.cc/v1/messages"
    )
    # base 不含 /v1 → 正常追加
    assert (
        _build_upstream_url("https://api.example.com", "", "openai")
        == "https://api.example.com/v1/chat/completions"
    )
    # path override（相对路径，如 opencode 的 /zen/v1/...）
    assert (
        _build_upstream_url("https://opencode.ai", "/zen/v1/chat/completions", "openai")
        == "https://opencode.ai/zen/v1/chat/completions"
    )
    # path override 是完整 URL → 原样返回
    assert (
        _build_upstream_url("https://opencode.ai", "https://full.url/chat", "openai")
        == "https://full.url/chat"
    )
    # base 与 override 都带 /v1 → 仍去重
    assert (
        _build_upstream_url("https://h.com/v1", "/v1/chat/completions", "openai")
        == "https://h.com/v1/chat/completions"
    )


def test_build_fingerprint_headers_presets_and_custom():
    """测试 Key 连通性请求必须与代理主流程携带相同的客户端指纹头。

    模型勾选了"模拟某客户端"时（如 WorkBuddy），测试连接也要注入对应指纹头，
    否则某些上游对陌生 UA 的测试请求会拒绝/限流，导致误报失败。
    """
    # 不模拟 → 空列表（零影响）
    assert _build_fingerprint_headers("", "") == []
    assert _build_fingerprint_headers("", "whatever") == []

    # 内置预设 workbuddy → 返回预设头（模板 value 未渲染）
    hdrs = _build_fingerprint_headers("workbuddy", "")
    names = [n for n, _ in hdrs]
    assert "User-Agent" in names
    assert "X-Client-Name" in names
    assert "X-Request-ID" in names
    assert dict(hdrs)["User-Agent"].startswith("WorkBuddy/")

    # custom → 解析 JSON 自定义头
    raw = '[{"name":"X-Foo","value":"bar"},{"name":"X-Request-ID","value":"{{uuid}}"}]'
    hdrs = _build_fingerprint_headers("custom", raw)
    assert dict(hdrs)["X-Foo"] == "bar"
    assert dict(hdrs)["X-Request-ID"] == "{{uuid}}"  # 模板原样，由调用方 render

    # custom 但 JSON 损坏 → 容错为空（退化为不注入）
    assert _build_fingerprint_headers("custom", "not json") == []

    # 未知预设 → 空列表（与 get_headers 行为一致）
    assert _build_fingerprint_headers("bogus", "") == []
