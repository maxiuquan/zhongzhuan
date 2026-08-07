"""客户端指纹模拟测试（v009）。

覆盖：
* header_templates.render 的 {{uuid}} 渲染与未知变量保留
* client_presets 的 PRESETS / get_headers / list_presets / is_valid_preset_name
  / validate_custom_header_name / parse_custom_headers（含容错）
* ProxyHandler._apply_client_fingerprint 三分支（不模拟/预设/自定义）
* DB 迁移 v009 + Model.client_preset/custom_headers CRUD 往返
"""

from __future__ import annotations

import json
import os
import re
import uuid

os.environ.setdefault("ZHONGZHUAN_DEV_NO_DPAPI", "1")

import pytest

from zhongzhuan.proxy.client_presets import (
    PRESETS,
    get_fingerprint_system_prefix,
    get_headers,
    is_valid_preset_name,
    list_presets,
    parse_custom_headers,
    serialize_custom_headers,
    validate_custom_header_name,
)
from zhongzhuan.proxy.header_templates import render
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow


# ---------------------------------------------------------------------------
# header_templates.render
# ---------------------------------------------------------------------------


class TestRender:
    def test_no_template_returns_as_is(self):
        assert render("plain value") == "plain value"
        assert render("") == ""

    def test_uuid_renders_to_valid_uuid(self):
        out = render("{{uuid}}")
        # 必须是合法 UUID4
        u = uuid.UUID(out)
        assert u.version == 4

    def test_uuid_unique_per_call(self):
        a = render("{{uuid}}")
        b = render("{{uuid}}")
        assert a != b

    def test_uuid_embedded_in_text(self):
        out = render("id={{uuid}}&ts=now")
        assert out.startswith("id=")
        assert out.endswith("&ts=now")
        body = out[len("id=") : -len("&ts=now")]
        uuid.UUID(body)  # 合法即不抛

    def test_unknown_var_preserved(self):
        out = render("{{unknown}}")
        assert out == "{{unknown}}"

    def test_multiple_vars_in_one_string(self):
        out = render("a={{uuid}}&keep={{unknown}}&b={{uuid}}")
        # 两个 uuid 不同
        m = re.findall(r"a=([^&]+)&keep=\{\{unknown\}\}&b=([^&]+)", out)
        assert m and m[0][0] != m[0][1]

    def test_spaces_around_var_name_tolerated(self):
        out = render("{{  uuid  }}")
        uuid.UUID(out)  # 合法即不抛


# ---------------------------------------------------------------------------
# client_presets
# ---------------------------------------------------------------------------


