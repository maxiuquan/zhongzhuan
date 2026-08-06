"""能力路由器：原生直通 > 本地模拟 > 等价降级（T25 / R-P1-44 / R-P1-45）。

为什么需要这一层
----------------
Responses API 的 hosted tool（``web_search`` / ``file_search`` / ``computer`` /
``code_interpreter`` / ``image_generation`` / ``mcp`` / ``tool_search``）不是
普通的 function call：它们要求**上游自己**具备执行器。历史实现把「没有 name」
的 hosted tool 直接丢弃，然后一律降级到 Chat Completions —— 客户端拿到 200 与
一段空洞的文本，却永远不知道自己请求的能力从未被执行。R-P1-45 的原文是
「运行时不假装成功」，这个模块就是那句话的执行体。

三级优先级（§3.9）
------------------
1. **NATIVE** —— 上游自己声明了该能力，请求原样发到 ``/v1/responses``，
   item / event 语义不被改写（R-P1-44）。
2. **EMULATE** —— v3 桥接本身能够完整模拟该能力（例如 ``stateful_responses``
   由 ResponseStore 承载、``background`` 由 BackgroundWorker 承载），模型轮次
   仍然走上游，但能力由本地执行器补齐。
3. **TRANSLATE** —— 只有在**不需要任何 hosted 能力**时才允许等价降级到
   ``/v1/chat/completions`` / ``/v1/messages``。「可证明等价」的前提是没有语义
   会在转换中丢失；一旦请求带着无人承载的 hosted 能力，降级就是伪造成功，
   此时返回标准错误而不是一个漂亮的空回答。

两种失败是不同的故障（§4-Q4）
-----------------------------
* 全局**没有任何** route 声明该能力 → 这是配置问题，客户端改请求才有用 →
  ``UNSUPPORTED_TOOL_CAPABILITY``（400，``param`` 指向 ``tools[N].type``）。
* 有 route 声明、但此刻全部熔断 / 限流 / 宕机 → 这是暂时性故障，重试有用 →
  ``CAPABILITY_ROUTE_UNAVAILABLE``（503）。

把两者压成同一个错误码会让客户端做出错误决策，所以它们在这里被严格区分。

启动期暴露缺口（R-P1-45 / 判据④⑤）
-----------------------------------
:meth:`CapabilityRouter.startup_gap_report` 在启动 / 配置阶段就把「声明要提供
但没有执行器」的能力列出来；生产模式下 ``strict_capability_startup: true``
时 :meth:`CapabilityRouter.assert_startup_ok` 直接 fail closed，拒绝启动 ——
而不是等到第一个真实请求打进来才发现没人能干活。

HONEST STUB
-----------
* :data:`DEFAULT_EMULATED_CAPABILITIES` 只列出 **已经真实实现** 的两项。
  ``file_search`` / ``tool_search`` 等 hosted 执行器属于 T26，在它们落地之前
  谎称可模拟就等于违反 R-P1-45，因此不写进默认集合；T26 通过构造参数
  ``emulated=`` 扩展即可，无需改这里。
* 真正的网络转发不在本模块，见 :mod:`.passthrough`。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..proxy.protocol.responses_errors import http_status_for, to_error_response
from ..proxy.protocol.responses_models import (
    Capability,
    ErrorClass,
    ExecutionMode,
    SanitizedRequest,
)
from ..proxy.ratelimit import KeyHealth

# ---------------------------------------------------------------------------
# 1. 常量
# ---------------------------------------------------------------------------

#: 原生 Responses 上游路径。NATIVE 决策只会指向这一个路径（判据②③）。
PATH_RESPONSES: str = "/v1/responses"

#: 等价降级路径：OpenAI 兼容上游。
PATH_CHAT_COMPLETIONS: str = "/v1/chat/completions"

#: 等价降级路径：Anthropic 上游。
PATH_MESSAGES: str = "/v1/messages"

#: v3 桥接**自己**就能完整承载的能力（不需要上游声明）。
#:
#: * ``STATEFUL_RESPONSES`` —— ResponseStore（T21）持久化 response / input items
#:   / event log，``previous_response_id`` 由 ChainResolver（T22）恢复。
#: * ``BACKGROUND`` —— BackgroundWorker（T24）提供 lease / heartbeat / cancel /
#:   catch-up 全套语义。
#:
#: 其余七项 hosted 能力**没有**本地执行器，故意留空：见模块头 HONEST STUB。
DEFAULT_EMULATED_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.STATEFUL_RESPONSES,
        Capability.BACKGROUND,
    }
)

#: 上游透传能力：桥接自身**不执行**，但原样把工具转发给上游、由上游执行。
#: 与 ``EMULATED`` 的区别在于没有本地执行器 —— 中继只负责「不拦截、不伪造、
#: 带工具透传」。当前仅 ``WEB_SEARCH``：OpenAI 系 / freemodel.dev 等上游在
#: Responses 与 Chat Completions 两条路径都原生支持 web 搜索，中继无需本地
#: 搜索后端即可放行。若某上游确实不支持，由上游自己返回错误，中继不必假装有
#: 能力（R-P1-45）。
UPSTREAM_FORWARDED_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.WEB_SEARCH,
    }
)

#: ``KeyHealth.upstream_mode`` 的字符串取值 -> :class:`ExecutionMode`。
#: 历史默认值 ``"bonded"`` 表示「未声明」，按最保守的 TRANSLATE 处理。
_MODE_ALIASES: dict[str, ExecutionMode] = {
    "native": ExecutionMode.NATIVE,
    "responses_native": ExecutionMode.NATIVE,
    "emulate": ExecutionMode.EMULATE,
    "translate": ExecutionMode.TRANSLATE,
    "bonded": ExecutionMode.TRANSLATE,
}

#: 缺口原因常量（§3.9 注释里给出的两种文案）。
REASON_NO_UPSTREAM: str = "no upstream declares capability"
REASON_ROUTE_UNAVAILABLE: str = "route unavailable"


def coerce_execution_mode(value: Any, default: ExecutionMode = ExecutionMode.TRANSLATE) -> ExecutionMode:
    """把配置里的宽松取值归一成 :class:`ExecutionMode`。

    接受 ``ExecutionMode`` 本身、``"native"`` / ``"responses_native"`` 之类的
    字符串，以及 ``None``。无法识别时返回 ``default`` —— 路由决策绝不能因为
    一个拼错的配置字符串而抛异常。
    """
    if value is None:
        return default
    if isinstance(value, ExecutionMode):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return _MODE_ALIASES.get(text, default)


def coerce_capabilities(values: Iterable[Any] | None) -> frozenset[Capability]:
    """把宽松的能力声明（字符串 / 枚举 / 混合）归一成 ``frozenset[Capability]``。

    无法识别的名字被静默忽略：一个手滑写错的能力名不应该让整个 key 不可用，
    它只会表现为「该能力没人声明」，然后被缺口报告如实抓出来。
    """
    if not values:
        return frozenset()
    out: set[Capability] = set()
    for raw in values:
        if isinstance(raw, Capability):
            out.add(raw)
            continue
        text = str(raw).strip().lower()
        if not text:
            continue
        try:
            out.add(Capability(text))
        except ValueError:
            continue
    return frozenset(out)


# ---------------------------------------------------------------------------
# 2. 数据结构（§3.9）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    """一项「要求存在、但没有执行器」的能力缺口。"""

    capability: Capability
    reason: str = REASON_NO_UPSTREAM
    param_path: str = ""  # 例如 "tools[2].type"

    def describe(self) -> str:
        """人类可读的一行描述，用于启动日志与异常消息。"""
        tail = " ({0})".format(self.param_path) if self.param_path else ""
        return "{0}: {1}{2}".format(self.capability.value, self.reason, tail)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一次成功的路由决策。"""

    mode: ExecutionMode
    key: KeyHealth
    upstream_path: str  # /v1/responses | /v1/chat/completions | /v1/messages
    granted: frozenset[Capability] = frozenset()
    gaps: tuple[CapabilityGap, ...] = ()
    reason: str = ""

    @property
    def is_native(self) -> bool:
        """是否走原生直通（判据③：为真时绝不允许出现 chat/completions）。"""
        return self.mode is ExecutionMode.NATIVE


