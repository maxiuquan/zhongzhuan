"""Tests for hosted tool recognition / validation / persistence / Q4 errors (T26).

覆盖 R-P1-46（识别不丢弃）、R-P1-48（tool_choice）、§4-Q4 错误契约。
判据映射见各测试 docstring。
"""

from __future__ import annotations

import pytest

from zhongzhuan.proxy.protocol.responses_models import (
    Capability,
    ErrorClass,
    HostedToolSpec,
    TerminalReason,
)
from zhongzhuan.responses_v3.capability import DEFAULT_EMULATED_CAPABILITIES
from zhongzhuan.responses_v3.hosted_tools import (
    HOSTED_CAPABILITIES,
    HOSTED_TOOL_TYPES,
    UNSUPPORTED_TOOL_MESSAGE,
    HostedToolRecognizer,
    HostedToolValidator,
    build_runtime_unavailable_event,
    build_unsupported_tool_error,
    validate_tool_choice,
)


# ---------------------------------------------------------------------------
# 判据①：7 类 hosted 能力全部可被识别，且 function tool 交错时 param_path 用原始下标
# ---------------------------------------------------------------------------


def test_hosted_tool_type_and_capability_counts():
    """判据①：10 个 type 字符串映射到 7 类能力，无重复无遗漏。"""
    # 3 个 web_search 别名 + 2 个 computer 别名 + 5 个独立类型 = 10。
    assert len(HOSTED_TOOL_TYPES) == 10
    assert "web_search" in HOSTED_TOOL_TYPES
    assert "web_search_preview" in HOSTED_TOOL_TYPES
    assert "web_search_preview_2025_03_11" in HOSTED_TOOL_TYPES
    assert "computer_use_preview" in HOSTED_TOOL_TYPES
    assert "tool_search" in HOSTED_TOOL_TYPES

    # 去重后的能力面恰好 7 个。
    assert HOSTED_CAPABILITIES == frozenset(
        {
            Capability.WEB_SEARCH,
            Capability.FILE_SEARCH,
            Capability.COMPUTER,
            Capability.CODE_INTERPRETER,
            Capability.IMAGE_GENERATION,
            Capability.REMOTE_MCP,
            Capability.TOOL_SEARCH,
        }
    )
    assert len(HOSTED_CAPABILITIES) == 7


def test_recognize_interleaved_function_tools_uses_original_index():
    """判据①：``tools`` 数组里 function tool 与 hosted tool 交错时，``param_path``

    用的是 **原始 ``tools`` 数组下标**（不是 hosted tool 自序号），这样客户端看到的
    ``tools[2].type`` 能直接定位到自己写的第 3 个元素。
    """
    payload = {
        "tools": [
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "web_search"},  # -> tools[1]
            {"type": "function", "function": {"name": "calc"}},
            {"type": "file_search"},  # -> tools[3]
            {"type": "computer_use_preview"},  # -> tools[4]
        ],
    }
    specs = HostedToolRecognizer().recognize(payload)
    # 只识别到 3 个 hosted tool，function tool 被跳过。
    assert [s.tool_type for s in specs] == [
        "web_search",
        "file_search",
        "computer_use_preview",
    ]
    assert [s.param_path for s in specs] == [
        "tools[1].type",
        "tools[3].type",
        "tools[4].type",
    ]
    # 能力映射正确（含 computer 别名 -> COMPUTER）。
    assert specs[0].required_capability == Capability.WEB_SEARCH
    assert specs[1].required_capability == Capability.FILE_SEARCH
    assert specs[2].required_capability == Capability.COMPUTER


def test_recognize_no_tools_returns_empty():
    """判据①：``tools`` 缺失 / 非列表 / 空列表都不报错，返回空。"""
    rec = HostedToolRecognizer()
    assert rec.recognize({}) == []
    assert rec.recognize({"tools": "nope"}) == []
    assert rec.recognize({"tools": []}) == []


# ---------------------------------------------------------------------------
# 判据②：请求校验期返回 400 unsupported_tool，param 精确指向出问题的那个 tool
# ---------------------------------------------------------------------------


def _web_search_spec(param_path: str = "tools[1].type") -> HostedToolSpec:
    return HostedToolSpec(
        tool_type="web_search",
        raw={"type": "web_search"},
        required_capability=Capability.WEB_SEARCH,
        param_path=param_path,
    )


def _file_search_spec(param_path: str = "tools[1].type") -> HostedToolSpec:
    # file_search 既不在默认 emulated 集合、也不在上游透传集合，属于「真不支持」。
    return HostedToolSpec(
        tool_type="file_search",
        raw={"type": "file_search"},
        required_capability=Capability.FILE_SEARCH,
        param_path=param_path,
    )


def test_validate_returns_400_unsupported_tool_with_precise_param():
    """判据②：能力不可服务 -> 400 ``unsupported_tool``，``param`` 指向具体位置。

    用 ``file_search`` 而不是 ``web_search``：web_search 现在属于上游透传能力
    （``UPSTREAM_FORWARDED_CAPABILITIES``），默认即视为可服务、不会再 400。
    file_search 既不 emulated 也不 forwarded，仍是「真不支持」的代表。
    """
    validator = HostedToolValidator()  # 默认只模拟 stateful_responses/background
    err = validator.validate([_file_search_spec()], available=frozenset())
    assert err is not None
    assert err.error_class is ErrorClass.UNSUPPORTED_TOOL_CAPABILITY
    assert err.http_status == 400
    # param 精确指向出问题的那个 tool，而不是笼统的 tools。
    assert err.param == "tools[1].type"
    status, body = err.to_response()
    assert status == 400
    assert body["error"]["code"] == "unsupported_tool"
    assert body["error"]["param"] == "tools[1].type"
    assert "file_search" in body["error"]["message"]