class TestClientPresets:
    def test_workbuddy_preset_exists(self):
        assert "workbuddy" in PRESETS
        assert "label" in PRESETS["workbuddy"]
        assert "headers" in PRESETS["workbuddy"]

    def test_workbuddy_has_real_client_fingerprint_headers(self):
        """预设必须含真实 WorkBuddy 客户端抓包所得的关键指纹头（2026-08-05）。

        缺失这些头时 freemodel.dev 返回 403 unsupported_client。
        """
        headers = dict(get_headers("workbuddy"))
        names = list(headers.keys())
        # 关键身份头必须在预设中
        for required in (
            "User-Agent",
            "X-Requested-With",
            "X-Request-ID",
            "X-IDE-Type",
            "X-IDE-Name",
            "X-IDE-Version",
            "X-CodeBuddy-Request",
            "X-Domain",
            "X-Product",
            "X-Stainless-Lang",
        ):
            assert required in names, f"缺少指纹头: {required}"
        assert headers["User-Agent"].startswith("WorkBuddy/5.3.8")
        assert headers["X-IDE-Type"] == "WorkBuddy"
        assert headers["X-CodeBuddy-Request"] == "1"

    def test_workbuddy_x_request_id_uses_uuid_template(self):
        headers = dict(get_headers("workbuddy"))
        assert headers["X-Request-ID"] == "{{uuid}}"

    def test_get_headers_unknown_preset_returns_empty(self):
        assert get_headers("nonexistent") == []

    def test_get_headers_returns_copy(self):
        a = get_headers("workbuddy")
        a.append(("X-Injected", "x"))
        b = get_headers("workbuddy")
        assert ("X-Injected", "x") not in b

    def test_list_presets_returns_workbuddy(self):
        presets = list_presets()
        assert any(p["key"] == "workbuddy" for p in presets)
        # list_presets 不含"不模拟"和"自定义"
        assert all(p["key"] != "" for p in presets)
        assert all(p["key"] != "custom" for p in presets)

    def test_is_valid_preset_name(self):
        assert is_valid_preset_name("") is True
        assert is_valid_preset_name("custom") is True
        assert is_valid_preset_name("workbuddy") is True
        assert is_valid_preset_name("bogus") is False

    def test_validate_custom_header_name_valid(self):
        assert validate_custom_header_name("X-Client-Name") is None
        assert validate_custom_header_name("User-Agent") is None
        assert validate_custom_header_name("  X-Trace-ID  ") is None

    def test_validate_custom_header_name_empty(self):
        assert validate_custom_header_name("") is not None
        assert validate_custom_header_name("   ") is not None

    def test_validate_custom_header_name_forbidden(self):
        for h in (
            "Authorization",
            "authorization",
            "Host",
            "host",
            "Content-Length",
            "Transfer-Encoding",
            "Connection",
        ):
            assert validate_custom_header_name(h) is not None, h


class TestParseCustomHeaders:
    def test_empty_returns_empty(self):
        assert parse_custom_headers("") == []
        assert parse_custom_headers(None) == []  # type: ignore[arg-type]

    def test_valid_json(self):
        raw = '[{"name":"X-Foo","value":"bar"},{"name":"X-Trace","value":"{{uuid}}"}]'
        out = parse_custom_headers(raw)
        assert out == [("X-Foo", "bar"), ("X-Trace", "{{uuid}}")]

    def test_invalid_json_returns_empty(self):
        assert parse_custom_headers("not json") == []
        assert parse_custom_headers("{broken") == []

    def test_non_array_returns_empty(self):
        assert parse_custom_headers('{"name":"X","value":"y"}') == []

    def test_skips_items_without_name(self):
        raw = '[{"name":"A","value":"1"},{"value":"2"},{"name":"","value":"3"},{"name":"C","value":"4"}]'
        out = parse_custom_headers(raw)
        assert out == [("A", "1"), ("C", "4")]

    def test_missing_value_defaults_to_empty_string(self):
        raw = '[{"name":"X-Foo"}]'
        out = parse_custom_headers(raw)
        assert out == [("X-Foo", "")]

    def test_non_dict_items_skipped(self):
        raw = '["str", 123, {"name":"A","value":"1"}]'
        out = parse_custom_headers(raw)
        assert out == [("A", "1")]

    def test_serialize_roundtrip(self):
        original = [("X-A", "1"), ("X-B", "{{uuid}}")]
        raw = serialize_custom_headers(original)
        assert parse_custom_headers(raw) == original


# ---------------------------------------------------------------------------
# ProxyHandler._apply_client_fingerprint
# ---------------------------------------------------------------------------


def _make_key(preset: str = "", custom: list[tuple[str, str]] | None = None) -> KeyHealth:
    """构造测试用 KeyHealth。"""
    return KeyHealth(
        key_id=1,
        api_key="test-key",
        window=SlidingWindow(60, 0),
        client_preset=preset,
        custom_headers=custom or [],
    )


def _make_handler():
    """构造一个最小 ProxyHandler 实例（只需 _apply_client_fingerprint 方法）。"""
    from zhongzhuan.proxy.handler import ProxyHandler

    return ProxyHandler.__new__(ProxyHandler)