@dataclass(frozen=True, slots=True)
class CapabilityError:
    """路由失败。

    刻意做成**返回值**而不是异常：``route()`` 的两种失败都是可预期的业务结果，
    调用方需要拿它去渲染标准 OpenAI 错误体（或写进 SSE ``response.failed``），
    而不是在调用栈上层做异常翻译。
    """

    error_class: ErrorClass
    message: str = ""
    param: str = ""
    gaps: tuple[CapabilityGap, ...] = ()

    @property
    def http_status(self) -> int:
        """§10.2 规定的 HTTP 状态码（400 / 503）。"""
        return http_status_for(self.error_class)

    def to_response(self) -> tuple[int, dict[str, Any]]:
        """渲染成 ``(status, {"error": {...}})``；消息已脱敏截断。"""
        return to_error_response(self.error_class, self.message, self.param or None)


class StartupCapabilityError(RuntimeError):
    """``strict_capability_startup: true`` 且存在能力缺口时抛出（判据⑤）。

    携带结构化的 :attr:`gaps`，这样启动脚本既能打印清单也能上报监控，不必去
    正则解析异常消息。
    """

    def __init__(self, gaps: Sequence[CapabilityGap]) -> None:
        self.gaps: tuple[CapabilityGap, ...] = tuple(gaps)
        detail = "; ".join(gap.describe() for gap in self.gaps) or "(none)"
        super().__init__("capability gaps detected with strict_capability_startup=true: " + detail)