def test_validate_servable_capability_passes():
    """判据②（反向）：能力已在 available 中则通过，不报错。"""
    validator = HostedToolValidator()
    err = validator.validate(
        [_web_search_spec()],
        available=frozenset({Capability.WEB_SEARCH}),
    )
    assert err is None


def test_build_unsupported_tool_error_shape():
    """判据②：``build_unsupported_tool_error`` 渲染的错误体与 §4-Q4 一致。"""
    spec = _web_search_spec("tools[0].type")
    err = build_unsupported_tool_error(spec)
    assert err.error_class is ErrorClass.UNSUPPORTED_TOOL_CAPABILITY
    assert err.param == "tools[0].type"
    # 消息已按字段渲染（模板里的 {tool_type} 占位符不复存在）。
    assert "{tool_type}" not in err.message
    assert Capability.WEB_SEARCH.value in err.message


# ---------------------------------------------------------------------------
# 判据③：validate_tool_choice —— 合法形态通过；非法形态返回
#          INVALID_TOOL_ARGUMENTS（400），**不是** unsupported_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param(None, id="absent-is-ok"),
        pytest.param("auto", id="auto"),
        pytest.param("none", id="none"),
        pytest.param("required", id="required"),
        pytest.param("web_search", id="hosted-tool-name-string"),
        pytest.param({"type": "function", "function": {"name": "foo"}}, id="function-object"),
        pytest.param({"type": "web_search"}, id="hosted-object"),
        pytest.param({"type": "allowed_tools", "allowed_tools": ["x"]}, id="allowed_tools"),
    ],
)
def test_validate_tool_choice_valid(choice):
    """判据③：四类合法形态都应通过（返回 None）。"""
    payload = {} if choice is None else {"tool_choice": choice}
    assert validate_tool_choice(payload) is None


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param({"type": "bogus"}, id="unknown-type"),
        pytest.param({"type": "function"}, id="function-no-name"),
        pytest.param({"type": ""}, id="empty-type"),
        pytest.param(123, id="int"),
        pytest.param("", id="empty-string"),
        pytest.param({"foo": "bar"}, id="missing-type"),
    ],
)
def test_validate_tool_choice_invalid_is_invalid_arguments_not_unsupported(choice):
    """判据③：非法 tool_choice -> 400 ``invalid_tool_arguments``，且 **不是**

    ``unsupported_tool``。两者语义不同：前者是请求体写错，后者是能力无执行器。
    """
    err = validate_tool_choice({"tool_choice": choice})
    assert err is not None
    assert err.error_class is ErrorClass.INVALID_TOOL_ARGUMENTS
    assert err.error_class is not ErrorClass.UNSUPPORTED_TOOL_CAPABILITY
    assert err.http_status == 400
    assert err.param == "tool_choice"
    _, body = err.to_response()
    assert body["error"]["code"] == "invalid_tool_arguments"


# ---------------------------------------------------------------------------
# 判据④：运行期收尾事件 —— 严格模式与兼容模式的外壳不同，事实（reason）一致
# ---------------------------------------------------------------------------


def test_runtime_unavailable_strict_vs_compat():
    """判据④：运行期才发现不可执行，按 strict 决定外壳事件类型，但 terminal_reason

    永远是 ``capability_route_unavailable``。
    """
    spec = _web_search_spec("tools[1].type")

    strict = build_runtime_unavailable_event(spec, strict=True, response_id="resp_1")
    assert strict["type"] == "response.incomplete"
    assert strict["response"]["status"] == "incomplete"
    assert strict["response"]["terminal_reason"] == TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE.value
    assert strict["response"]["incomplete_details"]["reason"] == TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE.value

    compat = build_runtime_unavailable_event(spec, strict=False, response_id="resp_1")
    assert compat["type"] == "response.completed"
    assert compat["response"]["status"] == "completed"
    # 兼容模式也必须带 terminal_reason + incomplete_details（R-P1-22：这是唯一可诊断信号）。
    assert compat["response"]["terminal_reason"] == TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE.value
    assert compat["response"]["incomplete_details"]["reason"] == TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE.value
    # 两种模式事实一致。
    assert strict["response"]["terminal_reason"] == compat["response"]["terminal_reason"]


def test_runtime_unavailable_custom_message():
    """判据④：可覆盖消息，缺省时回落到标准 unsupported 文案。"""
    spec = _web_search_spec()
    ev = build_runtime_unavailable_event(spec, strict=True, message="custom reason")
    assert ev["response"]["incomplete_details"]["message"] == "custom reason"


def test_default_emulated_capabilities_excludes_hosted():
    """HONEST STUB 一致性：默认 emulated 不含任何 hosted 能力（避免谎称可模拟）。"""
    assert Capability.WEB_SEARCH not in DEFAULT_EMULATED_CAPABILITIES
    assert Capability.FILE_SEARCH not in DEFAULT_EMULATED_CAPABILITIES