class TestApplyClientFingerprint:
    def test_empty_preset_no_modification(self):
        h = _make_handler()
        key = _make_key(preset="")
        headers = {"Authorization": "Bearer xxx", "Content-Type": "application/json"}
        original = dict(headers)
        result = h._apply_client_fingerprint(headers, key)
        assert result == original
        assert result is headers  # 同一对象

    def test_workbuddy_preset_injects_real_client_headers(self):
        h = _make_handler()
        key = _make_key(preset="workbuddy")
        headers = {"Authorization": "Bearer xxx"}
        h._apply_client_fingerprint(headers, key)
        assert headers["User-Agent"] == "WorkBuddy/5.3.8 WorkBuddy/5.3.8 CLI/2.115.0"
        assert headers["X-IDE-Type"] == "WorkBuddy"
        assert headers["X-IDE-Name"] == "WorkBuddy"
        assert headers["X-CodeBuddy-Request"] == "1"
        assert headers["X-Domain"] == "www.codebuddy.cn"
        assert headers["X-Product"] == "SaaS"
        # X-Request-ID 是动态 UUID
        uuid.UUID(headers["X-Request-ID"])
        # Authorization 不被覆盖（预设不含 Authorization）
        assert headers["Authorization"] == "Bearer xxx"

    def test_workbuddy_x_request_id_unique_per_call(self):
        h = _make_handler()
        key = _make_key(preset="workbuddy")
        h1 = {"Authorization": "x"}
        h2 = {"Authorization": "x"}
        h._apply_client_fingerprint(h1, key)
        h._apply_client_fingerprint(h2, key)
        assert h1["X-Request-ID"] != h2["X-Request-ID"]

    def test_custom_preset_uses_custom_headers(self):
        h = _make_handler()
        key = _make_key(
            preset="custom",
            custom=[("X-My-Client", "myapp/1.0"), ("X-Trace-ID", "{{uuid}}")],
        )
        headers = {"Authorization": "Bearer xxx"}
        h._apply_client_fingerprint(headers, key)
        assert headers["X-My-Client"] == "myapp/1.0"
        uuid.UUID(headers["X-Trace-ID"])  # 模板已渲染

    def test_custom_preset_empty_list_no_modification(self):
        h = _make_handler()
        key = _make_key(preset="custom", custom=[])
        headers = {"Authorization": "Bearer xxx"}
        original = dict(headers)
        h._apply_client_fingerprint(headers, key)
        assert headers == original