# ---------------------------------------------------------------------------
# 3. RouteRegistry
# ---------------------------------------------------------------------------


@runtime_checkable
class RouteRegistry(Protocol):
    """路由注册表的最小接口。

    仓库里还没有独立的 ``proxy/route_registry.py``（调度器目前直接持有
    ``list[KeyHealth]``），所以这里只声明 :class:`CapabilityRouter` 真正需要的
    四个方法，并提供 :class:`StaticRouteRegistry` 作为默认实现。等真正的注册表
    落地时，只要满足这个 Protocol 就能直接替换，不必改路由器。
    """

    def all_keys(self) -> Sequence[KeyHealth]: ...
    def declared_capabilities(self, key: KeyHealth) -> frozenset[Capability]: ...
    def upstream_mode(self, key: KeyHealth) -> ExecutionMode: ...
    def is_available(self, key: KeyHealth) -> bool: ...


@dataclass
class StaticRouteRegistry:
    """由一组 :class:`KeyHealth` 构成的只读注册表。

    能力 / 模式 / 可用性全部从 ``KeyHealth`` 自身读取，因此它和调度器看到的是
    同一份事实 —— 不存在「注册表说可用、调度器说熔断」的分裂。
    """

    keys: list[KeyHealth] = field(default_factory=list)

    def all_keys(self) -> Sequence[KeyHealth]:
        return tuple(self.keys)

    def declared_capabilities(self, key: KeyHealth) -> frozenset[Capability]:
        return key.declared_capabilities()

    def upstream_mode(self, key: KeyHealth) -> ExecutionMode:
        return key.execution_mode()

    def is_available(self, key: KeyHealth) -> bool:
        return bool(key.is_available())


def _declared_of(registry: Any, key: KeyHealth) -> frozenset[Capability]:
    """注册表优先，缺方法时回落到 ``KeyHealth`` 自己的声明。"""
    fn = getattr(registry, "declared_capabilities", None)
    if callable(fn):
        return coerce_capabilities(fn(key))
    return key.declared_capabilities()


def _mode_of(registry: Any, key: KeyHealth) -> ExecutionMode:
    fn = getattr(registry, "upstream_mode", None)
    if callable(fn):
        return coerce_execution_mode(fn(key))
    return key.execution_mode()


def _available(registry: Any, key: KeyHealth) -> bool:
    fn = getattr(registry, "is_available", None)
    if callable(fn):
        return bool(fn(key))
    return bool(key.is_available())


def _all_keys(registry: Any) -> tuple[KeyHealth, ...]:
    fn = getattr(registry, "all_keys", None)
    if callable(fn):
        return tuple(fn() or ())
    return ()


# ---------------------------------------------------------------------------
# 4. CapabilityRouter
# ---------------------------------------------------------------------------


