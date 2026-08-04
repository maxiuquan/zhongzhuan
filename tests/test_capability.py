"""CapabilityRouter 单测（T25 / R-P1-44 / R-P1-45）。

对齐任务表 T25 的完成判据：

* 判据① 三种执行模式各 1 例路由断言
  -> ``test_route_native`` / ``test_route_emulate`` / ``test_route_translate``
* 判据③ 原生模式不先降级为 Chat Completions
  -> ``test_native_mode_never_downgrades_to_chat``（另见 test_passthrough.py）
* 判据④ 启动期打印能力缺口清单
  -> ``test_startup_gap_report_lists_missing_capabilities`` 等
* 判据⑤ 生产模式 + strict_capability_startup=true + 缺口 -> 拒绝启动
  -> ``test_strict_startup_rejects_on_gap``
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from zhongzhuan.proxy.protocol.responses_errors import http_status_for
from zhongzhuan.proxy.protocol.responses_models import (
    Capability,
    ErrorClass,
    ExecutionMode,
    HostedToolSpec,
    SanitizedRequest,
)
from zhongzhuan.proxy.ratelimit import STATE_INVALID, KeyHealth, SlidingWindow
from zhongzhuan.responses_v3.capability import (
    PATH_CHAT_COMPLETIONS,
    PATH_MESSAGES,
    PATH_RESPONSES,
    REASON_NO_UPSTREAM,
    REASON_ROUTE_UNAVAILABLE,
    CapabilityError,
    CapabilityGap,
    CapabilityRouter,
    RouteDecision,
    StartupCapabilityError,
    StaticRouteRegistry,
    coerce_capabilities,
    coerce_execution_mode,
)

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeCfg:
    """最小配置对象：路由器只用 getattr 读，故普通 dataclass 即可注入。"""

    upstream_mode: str | None = None
    strict_capability_startup: bool = False
    required_capabilities: tuple[str, ...] = ()


def make_key(
    key_id: int = 1,
    *,
    capabilities: set[str] | None = None,
    upstream_mode: str = "bonded",
    protocol: str = "openai",
    available: bool = True,
) -> KeyHealth:
    key = KeyHealth(
        key_id=key_id,
        api_key="sk-test-{0}".format(key_id),
        window=SlidingWindow(60, 0),
        capabilities=set(capabilities or ()),
        upstream_mode=upstream_mode,
        upstream_protocol=protocol,
    )
    if not available:
        # invalid 是唯一与时间无关的不可用状态，测试里最稳。
        key.status = STATE_INVALID
    return key


def make_req(
    *caps: Capability,
    payload: dict | None = None,
) -> SanitizedRequest:
    """构造一个声明了若干 hosted 能力的 SanitizedRequest。"""
    hosted = [
        HostedToolSpec(
            tool_type=cap.value,
            raw={"type": cap.value},
            required_capability=cap,
            param_path="tools[{0}].type".format(index),
        )
        for index, cap in enumerate(caps)
    ]
    return SanitizedRequest(
        payload=payload if payload is not None else {"model": "gpt-4o"},
        hosted_tools=hosted,
        required_capabilities=frozenset(caps),
    )


def router_for(
    keys: list[KeyHealth],
    cfg: FakeCfg | None = None,
    **kwargs,
) -> CapabilityRouter:
    return CapabilityRouter(StaticRouteRegistry(keys), cfg or FakeCfg(), **kwargs)


# ---------------------------------------------------------------------------
# 判据① —— 三种执行模式各 1 例
# ---------------------------------------------------------------------------


def test_route_native():
    """上游声明 NATIVE 且覆盖所需能力 -> 原生直通 /v1/responses。"""
    key = make_key(capabilities={"web_search"}, upstream_mode="native")
    decision = router_for([key]).route(make_req(Capability.WEB_SEARCH), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.NATIVE
    assert decision.upstream_path == PATH_RESPONSES
    assert decision.key is key
    assert decision.granted == frozenset({Capability.WEB_SEARCH})
    assert decision.gaps == ()
    assert decision.is_native is True


def test_route_emulate():
    """上游不声明、但 v3 本地执行器能完整承载 -> EMULATE。"""
    key = make_key(upstream_mode="translate")
    decision = router_for([key]).route(make_req(Capability.STATEFUL_RESPONSES), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.EMULATE
    assert decision.granted == frozenset({Capability.STATEFUL_RESPONSES})
    assert "emulates" in decision.reason


def test_route_emulate_background_is_locally_served():
    """background 由 T24 的 BackgroundWorker 承载，同样走 EMULATE。"""
    key = make_key()
    decision = router_for([key]).route(make_req(Capability.BACKGROUND), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.EMULATE


def test_route_translate():
    """不需要任何 hosted 能力 -> 可证明等价降级到 Chat Completions。"""
    key = make_key()
    decision = router_for([key]).route(make_req(), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.TRANSLATE
    assert decision.upstream_path == PATH_CHAT_COMPLETIONS


def test_route_translate_anthropic_uses_messages_path():
    """Anthropic 上游降级到 /v1/messages 而不是 chat completions。"""
    key = make_key(protocol="anthropic")
    decision = router_for([key]).route(make_req(), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.TRANSLATE
    assert decision.upstream_path == PATH_MESSAGES


# ---------------------------------------------------------------------------
# 判据③ —— 原生模式不先降级为 Chat Completions
# ---------------------------------------------------------------------------


def test_native_mode_never_downgrades_to_chat():
    """upstream_mode=responses_native：即使上游没声明能力也不降级。"""
    key = make_key(upstream_mode="native")  # 未声明 web_search
    cfg = FakeCfg(upstream_mode="responses_native")
    decision = router_for([key], cfg).route(make_req(Capability.WEB_SEARCH), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.NATIVE
    assert decision.upstream_path == PATH_RESPONSES
    assert PATH_CHAT_COMPLETIONS not in decision.upstream_path
    # 未声明的能力如实进 gaps —— 直通但不假装成功（R-P1-45）。
    assert [g.capability for g in decision.gaps] == [Capability.WEB_SEARCH]
    assert decision.gaps[0].param_path == "tools[0].type"
    assert decision.granted == frozenset()


def test_forced_native_prefers_a_fully_declaring_key():
    """强制原生时仍优先挑「能力声明完整」的 key，而不是列表里的第一个。"""
    plain = make_key(1, upstream_mode="native")
    capable = make_key(2, capabilities={"code_interpreter"}, upstream_mode="native")
    cfg = FakeCfg(upstream_mode="responses_native")
    decision = router_for([plain, capable], cfg).route(
        make_req(Capability.CODE_INTERPRETER),
        [plain, capable],
    )

    assert isinstance(decision, RouteDecision)
    assert decision.key is capable
    assert decision.gaps == ()
    assert decision.granted == frozenset({Capability.CODE_INTERPRETER})


def test_forced_native_without_any_available_key_returns_error():
    """强制原生但候选全不可用 -> 503，而不是硬发一个必然失败的请求。"""
    key = make_key(capabilities={"web_search"}, upstream_mode="native", available=False)
    cfg = FakeCfg(upstream_mode="responses_native")
    result = router_for([key], cfg).route(make_req(Capability.WEB_SEARCH), [key])

    assert isinstance(result, CapabilityError)
    assert result.error_class is ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE


def test_native_key_missing_capability_does_not_win_native():
    """未强制原生时，能力声明不全的 NATIVE key 不能冒充原生直通。"""
    key = make_key(capabilities={"file_search"}, upstream_mode="native")
    result = router_for([key]).route(make_req(Capability.WEB_SEARCH), [key])

    assert isinstance(result, CapabilityError)
    assert result.error_class is ErrorClass.UNSUPPORTED_TOOL_CAPABILITY


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_route_unsupported_capability_returns_error():
    """required capability 无人承载 -> 400 + param 指向 tools[N].type。"""
    key = make_key()
    req = make_req(Capability.WEB_SEARCH, Capability.COMPUTER)
    result = router_for([key]).route(req, [key])

    assert isinstance(result, CapabilityError)
    assert result.error_class is ErrorClass.UNSUPPORTED_TOOL_CAPABILITY
    assert result.http_status == 400
    assert result.param.startswith("tools[")
    assert result.param.endswith("].type")
    assert {g.capability for g in result.gaps} == {
        Capability.WEB_SEARCH,
        Capability.COMPUTER,
    }
    assert all(g.reason == REASON_NO_UPSTREAM for g in result.gaps)

    status, body = result.to_response()
    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "unsupported_tool"
    assert body["error"]["param"] == result.param


def test_route_unsupported_error_param_matches_the_offending_tool():
    """param 必须指向真正缺能力的那个 tool，而不是永远的 tools[0]。"""
    key = make_key(capabilities={"web_search"}, upstream_mode="native")
    req = make_req(Capability.WEB_SEARCH, Capability.IMAGE_GENERATION)
    result = router_for([key]).route(req, [key])

    assert isinstance(result, CapabilityError)
    assert result.param == "tools[1].type"


def test_route_unavailable_returns_503():
    """能力有人声明、但此刻全部熔断 -> 503（可重试语义）。"""
    key = make_key(capabilities={"web_search"}, upstream_mode="native", available=False)
    result = router_for([key]).route(make_req(Capability.WEB_SEARCH), [key])

    assert isinstance(result, CapabilityError)
    assert result.error_class is ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE
    assert result.http_status == 503
    assert http_status_for(result.error_class) == 503
    assert [g.reason for g in result.gaps] == [REASON_ROUTE_UNAVAILABLE]

    status, body = result.to_response()
    assert status == 503
    assert body["error"]["code"] == "capability_route_unavailable"


def test_unavailable_and_unsupported_are_not_conflated():
    """同一份请求：key 可用 -> 400；key 不可用 -> 503。两者不可合并。"""
    req = make_req(Capability.WEB_SEARCH)
    declaring = make_key(capabilities={"web_search"}, upstream_mode="native")
    other = make_key(2, capabilities={"file_search"}, upstream_mode="native")

    down = make_key(
        3,
        capabilities={"web_search"},
        upstream_mode="native",
        available=False,
    )
    unavailable = router_for([down]).route(req, [down])
    unsupported = router_for([other]).route(req, [other])

    assert isinstance(unavailable, CapabilityError)
    assert isinstance(unsupported, CapabilityError)
    assert unavailable.error_class is ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE
    assert unsupported.error_class is ErrorClass.UNSUPPORTED_TOOL_CAPABILITY
    # 而 key 恢复可用时同一请求必须成功路由。
    assert isinstance(router_for([declaring]).route(req, [declaring]), RouteDecision)


def test_route_with_no_candidates_at_all():
    """完全没有候选 key -> 503 而不是崩溃。"""
    result = router_for([]).route(make_req(), [])

    assert isinstance(result, CapabilityError)
    assert result.error_class is ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE


def test_route_skips_unavailable_and_picks_healthy_key():
    """候选里混有熔断 key 时，路由跳过它选健康的那个。"""
    dead = make_key(1, capabilities={"web_search"}, upstream_mode="native", available=False)
    alive = make_key(2, capabilities={"web_search"}, upstream_mode="native")
    decision = router_for([dead, alive]).route(make_req(Capability.WEB_SEARCH), [dead, alive])

    assert isinstance(decision, RouteDecision)
    assert decision.key is alive


# ---------------------------------------------------------------------------
# 判据④ —— 启动期能力缺口清单
# ---------------------------------------------------------------------------


def test_startup_gap_report_lists_missing_capabilities():
    """承诺提供 web_search / computer，但注册表里只有 web_search。"""
    keys = [make_key(capabilities={"web_search"}, upstream_mode="native")]
    cfg = FakeCfg(required_capabilities=("web_search", "computer"))
    gaps = router_for(keys, cfg).startup_gap_report()

    assert [g.capability for g in gaps] == [Capability.COMPUTER]
    assert gaps[0].reason == REASON_NO_UPSTREAM
    assert "computer" in gaps[0].describe()


def test_startup_gap_report_empty_when_all_declared():
    keys = [make_key(capabilities={"web_search", "computer"}, upstream_mode="native")]
    cfg = FakeCfg(required_capabilities=("web_search", "computer"))

    assert router_for(keys, cfg).startup_gap_report() == []


def test_startup_gap_report_ignores_locally_emulated_capabilities():
    """本地能完整模拟的能力不算缺口 —— 否则每个部署都被噪音淹没。"""
    cfg = FakeCfg(required_capabilities=("stateful_responses", "background"))

    assert router_for([make_key()], cfg).startup_gap_report() == []


def test_startup_gap_report_flags_declared_but_unavailable_route():
    """声明了但 route 全部宕机 -> route unavailable 缺口。"""
    keys = [make_key(capabilities={"web_search"}, upstream_mode="native", available=False)]
    cfg = FakeCfg(required_capabilities=("web_search",))
    gaps = router_for(keys, cfg).startup_gap_report()

    assert [g.reason for g in gaps] == [REASON_ROUTE_UNAVAILABLE]


def test_startup_gap_report_is_empty_when_nothing_promised():
    """没有 required_capabilities 配置 = 什么都没承诺 = 没有缺口。"""
    assert router_for([make_key()], FakeCfg()).startup_gap_report() == []


def test_startup_gap_report_is_deterministically_ordered():
    """清单顺序稳定（按能力名排序），启动日志才能被 diff。"""
    cfg = FakeCfg(required_capabilities=("web_search", "computer", "image_generation"))
    gaps = router_for([make_key()], cfg).startup_gap_report()

    assert [g.capability.value for g in gaps] == [
        "computer",
        "image_generation",
        "web_search",
    ]


# ---------------------------------------------------------------------------
# 判据⑤ —— strict_capability_startup fail closed
# ---------------------------------------------------------------------------


def test_assert_startup_ok_raises_when_strict():
    cfg = FakeCfg(required_capabilities=("computer",))
    router = router_for([make_key()], cfg)

    with pytest.raises(StartupCapabilityError) as excinfo:
        router.assert_startup_ok(strict=True)

    assert [g.capability for g in excinfo.value.gaps] == [Capability.COMPUTER]
    assert "computer" in str(excinfo.value)
    assert "strict_capability_startup=true" in str(excinfo.value)


def test_assert_startup_ok_warns_when_not_strict():
    """strict=False：返回缺口清单供调用方 WARN，但不阻断启动。"""
    cfg = FakeCfg(required_capabilities=("computer",))
    gaps = router_for([make_key()], cfg).assert_startup_ok(strict=False)

    assert [g.capability for g in gaps] == [Capability.COMPUTER]


def test_strict_startup_rejects_on_gap():
    """判据⑤：生产模式 + strict_capability_startup=true + 缺口 -> 拒绝启动。"""
    keys = [make_key(capabilities={"web_search"}, upstream_mode="native")]
    prod = FakeCfg(
        strict_capability_startup=True,
        required_capabilities=("web_search", "code_interpreter"),
    )
    dev = FakeCfg(
        strict_capability_startup=False,
        required_capabilities=("web_search", "code_interpreter"),
    )

    # strict 取自配置，无需显式传参。
    with pytest.raises(StartupCapabilityError):
        router_for(keys, prod).assert_startup_ok()

    gaps = router_for(keys, dev).assert_startup_ok()
    assert [g.capability for g in gaps] == [Capability.CODE_INTERPRETER]


def test_assert_startup_ok_passes_without_gaps_even_when_strict():
    keys = [make_key(capabilities={"web_search"}, upstream_mode="native")]
    cfg = FakeCfg(strict_capability_startup=True, required_capabilities=("web_search",))

    assert router_for(keys, cfg).assert_startup_ok() == []


# ---------------------------------------------------------------------------
# 类型归一 / KeyHealth 视图 / 兼容性
# ---------------------------------------------------------------------------


def test_key_health_typed_views():
    key = make_key(capabilities={"web_search", "nope"}, upstream_mode="responses_native")

    assert key.declared_capabilities() == frozenset({Capability.WEB_SEARCH})
    assert key.execution_mode() is ExecutionMode.NATIVE


def test_key_health_defaults_stay_backwards_compatible():
    """T07 的字段契约不变：str 集合 + "bonded"，未声明按 TRANSLATE 处理。"""
    key = KeyHealth(key_id=1, api_key="k", window=SlidingWindow(60, 0))

    assert key.capabilities == set()
    assert key.upstream_mode == "bonded"
    assert key.declared_capabilities() == frozenset()
    assert key.execution_mode() is ExecutionMode.TRANSLATE


def test_coerce_helpers_are_lenient():
    assert coerce_capabilities(None) == frozenset()
    assert coerce_capabilities(["web_search", Capability.COMPUTER, "typo", ""]) == (
        frozenset({Capability.WEB_SEARCH, Capability.COMPUTER})
    )
    assert coerce_execution_mode("responses_native") is ExecutionMode.NATIVE
    assert coerce_execution_mode("bonded") is ExecutionMode.TRANSLATE
    assert coerce_execution_mode(None) is ExecutionMode.TRANSLATE
    assert coerce_execution_mode("garbage") is ExecutionMode.TRANSLATE
    assert coerce_execution_mode(ExecutionMode.EMULATE) is ExecutionMode.EMULATE


def test_router_accepts_custom_emulated_set():
    """T26 落地 hosted 执行器后只需注入新集合，无需改路由器。"""
    key = make_key()
    router = router_for([key], emulated=[Capability.FILE_SEARCH])
    decision = router.route(make_req(Capability.FILE_SEARCH), [key])

    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.EMULATE
    assert router.emulated_capabilities == frozenset({Capability.FILE_SEARCH})


def test_default_emulated_set_excludes_unimplemented_hosted_tools():
    """诚实性守卫：没有本地执行器的 hosted 能力不得混进默认可模拟集合。"""
    router = router_for([make_key()])

    for cap in (
        Capability.WEB_SEARCH,
        Capability.FILE_SEARCH,
        Capability.COMPUTER,
        Capability.CODE_INTERPRETER,
        Capability.IMAGE_GENERATION,
        Capability.REMOTE_MCP,
        Capability.TOOL_SEARCH,
    ):
        assert cap not in router.emulated_capabilities


def test_forced_mode_property():
    assert router_for([], FakeCfg(upstream_mode="responses_native")).forced_mode is (ExecutionMode.NATIVE)
    assert router_for([], FakeCfg(upstream_mode="bonded")).forced_mode is None
    assert router_for([], FakeCfg()).forced_mode is None


def test_capability_gap_is_frozen():
    gap = CapabilityGap(Capability.WEB_SEARCH, REASON_NO_UPSTREAM, "tools[0].type")

    with pytest.raises(Exception):
        gap.capability = Capability.COMPUTER  # type: ignore[misc]


def test_router_works_with_a_duck_typed_registry():
    """注册表只要有 all_keys() 就能用，缺的方法回落到 KeyHealth 自己。"""

    @dataclass
    class MinimalRegistry:
        keys: list = field(default_factory=list)

        def all_keys(self):
            return tuple(self.keys)

    key = make_key(capabilities={"web_search"}, upstream_mode="native")
    router = CapabilityRouter(
        MinimalRegistry([key]),
        FakeCfg(required_capabilities=("web_search",)),
    )

    decision = router.route(make_req(Capability.WEB_SEARCH), [key])
    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.NATIVE
    assert router.startup_gap_report() == []