class TestInjectSystemMessage:
    """_inject_system_message：require_system 预设（workbuddy）请求体补 system 消息。"""

    def _make_key(self, preset: str, upstream_model: str = "gpt-5.6-sol"):
        # 只依赖 client_preset / upstream_model 两个属性（handler 用 getattr 防御取）
        return type("K", (), {"client_preset": preset, "upstream_model": upstream_model})()

    def _body(self, messages):
        return json.dumps({"model": "gpt-5.6-sol", "messages": messages, "stream": False}).encode()

    def test_workbuddy_injects_system_when_missing(self):
        h = _make_handler()
        out = h._inject_system_message(
            self._body([{"role": "user", "content": "hi"}]),
            self._make_key("workbuddy"),
            "gpt-5.6-sol",
        )
        msgs = json.loads(out)["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"].startswith("This conversation is powered by gpt-5.6-sol")
        # 2026-08-07：workbuddy 特征 system 升级为完整模板（上游校验 3500+ 字符段）
        assert len(msgs[0]["content"]) > 4000
        assert len(msgs) == 2

    def test_workbuddy_does_not_duplicate_when_first_system_is_fingerprint(self):
        """第一条 system 已是特征内容 → 原样返回（不重复注入）。"""
        h = _make_handler()
        fp = get_fingerprint_system_prefix("workbuddy", "gpt-5.6-sol")
        orig = [
            {"role": "system", "content": fp},
            {"role": "user", "content": "hi"},
        ]
        raw = self._body(orig)
        out = h._inject_system_message(raw, self._make_key("workbuddy"), "gpt-5.6-sol")
        assert json.loads(out) == json.loads(raw)  # 内容原样返回（无重复注入）

    def test_workbuddy_prepends_fingerprint_when_system_is_foreign(self):
        """请求体自带非特征 system（如 Trae 的 "powered by TRAE"）→ 最前面插入特征 system，
        原 system 被挤到第二位（指令仍生效），否则上游 403 unsupported_client。"""
        h = _make_handler()
        orig = [
            {
                "role": "system",
                "content": "You are a powerful code assistant, powered by a proprietary model from TRAE.",
            },
            {"role": "user", "content": "hi"},
        ]
        out = h._inject_system_message(self._body(orig), self._make_key("workbuddy"), "gpt-5.6-sol")
        msgs = json.loads(out)["messages"]
        assert len(msgs) == 3
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"].startswith("This conversation is powered by gpt-5.6-sol")
        assert len(msgs[0]["content"]) > 4000
        assert "TRAE" in msgs[1]["content"]  # 原 system 保留在后面

    def test_workbuddy_prepends_when_first_message_is_not_system(self):
        """第一条消息不是 system（如直接 user）→ 也前置插入特征 system。"""
        h = _make_handler()
        out = h._inject_system_message(
            self._body([{"role": "user", "content": "hi"}]),
            self._make_key("workbuddy"),
            "gpt-5.6-sol",
        )
        msgs = json.loads(out)["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"].startswith("This conversation is powered by gpt-5.6-sol")
        assert len(msgs[0]["content"]) > 4000
        assert len(msgs) == 2

    def test_no_preset_zero_impact(self):
        h = _make_handler()
        raw = self._body([{"role": "user", "content": "hi"}])
        out = h._inject_system_message(raw, self._make_key(""), "gpt-5.6-sol")
        assert out is raw

    def test_custom_preset_zero_impact(self):
        h = _make_handler()
        raw = self._body([{"role": "user", "content": "hi"}])
        out = h._inject_system_message(raw, self._make_key("custom"), "gpt-5.6-sol")
        assert out is raw

    def test_non_messages_body_zero_impact(self):
        h = _make_handler()
        raw = json.dumps({"model": "x", "input": "text"}).encode()
        out = h._inject_system_message(raw, self._make_key("workbuddy"), "gpt-5.6-sol")
        assert out is raw

    def test_invalid_json_zero_impact(self):
        h = _make_handler()
        raw = b"{not json"
        out = h._inject_system_message(raw, self._make_key("workbuddy"), "gpt-5.6-sol")
        assert out is raw

    def test_unknown_preset_no_modification(self):
        h = _make_handler()
        key = _make_key(preset="bogus")
        headers = {"Authorization": "Bearer xxx"}
        original = dict(headers)
        h._apply_client_fingerprint(headers, key)
        assert headers == original

    def test_custom_can_override_authorization(self):
        """自定义头含 Authorization 会覆盖 key 注入的 Bearer token。
        P0 内置预设不含 Authorization; 自定义模式下用户显式覆盖是允许的
        （受控头黑名单在 API 层拦截, 运行时不重复校验以保性能）。
        """
        h = _make_handler()
        key = _make_key(
            preset="custom",
            custom=[("Authorization", "Bearer custom-token")],
        )
        headers = {"Authorization": "Bearer xxx"}
        h._apply_client_fingerprint(headers, key)
        assert headers["Authorization"] == "Bearer custom-token"