class CapabilityRouter:
    """按「原生直通 > 本地模拟 > 等价降级」三级优先级选择执行模式（§3.9）。"""

    def __init__(
        self,
        registry: RouteRegistry | Any,
        cfg: Any = None,
        *,
        emulated: Iterable[Capability] | None = None,
        forwarded: Iterable[Capability] | None = None,
    ) -> None:
        self._registry = registry
        self._cfg = cfg
        #: 配置强制的执行模式；``responses_native`` 时禁止任何降级（R-P1-44）。
        self._forced_mode: ExecutionMode | None = self._read_forced_mode(cfg)
        self._strict_default: bool = bool(getattr(cfg, "strict_capability_startup", False))
        self._emulated: frozenset[Capability] = (
            coerce_capabilities(emulated) if emulated is not None else DEFAULT_EMULATED_CAPABILITIES
        )
        #: 上游透传能力（中继不执行、原样转发的 hosted 能力，见模块常量）。
        self._forwarded: frozenset[Capability] = (
            coerce_capabilities(forwarded) if forwarded is not None else UPSTREAM_FORWARDED_CAPABILITIES
        )
        #: 部署声称要提供的能力；空集合表示「什么都没承诺」，于是没有缺口。
        self._required: frozenset[Capability] = coerce_capabilities(getattr(cfg, "required_capabilities", None))

    # -- 只读属性 --------------------------------------------------------

    @property
    def emulated_capabilities(self) -> frozenset[Capability]:
        """本地执行器能完整承载的能力集合。"""
        return self._emulated

    @property
    def forwarded_capabilities(self) -> frozenset[Capability]:
        """上游透传能力集合（中继不执行、原样转发给上游）。"""
        return self._forwarded

    @property
    def forced_mode(self) -> ExecutionMode | None:
        """配置层强制的执行模式；``None`` 表示由请求内容自行决定。"""
        return self._forced_mode

    @staticmethod
    def _read_forced_mode(cfg: Any) -> ExecutionMode | None:
        """从配置读取 ``upstream_mode``；未配置时返回 ``None``（不强制）。"""
        raw = getattr(cfg, "upstream_mode", None)
        if raw is None:
            return None
        if isinstance(raw, ExecutionMode):
            return raw
        text = str(raw).strip().lower()
        if not text or text == "bonded":
            return None
        return _MODE_ALIASES.get(text)

    # -- 路由 ------------------------------------------------------------

    def route(
        self,
        req: SanitizedRequest,
        candidates: Sequence[KeyHealth],
    ) -> RouteDecision | CapabilityError:
        """为 ``req`` 选出执行模式与上游路径，或给出标准错误。

        ``candidates`` 是调度器已经按模型 / 租户筛过的候选 key（可包含当前不可
        用的），本方法只负责能力维度的判定，不重复做权重调度。
        """
        required = coerce_capabilities(req.required_capabilities)
        # 上游透传能力由上游自己执行，中继只负责带工具转发，不承担、也不要求
        # 本地执行器或原生声明。从「路由必须保全」的集合里剔除，避免它们被误判
        # 为无人承载而 400。仍记在 ``granted`` 里如实反映「该能力由上游提供」。
        required_effective = required - self._forwarded
        available = [k for k in candidates if _available(self._registry, k)]

        # -- 0. 配置强制原生：R-P1-44「原生模式不得先降级为 Chat Completions」--
        if self._forced_mode is ExecutionMode.NATIVE:
            return self._forced_native(req, required, required_effective, candidates, available)

        # -- 1. NATIVE：上游自己声明了全部所需能力 --
        native = self._pick_native(required_effective, available)
        if native is not None:
            return RouteDecision(
                mode=ExecutionMode.NATIVE,
                key=native,
                upstream_path=PATH_RESPONSES,
                granted=required,
                reason="upstream declares every required capability",
            )

        if available:
            key = available[0]

            # -- 2. TRANSLATE：没有任何「需本地保全」的 hosted 能力时的等价降级 --
            # 先于 EMULATE 判定：``required_effective`` 为空时不存在「被模拟的
            # 能力」（透传类能力由上游承载），把它算作 EMULATE 会让纯文本 +
            # web_search 请求错误地宣称本地执行器参与过。
            if not required_effective:
                return RouteDecision(
                    mode=ExecutionMode.TRANSLATE,
                    key=key,
                    upstream_path=_translate_path(key),
                    granted=required,
                    reason="no locally-executed capability required; upstream forwards hosted tools",
                )

            # -- 3. EMULATE：缺的部分本地执行器能补齐 --
            # 取「缺得最少」的可用 key，而不是列表里的第一个：候选顺序由调度器
            # 的权重决定，与能力覆盖无关。
            best = min(available, key=lambda k: len(self._unmet(required_effective, k)))
            unmet = self._unmet(required_effective, best)
            if unmet <= self._emulated:
                return RouteDecision(
                    mode=ExecutionMode.EMULATE,
                    key=best,
                    upstream_path=_translate_path(best),
                    granted=required,
                    reason="bridge emulates: " + (_join(unmet) or "(nothing missing)"),
                )

        # -- 4. 失败：区分「没人声明」与「声明了但不可用」--
        # 透传能力已在上游侧满足，不再计入缺口。
        return self._failure(req, required, candidates)

    # -- 路由内部实现 ----------------------------------------------------

    def _forced_native(
        self,
        req: SanitizedRequest,
        required: frozenset[Capability],
        required_effective: frozenset[Capability],
        candidates: Sequence[KeyHealth],
        available: Sequence[KeyHealth],
    ) -> RouteDecision | CapabilityError:
        """``upstream_mode: responses_native`` 下的强制直通。

        即便上游的能力声明不完整也**不降级**（否则就违反 R-P1-44 / 判据③）；
        未声明的部分如实进 ``gaps``，由调用方决定是审计还是告警 —— 唯独不会
        变成一次伪装成功的 Chat Completions 请求。透传能力由上游承载，不计入
        缺失。
        """
        preferred = self._pick_native(required_effective, available)
        key = preferred or (available[0] if available else None)
        if key is None:
            return self._failure(req, required, candidates)
        declared = _declared_of(self._registry, key)
        missing = required_effective - declared - self._emulated
        return RouteDecision(
            mode=ExecutionMode.NATIVE,
            key=key,
            upstream_path=PATH_RESPONSES,
            granted=required & (declared | self._emulated | self._forwarded),
            gaps=self._gaps_for(req, missing, REASON_NO_UPSTREAM),
            reason="upstream_mode=responses_native (passthrough forced)",
        )

    def _pick_native(
        self,
        required: frozenset[Capability],
        available: Sequence[KeyHealth],
    ) -> KeyHealth | None:
        """挑一个声明为 NATIVE 且覆盖全部所需能力的可用 key。"""
        for key in available:
            if _mode_of(self._registry, key) is not ExecutionMode.NATIVE:
                continue
            if required <= _declared_of(self._registry, key):
                return key
        return None

    def _unmet(self, required: frozenset[Capability], key: KeyHealth) -> frozenset[Capability]:
        """``key`` 声明之外、仍需别处兜底的能力。"""
        return required - _declared_of(self._registry, key)

    def _failure(
        self,
        req: SanitizedRequest,
        required: frozenset[Capability],
        candidates: Sequence[KeyHealth],
    ) -> CapabilityError:
        """构造 400 / 503 —— 两种故障语义完全不同，不可合并。"""
        pool = list(candidates) or list(_all_keys(self._registry))
        undeclared = sorted(
            (
                cap
                for cap in required
                if cap not in self._emulated
                and cap not in self._forwarded
                and not any(cap in _declared_of(self._registry, k) for k in pool)
            ),
            key=lambda c: c.value,
        )
        if undeclared:
            gaps = self._gaps_for(req, undeclared, REASON_NO_UPSTREAM)
            return CapabilityError(
                error_class=ErrorClass.UNSUPPORTED_TOOL_CAPABILITY,
                message="no route can serve capability: " + _join(undeclared),
                param=gaps[0].param_path if gaps else "",
                gaps=gaps,
            )
        # 走到这里说明能力有人声明（或本地可模拟），只是此刻没有可用 route。
        return CapabilityError(
            error_class=ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE,
            message=(
                "every route declaring the required capability is currently "
                "unavailable: " + (_join(sorted(required, key=lambda c: c.value)) or "(none)")
            ),
            gaps=self._gaps_for(req, required, REASON_ROUTE_UNAVAILABLE),
        )

    def _gaps_for(
        self,
        req: SanitizedRequest | None,
        caps: Iterable[Capability],
        reason: str,
    ) -> tuple[CapabilityGap, ...]:
        """把能力集合转成缺口列表，并回填 ``tools[N].type`` 参数路径。"""
        paths = _capability_param_paths(req)
        return tuple(
            CapabilityGap(capability=cap, reason=reason, param_path=paths.get(cap, ""))
            for cap in sorted(caps, key=lambda c: c.value)
        )

    # -- 启动期自检（判据④⑤）-------------------------------------------

    def startup_gap_report(self) -> list[CapabilityGap]:
        """扫描注册表，列出「承诺提供但无执行器」的能力缺口。

        判定口径：只检查配置里 ``required_capabilities`` 声明要提供的能力 ——
        没承诺的能力不算缺口，否则任何只跑纯文本的部署都会被 9 条噪音淹没。
        对每一项：

        * 本地可模拟           -> 不是缺口；
        * 有 key 声明且可用     -> 不是缺口；
        * 有 key 声明但全不可用 -> ``route unavailable``；
        * 无任何 key 声明       -> ``no upstream declares capability``。
        """
        keys = _all_keys(self._registry)
        report: list[CapabilityGap] = []
        for cap in sorted(self._required, key=lambda c: c.value):
            if cap in self._emulated or cap in self._forwarded:
                # 本地模拟或上游透传都算「已满足」，不算缺口。
                continue
            declaring = [k for k in keys if cap in _declared_of(self._registry, k)]
            if not declaring:
                report.append(CapabilityGap(cap, REASON_NO_UPSTREAM))
                continue
            if not any(_available(self._registry, k) for k in declaring):
                report.append(CapabilityGap(cap, REASON_ROUTE_UNAVAILABLE))
        return report

    def assert_startup_ok(self, *, strict: bool | None = None) -> list[CapabilityGap]:
        """存在缺口时按 ``strict`` 决定 fail closed 还是放行。

        返回缺口清单（可能为空），方便调用方打印 WARN —— 路由器自己不碰
        logging 配置，因为它同时被启动脚本、单测和管理端复用。

        ``strict`` 省略时取配置里的 ``strict_capability_startup``。
        """
        effective = self._strict_default if strict is None else bool(strict)
        gaps = self.startup_gap_report()
        if gaps and effective:
            raise StartupCapabilityError(gaps)
        return gaps


