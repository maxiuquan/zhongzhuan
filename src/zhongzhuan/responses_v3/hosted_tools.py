"""hosted tool 识别 / 校验 / 持久化与 §4-Q4 错误语义（T26）。

覆盖 R-P1-46（不静默丢弃）、R-P1-47（审批 + 幂等 + 审计 + 租户隔离）、
R-P1-48（并行工具与 ``tool_choice``）以及 PRD §4-Q4 的完整错误契约。

要解决的问题
------------
历史实现把 ``tools`` 数组里「没有 ``name`` 字段」的条目当成畸形输入直接剔除，
然后照常调用模型 —— 客户端拿到 HTTP 200 和一段看起来很正常的文本，误以为模型
自己决定不调工具。这是 PRD §3 范围裁定表里明确点名的**反例**（闸门①与闸门④）。
hosted tool 本来就没有 name：它的身份是 ``type``，执行器在上游。

五道闸门里，本模块负责第 ①②④ 道：

* ① **请求侧不丢弃** —— :class:`HostedToolRecognizer` 按 ``type`` 识别，
  :meth:`HostedToolRecognizer.persist` 把每一条写进 ``tool_executions``。
  识别的下标 ``i`` 就是 ``param_path = "tools[i].type"``，错误体因此能精确
  指向客户端写错的那一行。
* ② 路由侧显式判定 —— 由 T25 的
  :class:`~.capability.CapabilityRouter` 承担，本模块只消费它的 ``available``。
* ④ **无能力则标准报错** —— :class:`HostedToolValidator` 在**请求校验阶段**
  （未连上游、未发任何 delta）返回 400 ``unsupported_tool``；运行期才暴露的
  同类故障由 :func:`build_runtime_unavailable_event` 走 SSE 收尾。

两个判定时机，两种形态（§4-Q4）
-------------------------------
=================================  ==========================================
时机                                行为
=================================  ==========================================
请求校验阶段（可静态判定）           HTTP 400，不开 SSE，``code=unsupported_tool``
上游中途返回不可执行的 tool call     SSE：安全关闭 -> ``response.incomplete``
                                    （严格）/ ``response.completed`` +
                                    ``incomplete_details``（兼容）-> ``[DONE]``，
                                    ``terminal_reason=capability_route_unavailable``
=================================  ==========================================

「请求期拒绝优先」不是风格偏好：请求期能判定却拖到运行期再报，客户端已经收了
一半 delta，无法安全重试。

HONEST STUB
-----------
* :func:`build_runtime_unavailable_event` **只构造事件 dict**，不碰 emitter、
  不管「安全关闭已开 item」那一步 —— 事件真正被写进 SSE 流是 T28 的接线工作。
* :class:`HostedToolRecognizer` 只识别与持久化；hosted tool 的**执行**在有原生
  上游时由 T25 的直通承担，没有原生上游时按 ④ 报错。本模块不模拟任何执行器，
  也不会声称能模拟（R-P1-45：运行时不假装成功）。
* :meth:`HostedToolRecognizer.persist` 写入的 ``approval_state`` 恒为 ``none``；
  把它翻成 ``pending`` 并驱动 ``mcp_approval_request`` 由 T27 的
  :meth:`~.mcp_client.McpClient.request_approval` 完成（已落地）。

例外：``mcp``
-------------
7 类 hosted tool 里只有 ``mcp`` 不走「无执行器 -> 标准错误」这条路。PRD §3.3 #4
把它裁定为 🟢 完整实现，执行器就在
:mod:`~zhongzhuan.responses_v3.mcp_client`。要让它对本模块的
:class:`HostedToolValidator` 可服务，构造时把
:attr:`~..proxy.protocol.responses_models.Capability.REMOTE_MCP` 加进
``emulated=``（默认集合仍不含它 —— 是否启用 MCP 由部署配置说了算，默认打开一个
能对外发网络请求的执行器不是安全的默认）。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..proxy.protocol.responses_errors import to_incomplete_details
from ..proxy.protocol.responses_models import (
    HOSTED_TOOL_CAPABILITY,
    NAMESPACE_TOOL_TYPE,
    Capability,
    ErrorClass,
    HostedToolSpec,
    TerminalReason,
)
from ..store.tool_executions import (
    APPROVAL_NONE,
    STATUS_RECOGNIZED,
    ToolExecutionStore,
)
from .capability import (
    DEFAULT_EMULATED_CAPABILITIES,
    UPSTREAM_FORWARDED_CAPABILITIES,
    CapabilityError,
)

# ---------------------------------------------------------------------------
# 1. 常量
# ---------------------------------------------------------------------------

#: 全部被识别为 hosted tool 的 ``type`` 字符串。
#:
#: 直接从 :data:`HOSTED_TOOL_CAPABILITY` 派生，**不另写一份**：一旦两处清单
#: 分叉，就会出现「schema 认得但路由不认得」的 tool，那正是 R-P1-46 要消灭的
#: 静默丢弃。当前 10 个字符串（含 3 个 ``web_search`` 别名与 2 个 ``computer``
#: 别名）映射到 7 类 hosted 能力。
HOSTED_TOOL_TYPES: frozenset[str] = frozenset(HOSTED_TOOL_CAPABILITY)

#: 7 类 hosted 能力（去掉别名后的实际能力面）。判据①按这个集合计数。
HOSTED_CAPABILITIES: frozenset[Capability] = frozenset(HOSTED_TOOL_CAPABILITY.values())

#: ``tool_choice`` 的三个字面量取值（R-P1-48）。
TOOL_CHOICE_LITERALS: frozenset[str] = frozenset({"auto", "none", "required"})

#: ``tool_choice`` 为对象时允许的 ``type``。``function`` 是 OpenAI 官方形态；
#: hosted tool 也可以被点名（``{"type": "web_search"}``），所以 hosted 的
#: type 全集同样合法。``namespace`` 是 Codex 26.x 的工具容器，也放行。
_TOOL_CHOICE_OBJECT_TYPES: frozenset[str] = frozenset({"function", "allowed_tools", "custom"}) | HOSTED_TOOL_TYPES | {NAMESPACE_TOOL_TYPE}

#: §4-Q4 给定的错误消息模板。逐字对齐 PRD 的建议错误体 —— 客户端可以拿
#: ``capability`` 名去查配置，拿 ``upstream_mode`` 提示去改部署。
UNSUPPORTED_TOOL_MESSAGE: str = (
    "Tool type '{tool_type}' is not supported by the selected model/upstream "
    "route. Configure an upstream with capability '{capability}' "
    "(upstream_mode: responses_native)."
)


# ---------------------------------------------------------------------------
# 1.5 配置驱动的能力面（T28 / T27 遗留 opt-in）
# ---------------------------------------------------------------------------


def hosted_tool_emulated_capabilities(cfg: Any | None = None) -> frozenset[Capability]:
    """从配置计算桥接自己能完整承载的 hosted 能力集合。

    默认取 :data:`DEFAULT_EMULATED_CAPABILITIES`（只含真正实现了的两项，不谎称
    能模拟 hosted 执行器）。当 ``cfg.hosted_tools.mcp_enabled = true`` 时把
    :attr:`Capability.REMOTE_MCP` 加进去——``mcp`` 是 PRD §3.3 #4 唯一 🟢 完整
    实现的 hosted tool，执行器在 T27 的 :class:`~.mcp_client.McpClient`。默认
    关闭是安全默认：不经过显式配置，谁也不该给每租户默认开出出网通道。
    """
    caps: set[Capability] = set(DEFAULT_EMULATED_CAPABILITIES)
    hosted = getattr(cfg, "hosted_tools", None)
    if hosted is not None and getattr(hosted, "mcp_enabled", False):
        caps.add(Capability.REMOTE_MCP)
    # FR-6 / APIAADBPW-REQ-MA-001：``tool_search`` 与 ``multi_agent`` 是一体能力，
    # 必须两个开关同时为真才视为可服务——避免「只开其一」的半残状态（暴露了
    # namespace 却无法执行，或能执行却没暴露）。两者皆开时，中继自行合成
    # ``tool_search_output`` 并就地执行 ``multi_agent_v1`` 调用，路由器不再 400
    # ``no route can serve capability: tool_search``。
    ma = getattr(cfg, "multi_agent", None)
    if (
        hosted is not None
        and getattr(hosted, "tool_search_enabled", False)
        and ma is not None
        and getattr(ma, "enabled", False)
    ):
        caps.add(Capability.TOOL_SEARCH)
    return frozenset(caps)


def resolve_mcp_executor(cfg: Any | None = None) -> Any | None:
    """按配置解析 Remote MCP 执行器（T28 / R-P1-46 闸门②的执行侧）。

    开关关闭 -> ``None``（请求携带 ``mcp`` tool 时由 validator 判 400
    ``unsupported_tool``）；开关打开 -> 返回一个 T27 的 :class:`~.mcp_client.McpClient`
    （惰性导入，避免把 store 依赖拖进本模块的静态导入图）。
    """
    if Capability.REMOTE_MCP not in hosted_tool_emulated_capabilities(cfg):
        return None
    from .mcp_client import McpClient  # noqa: PLC0415 - 惰性导入防循环

    return McpClient()


# ---------------------------------------------------------------------------
# 2. 识别与持久化（闸门①）
# ---------------------------------------------------------------------------


class HostedToolRecognizer:
    """从请求体里认出 hosted tool，并把它们持久化。"""

    def recognize(self, payload: Mapping[str, Any]) -> list[HostedToolSpec]:
        """扫描 ``payload["tools"]``，返回全部 hosted tool 的规格。

        ``param_path`` 用的是 **原始 ``tools`` 数组下标**，不是 hosted tool
        自身的序号 —— 客户端看到 ``tools[2].type`` 时能直接定位到自己写的第 3
        个元素。混在中间的 function tool 会让两种编号错位，所以这里必须用
        ``enumerate(tools)`` 的下标。

        非 hosted 的条目（普通 function tool）被跳过而不是报错：它们由既有的
        function call 路径处理，本模块无权对它们下判断。
        """
        tools = payload.get("tools")
        if not isinstance(tools, (list, tuple)):
            return []
        specs: list[HostedToolSpec] = []
        for index, tool in enumerate(tools):
            if not isinstance(tool, Mapping):
                continue
            tool_type = str(tool.get("type") or "")
            capability = HOSTED_TOOL_CAPABILITY.get(tool_type)
            if capability is None:
                continue
            specs.append(
                HostedToolSpec(
                    tool_type=tool_type,
                    raw=dict(tool),
                    required_capability=capability,
                    param_path="tools[{0}].type".format(index),
                )
            )
        return specs

    def required_capabilities(
        self,
        specs: list[HostedToolSpec],
    ) -> frozenset[Capability]:
        """``specs`` 需要的能力集合，可直接填进 ``SanitizedRequest``。"""
        return frozenset(spec.required_capability for spec in specs)

    async def persist(
        self,
        response_id: str,
        workspace_id: str,
        specs: list[HostedToolSpec],
        store: ToolExecutionStore,
    ) -> None:
        """把每条 spec 写进 ``tool_executions``（闸门①的「持久化」半边）。

        ``tool_seq`` 用 ``specs`` 列表下标而非 ``tools`` 数组下标：这张表记录的
        是「代理识别到的第 N 个 hosted tool」，读取时按它排序才连续。原始数组
        位置已经由 ``param_path`` 记在错误体里，两者各司其职。
        """
        for tool_seq, spec in enumerate(specs):
            await store.record(
                response_id=response_id,
                workspace_id=workspace_id,
                tool_seq=tool_seq,
                tool_type=spec.tool_type,
                capability=spec.required_capability.value,
                status=STATUS_RECOGNIZED,
                approval_state=APPROVAL_NONE,
            )


# ---------------------------------------------------------------------------
# 3. 请求期校验（闸门④ / §4-Q4 第一行）
# ---------------------------------------------------------------------------


def build_unsupported_tool_error(spec: HostedToolSpec) -> CapabilityError:
    """按 §4-Q4 构造 400 ``unsupported_tool`` 错误。

    渲染结果（:meth:`CapabilityError.to_response`）是::

        400, {"error": {"type": "invalid_request_error",
                        "code": "unsupported_tool",
                        "param": "tools[2].type",
                        "message": "Tool type '...' is not supported ..."}}

    ``param`` 指向出问题的那个 tool，而不是笼统的 ``tools`` —— 并行请求 3 个
    工具时，客户端需要知道是**哪一个**没有路由。
    """
    return CapabilityError(
        error_class=ErrorClass.UNSUPPORTED_TOOL_CAPABILITY,
        message=UNSUPPORTED_TOOL_MESSAGE.format(
            tool_type=spec.tool_type,
            capability=spec.required_capability.value,
        ),
        param=spec.param_path,
    )


class HostedToolValidator:
    """请求校验阶段的 hosted tool 可服务性判定。"""

    def __init__(
        self,
        *,
        emulated: frozenset[Capability] = DEFAULT_EMULATED_CAPABILITIES,
        forwarded: frozenset[Capability] = UPSTREAM_FORWARDED_CAPABILITIES,
    ) -> None:
        #: 桥接自己能完整承载的能力。默认取 T25 的清单（只含真正实现了的两项）；
        #: 后续任务落地新执行器时从构造参数注入，不必改这里。
        self._emulated = emulated
        #: 上游透传能力（中继不执行、原样转发的 hosted 能力）。默认仅 web_search；
        #: 这些能力由上游承载，校验时视为可服务，不会 400。
        self._forwarded = forwarded

    def validate(
        self,
        specs: list[HostedToolSpec],
        *,
        available: frozenset[Capability],
    ) -> CapabilityError | None:
        """全部 hosted tool 都有能力承载时返回 ``None``，否则返回标准错误。

        取**第一个**不可服务的 tool 报错，而不是聚合成一条多值消息：官方错误体
        的 ``param`` 是单值字段，聚合会让它退化成 ``tools``，客户端就得自己
        猜。逐个报错、客户端逐个修，是可编程处理的失败。
        """
        servable = frozenset(available) | self._emulated | self._forwarded
        for spec in specs:
            if spec.required_capability not in servable:
                return build_unsupported_tool_error(spec)
        return None

    def unsupported(
        self,
        specs: list[HostedToolSpec],
        *,
        available: frozenset[Capability],
    ) -> list[HostedToolSpec]:
        """全部不可服务的 spec（审计 / 启动期缺口清单用，不用于错误体）。"""
        servable = frozenset(available) | self._emulated | self._forwarded
        return [s for s in specs if s.required_capability not in servable]


def validate_tool_choice(payload: Mapping[str, Any]) -> CapabilityError | None:
    """校验 ``payload["tool_choice"]``（R-P1-48）。

    合法形态：

    * 省略 / ``None`` —— 等价于 ``auto``；
    * ``"auto"`` / ``"none"`` / ``"required"``；
    * 具体工具名字符串（例如 ``"web_search"`` 或某个 function 名）；
    * ``{"type": "function", "function": {"name": "..."}}``；
    * ``{"type": "<hosted tool type>"}`` —— 点名某个 hosted tool。

    非法形态返回 :class:`CapabilityError`，错误分类是
    :attr:`ErrorClass.INVALID_TOOL_ARGUMENTS`（400 ``invalid_tool_arguments``）
    而**不是** ``unsupported_tool``：§4-Q4 的 ``unsupported_tool`` 说的是
    「这个工具没有执行器」，而这里是「``tool_choice`` 这个字段本身写错了」。
    把两者混成一个码，客户端会去改部署配置，而真正要改的是请求体。
    """
    if "tool_choice" not in payload:
        return None
    choice = payload.get("tool_choice")
    if choice is None:
        return None

    if isinstance(choice, str):
        # 字面量与具体工具名都是字符串；空串没有任何含义，判非法。
        return None if choice.strip() else _invalid_tool_choice("tool_choice must not be an empty string")

    if isinstance(choice, Mapping):
        kind = str(choice.get("type") or "")
        if not kind:
            return _invalid_tool_choice("tool_choice object requires a 'type' field")
        if kind not in _TOOL_CHOICE_OBJECT_TYPES:
            return _invalid_tool_choice("unknown tool_choice type '{0}'".format(kind))
        if kind == "function" and not _function_name(choice):
            return _invalid_tool_choice("tool_choice type 'function' requires function.name")
        return None

    return _invalid_tool_choice("tool_choice must be a string or an object, got {0}".format(type(choice).__name__))


def _function_name(choice: Mapping[str, Any]) -> str:
    """取 ``{"type":"function", ...}`` 里的函数名。

    官方历史上有两种写法：嵌套的 ``function.name`` 与扁平的 ``name``。两种都
    接受 —— 拒绝一种正确的官方写法就是新的兼容性缺陷。
    """
    nested = choice.get("function")
    if isinstance(nested, Mapping):
        name = str(nested.get("name") or "")
        if name:
            return name
    return str(choice.get("name") or "")


def _invalid_tool_choice(message: str) -> CapabilityError:
    return CapabilityError(
        error_class=ErrorClass.INVALID_TOOL_ARGUMENTS,
        message=message,
        param="tool_choice",
    )


# ---------------------------------------------------------------------------
# 4. 运行期收尾（§4-Q4 第二行）
# ---------------------------------------------------------------------------


def build_runtime_unavailable_event(
    spec: HostedToolSpec,
    *,
    strict: bool,
    response_id: str = "",
    message: str = "",
) -> dict[str, Any]:
    """构造「运行期才发现 hosted tool 不可执行」的终止事件。

    上游已经开始回流、甚至已经发过 delta 时才暴露的故障，不能再改成 HTTP 400
    —— 状态码早就发出去了。此时唯一诚实的做法是把流按终止语义收掉，并在
    ``incomplete_details`` 里说明原因（R-P1-22：兼容模式下它是**唯一**可诊断
    信号，缺失即 P0 缺陷）。

    ``strict=True``   -> ``response.incomplete``，``status="incomplete"``
    ``strict=False``  -> ``response.completed``，``status="completed"``

    两种模式的 ``terminal_reason`` 都是
    :attr:`TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE` —— 模式只改变外壳事件
    类型（兼容那些把 ``response.incomplete`` 当异常的旧 SDK），不改变事实。

    HONEST STUB：本函数只返回 dict。「安全关闭已开 item」「发 ``[DONE]``」
    「写事件日志」都由 T28 的 emitter 接线负责。
    """
    reason = TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE.value
    event_type = "response.incomplete" if strict else "response.completed"
    status = "incomplete" if strict else "completed"
    detail = message or UNSUPPORTED_TOOL_MESSAGE.format(
        tool_type=spec.tool_type,
        capability=spec.required_capability.value,
    )
    return {
        "type": event_type,
        "response": {
            "id": response_id,
            "status": status,
            "incomplete_details": to_incomplete_details(reason, detail),
            "terminal_reason": reason,
        },
    }


__all__ = [
    "HOSTED_TOOL_TYPES",
    "HOSTED_CAPABILITIES",
    "TOOL_CHOICE_LITERALS",
    "UNSUPPORTED_TOOL_MESSAGE",
    "HostedToolRecognizer",
    "HostedToolValidator",
    "build_unsupported_tool_error",
    "validate_tool_choice",
    "build_runtime_unavailable_event",
]