# ---------------------------------------------------------------------------
# DB 迁移 + Model CRUD 往返
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_client_preset_crud_roundtrip(store):
    """v009 列 client_preset / custom_headers 的 CRUD 往返。"""
    from zhongzhuan.store.models import Model, create_model, get_model, update_model

    m = await create_model(
        store,
        Model(
            name="workbuddy-model",
            upstream_base="https://work.freemodel.dev",
            upstream_model="gpt-5.6-sol",
            client_preset="workbuddy",
            custom_headers='[{"name":"X-Stash","value":"saved"}]',
        ),
    )
    got = await get_model(store, "workbuddy-model")
    assert got is not None
    assert got.client_preset == "workbuddy"
    assert got.custom_headers == '[{"name":"X-Stash","value":"saved"}]'

    # 切换到 custom, 验证 custom_headers 可独立修改
    await update_model(
        store,
        m.id,
        Model(
            name="workbuddy-model",
            upstream_base="https://work.freemodel.dev",
            upstream_model="gpt-5.6-sol",
            client_preset="custom",
            custom_headers='[{"name":"X-Custom","value":"v1"}]',
        ),
    )
    got2 = await get_model(store, "workbuddy-model")
    assert got2 is not None
    assert got2.client_preset == "custom"
    assert got2.custom_headers == '[{"name":"X-Custom","value":"v1"}]'

    # 切换回不模拟, custom_headers 保留（不丢失）
    await update_model(
        store,
        m.id,
        Model(
            name="workbuddy-model",
            upstream_base="https://work.freemodel.dev",
            upstream_model="gpt-5.6-sol",
            client_preset="",
            custom_headers='[{"name":"X-Custom","value":"v1"}]',
        ),
    )
    got3 = await get_model(store, "workbuddy-model")
    assert got3 is not None
    assert got3.client_preset == ""
    assert got3.custom_headers == '[{"name":"X-Custom","value":"v1"}]'


# ---------------------------------------------------------------------------
# 管理端 API 校验
# ---------------------------------------------------------------------------


class TestApiValidation:
    def _validate(self, data: dict) -> str | None:
        from zhongzhuan.admin.api_models import _validate_payload

        return _validate_payload(data)

    def test_valid_empty_preset(self):
        assert self._validate({"name": "m", "upstream_base": "https://x", "client_preset": ""}) is None

    def test_valid_workbuddy(self):
        assert self._validate({"name": "m", "upstream_base": "https://x", "client_preset": "workbuddy"}) is None

    def test_valid_custom_no_headers(self):
        assert self._validate({"name": "m", "upstream_base": "https://x", "client_preset": "custom"}) is None

    def test_valid_custom_with_headers(self):
        raw = '[{"name":"X-Foo","value":"bar"}]'
        assert (
            self._validate(
                {"name": "m", "upstream_base": "https://x", "client_preset": "custom", "custom_headers": raw}
            )
            is None
        )

    def test_invalid_preset_name(self):
        err = self._validate({"name": "m", "upstream_base": "https://x", "client_preset": "bogus"})
        assert err is not None and "无效" in err

    def test_invalid_custom_headers_json(self):
        err = self._validate(
            {"name": "m", "upstream_base": "https://x", "client_preset": "custom", "custom_headers": "not json"}
        )
        assert err is not None and "JSON" in err

    def test_custom_headers_not_array(self):
        err = self._validate(
            {"name": "m", "upstream_base": "https://x", "client_preset": "custom", "custom_headers": '{"a":1}'}
        )
        assert err is not None and "数组" in err

    def test_custom_headers_forbidden_name(self):
        raw = '[{"name":"Authorization","value":"Bearer x"}]'
        err = self._validate(
            {"name": "m", "upstream_base": "https://x", "client_preset": "custom", "custom_headers": raw}
        )
        assert err is not None and "受控头" in err

    def test_custom_headers_empty_name_rejected(self):
        raw = '[{"name":"","value":"x"}]'
        err = self._validate(
            {"name": "m", "upstream_base": "https://x", "client_preset": "custom", "custom_headers": raw}
        )
        assert err is not None

    def test_missing_name_rejected(self):
        err = self._validate({"upstream_base": "https://x"})
        assert err is not None and "名称" in err

    def test_missing_upstream_base_rejected(self):
        err = self._validate({"name": "m"})
        assert err is not None and "上游地址" in err