# ---------------------------------------------------------------------------
# 5. 模块级小工具
# ---------------------------------------------------------------------------


def _translate_path(key: KeyHealth) -> str:
    """按上游协议选择降级路径。"""
    protocol = (getattr(key, "upstream_protocol", "") or "").strip().lower()
    return PATH_MESSAGES if protocol == "anthropic" else PATH_CHAT_COMPLETIONS


def _join(caps: Iterable[Capability]) -> str:
    return ", ".join(cap.value for cap in caps)


def _capability_param_paths(req: SanitizedRequest | None) -> dict[Capability, str]:
    """``Capability -> tools[N].type``，取自 sanitizer 记录的 hosted tool 位置。"""
    if req is None:
        return {}
    paths: dict[Capability, str] = {}
    for index, spec in enumerate(getattr(req, "hosted_tools", ()) or ()):
        cap = getattr(spec, "required_capability", None)
        if cap is None or cap in paths:
            continue
        paths[cap] = getattr(spec, "param_path", "") or "tools[{0}].type".format(index)
    return paths


__all__ = [
    "PATH_RESPONSES",
    "PATH_CHAT_COMPLETIONS",
    "PATH_MESSAGES",
    "DEFAULT_EMULATED_CAPABILITIES",
    "UPSTREAM_FORWARDED_CAPABILITIES",
    "REASON_NO_UPSTREAM",
    "REASON_ROUTE_UNAVAILABLE",
    "coerce_execution_mode",
    "coerce_capabilities",
    "CapabilityGap",
    "RouteDecision",
    "CapabilityError",
    "StartupCapabilityError",
    "RouteRegistry",
    "StaticRouteRegistry",
    "CapabilityRouter",
]
