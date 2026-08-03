"""Remote MCP client（T27 / R-P1-39、R-P1-46、R-P1-47、§3.3 #4）。

7 类 hosted tool 里，PRD §3.3 第 4 行只给了 MCP 一个 🟢 **完整实现** 的裁定，
理由写得很直白：MCP 是纯网络协议，不需要沙箱、浏览器或 GPU，是网关唯一能低成本
自持的能力。所以本模块**不是** HONEST STUB —— 它真的会把 ``tools/call`` 发到一个
MCP server 并把返回值变成 ``mcp_call`` item。

四类 item 与三个事件族（§10.3）
--------------------------------
=============================  ==================================================
item                            事件
=============================  ==================================================
``mcp_list_tools``              ``response.mcp_list_tools.in_progress``
                                ``.completed`` | ``.failed``
``mcp_call``                    ``response.mcp_call.in_progress``
                                ``.arguments.delta`` -> ``.arguments.done``
                                ``.completed`` | ``.failed``
``mcp_approval_request``        ``response.output_item.approval_request``
``mcp_approval_response``       （客户端输入 item，无事件；由
                                :meth:`McpClient.submit_approval` 消费）
=============================  ==================================================

判据③ 是本模块最容易失守的一条：**超时、传输故障、远端 ``isError``、审批被拒、
幂等冲突 —— 五种失败全部落在官方 ``response.mcp_call.failed`` 上**，没有任何一条
路径 return None 或吞掉异常。:meth:`McpClient.call_tool` 因此永远返回
:class:`McpOutcome`，不抛异常：调用方拿到的一定是一个可以直接写进 SSE 的事件序列。

可注入 transport（判据：测试不依赖真实网络）
--------------------------------------------
与 T25 的 :class:`~.passthrough.Transport` 同构：本模块定义 JSON-RPC 层的
:class:`McpTransport`（``request(method, params) -> dict``），
:class:`JsonRpcMcpSession` 在它之上跑完整的 MCP 握手（``initialize`` ->
``tools/list`` / ``tools/call``）。测试注入 :class:`InMemoryMcpServer`，生产注入
真实 SDK —— 两者对 :class:`McpClient` 完全等价。

``mcp>=1.2`` 是**可选依赖**，而不是前置条件
--------------------------------------------
MCP over HTTP 就是 JSON-RPC over POST，仓库的核心依赖 ``aiohttp`` 足以承载。
:class:`HttpMcpTransport` 因此是**不带任何可选依赖的真实网络实现** —— 配了
``server_url`` 就能真的连到远端 MCP server。``mcp`` 包只在需要 stdio 传输时才
用得上，由 :func:`load_mcp_sdk` 延迟导入，缺失时抛
:class:`McpDependencyError`（带指引），**不是**裸 ``ImportError`` —— 后者会在栈
顶被当成「模块坏了」而不是「没装可选依赖」。因此不装 ``mcp`` 的环境里：

* 全量测试照常通过（测试注入内存 server / 假 poster，一次网络都不发）；
* ``server_url`` 形态的真实调用完全可用；
* 只有 stdio 形态给出一句人能读懂的降级说明，而不是 500。

偿还 T26 留下的三处 STUB
------------------------
1. ``mcp_approval_request`` / ``mcp_approval_response`` item 与事件族
   —— :meth:`McpClient.request_approval` / :meth:`McpClient.submit_approval`。
2. ``approval_state`` 从 ``none`` 推到 ``pending`` 并真正驱动往返
   —— :meth:`McpClient.request_approval` 写 :data:`APPROVAL_PENDING`。
3. :meth:`IdempotencyStore.reserve` 的轮询 / 超时 / conflict 判定
   —— :meth:`McpClient._acquire`（占位失败后轮询等待原执行者，超时与同键异体
   各自映射到独立的 failed 错误码）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..proxy.protocol.responses_models import ItemType, canonical_json
from ..store.idempotency import (
    DEFAULT_TTL_SECONDS,
    STATE_DONE,
    IdempotencyStore,
)
from ..store.tool_executions import (
    APPROVAL_APPROVED,
    APPROVAL_NONE,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ToolExecutionStore,
    execution_id_for,
)

# ---------------------------------------------------------------------------
# 1. 事件族与常量（§10.3）
# ---------------------------------------------------------------------------

EVENT_LIST_TOOLS_IN_PROGRESS: str = "response.mcp_list_tools.in_progress"
EVENT_LIST_TOOLS_COMPLETED: str = "response.mcp_list_tools.completed"
EVENT_LIST_TOOLS_FAILED: str = "response.mcp_list_tools.failed"

EVENT_CALL_IN_PROGRESS: str = "response.mcp_call.in_progress"
EVENT_CALL_ARGUMENTS_DELTA: str = "response.mcp_call.arguments.delta"
EVENT_CALL_ARGUMENTS_DONE: str = "response.mcp_call.arguments.done"
EVENT_CALL_COMPLETED: str = "response.mcp_call.completed"
EVENT_CALL_FAILED: str = "response.mcp_call.failed"

#: 审批请求走 ``response.output_item.approval_request``（§10.3 最后一行）。
EVENT_APPROVAL_REQUEST: str = "response.output_item.approval_request"

#: §10.3 为 MCP 列出的全部事件类型。测试用它断言「本模块不会发明事件名」。
MCP_EVENT_TYPES: frozenset[str] = frozenset({
    EVENT_LIST_TOOLS_IN_PROGRESS,
    EVENT_LIST_TOOLS_COMPLETED,
    EVENT_LIST_TOOLS_FAILED,
    EVENT_CALL_IN_PROGRESS,
    EVENT_CALL_ARGUMENTS_DELTA,
    EVENT_CALL_ARGUMENTS_DONE,
    EVENT_CALL_COMPLETED,
    EVENT_CALL_FAILED,
    EVENT_APPROVAL_REQUEST,
})

#: ``mcp_call`` 失败时写进 ``item["error"]["code"]`` 的取值。每种失败一个独立
#: 的码 —— 全部塞成 ``mcp_error`` 会让客户端无法区分「重试有用」（超时、传输）
#: 和「重试没用」（被拒、不在白名单、同键异体）。
ERROR_TIMEOUT: str = "mcp_call_timeout"
ERROR_TRANSPORT: str = "mcp_transport_error"
ERROR_TOOL_FAILED: str = "mcp_tool_error"
ERROR_APPROVAL_REJECTED: str = "mcp_approval_rejected"
ERROR_TOOL_NOT_ALLOWED: str = "mcp_tool_not_allowed"
ERROR_IDEMPOTENCY_CONFLICT: str = "idempotency_conflict"
ERROR_IDEMPOTENCY_WAIT_TIMEOUT: str = "idempotency_wait_timeout"
ERROR_DEPENDENCY_MISSING: str = "mcp_dependency_missing"

#: 全部会出现在 ``mcp_call.failed`` 里的错误码。
MCP_ERROR_CODES: frozenset[str] = frozenset({
    ERROR_TIMEOUT, ERROR_TRANSPORT, ERROR_TOOL_FAILED,
    ERROR_APPROVAL_REJECTED, ERROR_TOOL_NOT_ALLOWED,
    ERROR_IDEMPOTENCY_CONFLICT, ERROR_IDEMPOTENCY_WAIT_TIMEOUT,
    ERROR_DEPENDENCY_MISSING,
})

#: 写进 ``tool_executions.status`` 的执行状态（审计轨迹，判据②）。
STATUS_AWAITING_APPROVAL: str = "awaiting_approval"
STATUS_IN_PROGRESS: str = "in_progress"
STATUS_COMPLETED: str = "completed"
STATUS_FAILED: str = "failed"
STATUS_REJECTED: str = "rejected"
STATUS_DUPLICATE: str = "duplicate"

#: :attr:`McpOutcome.kind` 的取值。
OUTCOME_COMPLETED: str = "completed"
OUTCOME_FAILED: str = "failed"
OUTCOME_PENDING_APPROVAL: str = "pending_approval"
OUTCOME_DUPLICATE: str = "duplicate"

#: hosted tool 的 ``type`` 与能力名，落库用。
MCP_TOOL_TYPE: str = "mcp"
MCP_CAPABILITY: str = "remote_mcp"

#: 单次 ``tools/call`` 的墙钟上限。MCP server 多半是用户自己部署的，慢是常态，
#: 但「永远不返回」不能变成网关的挂起 —— 超时后走 failed 事件（判据③）。
DEFAULT_TIMEOUT_SECONDS: float = 30.0

#: 幂等占位被别人拿走时，最多等多久原执行者出结果。
DEFAULT_IDEMPOTENCY_WAIT_SECONDS: float = 5.0
DEFAULT_IDEMPOTENCY_POLL_SECONDS: float = 0.05

#: **占位租约**的 TTL，远短于成功记录的 TTL。
#:
#: 失败的执行不能把幂等键锁死 24 小时：那会让一次网络抖动变成一整天都不能重试。
#: 但也不能在失败时把记录删掉 —— 删除等于放开并发闸门。短租约是两者之间唯一正确
#: 的解：成功时 :meth:`IdempotencyStore.mark_executed` 用完整 TTL 覆盖它，失败时
#: 让它自己过期。
DEFAULT_RESERVATION_TTL_SECONDS: int = 300

#: MCP 协议版本（``initialize`` 握手用）。
MCP_PROTOCOL_VERSION: str = "2024-11-05"

#: 无法建立会话时给出的指引。刻意同时说清两条出路 —— 只说「装 mcp」会让用户
#: 以为可选依赖是必需的，而 HTTP 传输本来就不需要它。
DEPENDENCY_HINT: str = (
    "set 'server_url' on the mcp tool to use the built-in Streamable HTTP "
    "transport (no extra dependency), or install the optional stdio SDK with: "
    "pip install 'zhongzhuan[mcp]'  (equivalently: pip install 'mcp>=1.2')"
)


# ---------------------------------------------------------------------------
# 2. 异常
# ---------------------------------------------------------------------------


class McpError(RuntimeError):
    """MCP 侧的所有失败都带一个 :attr:`code`，直接进 failed 事件的 error 体。"""

    code: str = ERROR_TRANSPORT

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        if code:
            self.code = code


class McpDependencyError(McpError):
    """``mcp>=1.2`` 未安装。

    刻意**不**让裸 ``ImportError`` 逃出去：调用栈上层看到 ImportError 会当成
    「代码坏了」去查 bug，而实际情况是「可选依赖没装」，两者的处置完全不同。
    """

    code = ERROR_DEPENDENCY_MISSING


class McpTransportError(McpError):
    """连接 / JSON-RPC 层失败。"""

    code = ERROR_TRANSPORT


class McpToolError(McpError):
    """远端把工具执行判为失败（JSON-RPC error 或 ``isError: true``）。"""

    code = ERROR_TOOL_FAILED


# ---------------------------------------------------------------------------
# 3. 可注入的 transport / session
# ---------------------------------------------------------------------------


@runtime_checkable
class McpTransport(Protocol):
    """JSON-RPC 层传输：一次 ``request`` 对应 MCP 的一个方法调用。"""

    async def request(
        self, method: str, params: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class McpSession(Protocol):
    """:class:`McpClient` 唯一依赖的会话接口。

    真实 SDK（:class:`SdkMcpSession`）与内存 fake（:class:`JsonRpcMcpSession`
    + :class:`InMemoryMcpServer`）都实现它，所以判据①②③的测试可以在没有网络、
    也没有 ``mcp`` 包的环境里跑完整条链路。
    """

    async def list_tools(self) -> Sequence[Mapping[str, Any]]: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class JsonRpcMcpSession:
    """在 :class:`McpTransport` 之上跑 MCP 握手与两个方法。

    ``initialize`` 只做一次并缓存 —— MCP 规定它是连接级握手，每次
    ``tools/call`` 都重握手既慢又会被规范良好的 server 拒绝。
    """

    def __init__(
        self,
        transport: McpTransport,
        *,
        protocol_version: str = MCP_PROTOCOL_VERSION,
        client_name: str = "zhongzhuan",
    ) -> None:
        self._transport = transport
        self._protocol_version = protocol_version
        self._client_name = client_name
        self._server_info: dict[str, Any] | None = None

    @property
    def server_info(self) -> dict[str, Any]:
        """``initialize`` 的返回；未握手时是空 dict。"""
        return dict(self._server_info or {})

    async def initialize(self) -> dict[str, Any]:
        if self._server_info is None:
            result = await self._request("initialize", {
                "protocolVersion": self._protocol_version,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": self._client_name, "version": "3"},
            })
            self._server_info = dict(result)
        return dict(self._server_info)

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, (list, tuple)):
            raise McpTransportError("tools/list returned no 'tools' array")
        return [_normalize_tool(tool) for tool in tools]

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self.initialize()
        result = await self._request("tools/call", {
            "name": name, "arguments": dict(arguments),
        })
        return _normalize_call_result(result)

    async def _request(
        self, method: str, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = await self._transport.request(method, params)
        if not isinstance(raw, Mapping):
            raise McpTransportError(
                "transport returned {0}, expected a mapping".format(
                    type(raw).__name__
                )
            )
        error = raw.get("error")
        if isinstance(error, Mapping):
            raise McpToolError(
                "MCP server returned error for {0}: {1}".format(
                    method, error.get("message") or error,
                )
            )
        result = raw.get("result", raw)
        return dict(result) if isinstance(result, Mapping) else {}


class SdkMcpSession:
    """把 ``mcp>=1.2`` 的 ``ClientSession`` 适配成 :class:`McpSession`。

    只依赖鸭子类型（``list_tools()`` / ``call_tool(name, arguments)``），因此
    **不装 ``mcp`` 包也能被单测覆盖** —— 测试传一个形状相同的假对象即可，归一化
    逻辑（SDK 的 pydantic 模型 -> 官方 item 里的 dict）照样被真实断言。
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> list[dict[str, Any]]:
        raw = await self._session.list_tools()
        tools = getattr(raw, "tools", raw)
        return [_normalize_tool(tool) for tool in (tools or ())]

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = await self._session.call_tool(name, dict(arguments))
        return _normalize_call_result(raw)


class HttpMcpTransport:
    """MCP **Streamable HTTP** 传输 —— 真实网络实现，不需要 ``mcp`` 包。

    MCP over HTTP 就是「POST 一个 JSON-RPC 请求，收一个 JSON-RPC 响应」，规范
    允许响应体是 ``application/json``，也允许是一段 ``text/event-stream``（服务
    端要推进度时用）。两种都在这里解析。``initialize`` 返回的 ``Mcp-Session-Id``
    被记住并回带 —— 不回带的话，规范良好的 server 会把后续请求判为新连接而拒绝。

    HTTP 那一层本身也是可注入的（``poster``）：默认实现用仓库的核心依赖
    ``aiohttp``，测试注入一个假 poster 就能**逐字节断言发出去的 JSON-RPC 报文**，
    等价于 T25 判据②的「抓包」。所以「真实实现」与「测试不碰网络」不冲突。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        poster: Callable[..., Awaitable[tuple[int, Mapping[str, str], bytes]]]
        | None = None,
        protocol_version: str = MCP_PROTOCOL_VERSION,
    ) -> None:
        if not url:
            raise ValueError("HttpMcpTransport requires a non-empty url")
        self._url = url
        self._extra_headers = dict(headers or {})
        self._poster = poster or _aiohttp_post
        self._protocol_version = protocol_version
        self._session_id = ""
        self._next_id = 0

    @property
    def session_id(self) -> str:
        """``initialize`` 之后由服务端分配的 ``Mcp-Session-Id``。"""
        return self._session_id

    async def request(
        self, method: str, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._next_id += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": dict(params),
        }, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            # 规范要求同时声明两种：服务端据此决定回 JSON 还是 SSE。
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self._extra_headers)

        status, resp_headers, payload = await self._poster(
            self._url, headers, body,
        )
        lowered = {str(k).lower(): str(v) for k, v in dict(resp_headers).items()}
        session_id = lowered.get("mcp-session-id", "")
        if session_id:
            self._session_id = session_id
        if status < 200 or status >= 300:
            raise McpTransportError(
                "MCP server returned HTTP {0} for {1}".format(status, method)
            )
        content_type = lowered.get("content-type", "")
        if "text/event-stream" in content_type:
            return _parse_sse_jsonrpc(payload)
        try:
            decoded = json.loads(payload.decode("utf-8") or "{}")
        except ValueError as exc:
            raise McpTransportError(
                "MCP server returned a non-JSON body for " + method
            ) from exc
        if not isinstance(decoded, Mapping):
            raise McpTransportError(
                "MCP server returned a JSON {0}, expected an object".format(
                    type(decoded).__name__
                )
            )
        return dict(decoded)


async def _aiohttp_post(
    url: str, headers: Mapping[str, str], body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    """默认 HTTP 后端。

    每次请求开一个 ``ClientSession``：正确但不省连接。带连接池、超时六件套与
    取消传播的共享客户端是 T28 pipeline 的职责，届时注入一个 ``poster`` 即可，
    不必改本模块。
    """
    try:  # pragma: no cover - aiohttp 是核心依赖，缺失属于安装损坏
        import aiohttp
    except ImportError as exc:  # pragma: no cover
        raise McpTransportError("aiohttp is required for HttpMcpTransport") from exc
    async with aiohttp.ClientSession() as session:  # pragma: no cover - 需网络
        async with session.post(url, headers=dict(headers), data=body) as resp:
            payload = await resp.read()
            return resp.status, dict(resp.headers), payload


def _parse_sse_jsonrpc(payload: bytes) -> dict[str, Any]:
    """从 ``text/event-stream`` 响应体里取出那一条 JSON-RPC 响应。"""
    for line in payload.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            decoded = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(decoded, Mapping) and (
            "result" in decoded or "error" in decoded
        ):
            return dict(decoded)
    raise McpTransportError("no JSON-RPC payload found in SSE response")


def load_mcp_sdk() -> Any:
    """延迟导入 ``mcp>=1.2``；未安装时抛 :class:`McpDependencyError`。

    模块导入期就 ``import mcp`` 会让整个 ``responses_v3`` 包在没装可选依赖的
    环境里直接崩掉，而 MCP 只是 7 类 hosted tool 之一。
    """
    try:
        import mcp  # noqa: PLC0415
    except ImportError as exc:
        raise McpDependencyError(DEPENDENCY_HINT) from exc
    return mcp


def connect(
    config: "McpServerConfig",
    *,
    transport: McpTransport | None = None,
    poster: Callable[..., Awaitable[tuple[int, Mapping[str, str], bytes]]]
    | None = None,
) -> McpSession:
    """按配置建立会话。

    1. 注入了 ``transport`` -> 直接用它（测试与自定义传输）；
    2. 配了 ``server_url`` -> :class:`HttpMcpTransport`，**真实网络调用**；
    3. 两者都没有 -> :class:`McpDependencyError`，消息里写清楚要么配
       ``server_url``、要么装 ``mcp`` 走 stdio。

    第 3 条特意不返回一个「什么都不做的会话」：静默降级成空实现正是 R-P1-45
    禁止的「运行时假装成功」。
    """
    if transport is not None:
        return JsonRpcMcpSession(transport)
    if config.server_url:
        return JsonRpcMcpSession(
            HttpMcpTransport(
                config.server_url, headers=config.headers, poster=poster,
            )
        )
    raise McpDependencyError(
        "mcp server '{0}' has no server_url; {1}".format(
            config.server_label, DEPENDENCY_HINT,
        )
    )


# ---------------------------------------------------------------------------
# 4. 内存 MCP server（测试与本地演练用，等价于 T25 的 RecordingTransport）
# ---------------------------------------------------------------------------


@dataclass
class InMemoryMcpServer:
    """纯内存 MCP server：实现 ``initialize`` / ``tools/list`` / ``tools/call``。

    与 :class:`~.passthrough.RecordingTransport` 同一个定位 —— 放在源码里而不是
    测试里，是因为「怎样才算一个合法的 MCP 对端」属于本模块的契约，测试只是它的
    第一个消费者。

    :attr:`calls` 记录每一次 ``request``，判据「同一幂等键不二次执行」直接数它。
    """

    tools: list[dict[str, Any]] = field(default_factory=list)
    handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = field(
        default_factory=dict
    )
    #: 每次 ``request`` 前 sleep 的秒数（驱动超时用例）。
    delay_seconds: float = 0.0
    #: 非空时 ``request`` 直接抛 :class:`McpTransportError`（驱动传输故障用例）。
    transport_error: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    server_name: str = "fake-mcp"

    async def request(
        self, method: str, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.transport_error:
            raise McpTransportError(self.transport_error)
        if method == "initialize":
            return {"result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": "1.0"},
            }}
        if method == "tools/list":
            return {"result": {"tools": [dict(t) for t in self.tools]}}
        if method == "tools/call":
            return {"result": await self._invoke(params)}
        return {"error": {"code": -32601, "message": "unknown method " + method}}

    async def _invoke(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        handler = self.handlers.get(name)
        if handler is None:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "unknown tool: " + name}],
            }
        try:
            outcome = handler(params.get("arguments") or {})
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001 - 真实 server 也会把它变成 isError
            return {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}],
            }
        if isinstance(outcome, Mapping) and "content" in outcome:
            return dict(outcome)
        return {
            "isError": False,
            "content": [{"type": "text", "text": _as_text(outcome)}],
        }

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """该方法被调用的全部参数（``len()`` 即调用次数）。"""
        return [params for name, params in self.calls if name == method]


# ---------------------------------------------------------------------------
# 5. 服务器配置（``tools[i]`` 里那个 ``{"type": "mcp", ...}``）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """一个 MCP server 的接入配置。

    ``require_approval`` 默认 ``"always"`` —— 与 OpenAI 官方默认一致，也是唯一
    安全的默认：远端工具有副作用，「没配就不用批」等于把审批做成可选项。
    """

    server_label: str
    server_url: str = ""
    require_approval: Any = "always"
    allowed_tools: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_tool(cls, raw: Mapping[str, Any]) -> "McpServerConfig":
        """从请求体里的 hosted tool 条目构造。

        缺 ``server_label`` 时抛 :class:`ValueError`：审计与事件里的
        ``server_label`` 是客户端区分多个 MCP server 的唯一标识，缺了它整条
        执行记录就无法归属。
        """
        label = str(raw.get("server_label") or "").strip()
        if not label:
            raise ValueError("mcp tool requires a non-empty 'server_label'")
        allowed = raw.get("allowed_tools")
        names: tuple[str, ...] = ()
        if isinstance(allowed, (list, tuple)):
            names = tuple(str(n) for n in allowed if str(n))
        headers = raw.get("headers")
        return cls(
            server_label=label,
            server_url=str(raw.get("server_url") or ""),
            require_approval=raw.get("require_approval", "always"),
            allowed_tools=names,
            headers=dict(headers) if isinstance(headers, Mapping) else {},
        )

    def tool_allowed(self, tool_name: str) -> bool:
        """``allowed_tools`` 为空表示不限制；非空则是**白名单**。"""
        return not self.allowed_tools or tool_name in self.allowed_tools

    def approval_required(self, tool_name: str) -> bool:
        """解析官方的三种 ``require_approval`` 形态。

        * ``"never"`` -> 全部免批；
        * ``"always"`` / 缺省 / 无法识别 -> 全部要批（**未知取值按最严处理**：
          把一个拼错的策略字符串解释成「免批」会让副作用悄悄执行）；
        * ``{"never": {"tool_names": [...]}, "always": {...}}`` -> 按名单：
          ``never`` 优先（显式点名免批的工具就是免批）；名单外的兜底取值分两种
          情况——只给了 ``always`` 名单时，说明调用方是在**枚举需要审批的工具**，
          名单外免批；否则（只给 ``never`` 名单、或名单都空）一律要批。
          兜底方向必须偏严：把没点名的工具解释成免批，等于让副作用悄悄执行。
        """
        policy = self.require_approval
        if isinstance(policy, str):
            return policy.strip().lower() != "never"
        if isinstance(policy, Mapping):
            if tool_name in _policy_names(policy.get("never")):
                return False
            always = _policy_names(policy.get("always"))
            if always:
                return tool_name in always
            return True
        return True


def _policy_names(section: Any) -> frozenset[str]:
    if not isinstance(section, Mapping):
        return frozenset()
    names = section.get("tool_names")
    if not isinstance(names, (list, tuple)):
        return frozenset()
    return frozenset(str(n) for n in names)


# ---------------------------------------------------------------------------
# 6. item / 事件构造器
# ---------------------------------------------------------------------------


def make_list_tools_item_id(response_id: str, output_index: int) -> str:
    return "mcpl_{0}_{1}".format(response_id, output_index)


def make_call_item_id(response_id: str, tool_seq: int) -> str:
    return "mcp_{0}_{1}".format(response_id, int(tool_seq))


def make_approval_request_id(response_id: str, tool_seq: int) -> str:
    return "mcpr_{0}_{1}".format(response_id, int(tool_seq))


def parse_approval_request_id(request_id: str) -> tuple[str, int]:
    """``mcpr_{response_id}_{tool_seq}`` -> ``(response_id, tool_seq)``。

    ``response_id`` 自身可能含下划线，所以只从右边切一刀。切不出整数 seq 时抛
    :class:`ValueError` —— 一个认不出的 ``approval_request_id`` 绝不能被静默当作
    ``tool_seq=0``，那会把审批批到别人头上。
    """
    if not request_id.startswith("mcpr_"):
        raise ValueError("not an approval_request_id: {0!r}".format(request_id))
    body = request_id[len("mcpr_"):]
    head, _, tail = body.rpartition("_")
    if not head or not tail.lstrip("-").isdigit():
        raise ValueError("malformed approval_request_id: {0!r}".format(request_id))
    return head, int(tail)


def build_list_tools_item(
    item_id: str, server_label: str, tools: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": ItemType.MCP_LIST_TOOLS.value,
        "status": "completed",
        "server_label": server_label,
        "tools": [dict(t) for t in tools],
    }


def build_call_item(
    item_id: str,
    server_label: str,
    name: str,
    arguments: Mapping[str, Any],
    *,
    status: str = "completed",
    output: Any = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """官方 ``mcp_call`` item。``error`` 非空时 ``output`` 恒为 ``None``。"""
    return {
        "id": item_id,
        "type": ItemType.MCP_CALL.value,
        "status": status,
        "server_label": server_label,
        "name": name,
        "arguments": canonical_json(dict(arguments)),
        "output": None if error else output,
        "error": dict(error) if error else None,
    }


def build_approval_request_item(
    request_id: str,
    server_label: str,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": request_id,
        "type": ItemType.MCP_APPROVAL_REQUEST.value,
        "status": "in_progress",
        "server_label": server_label,
        "name": name,
        "arguments": canonical_json(dict(arguments)),
    }


def build_approval_response_item(
    request_id: str, *, approve: bool, reason: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": ItemType.MCP_APPROVAL_RESPONSE.value,
        "approval_request_id": request_id,
        "approve": bool(approve),
    }
    if reason:
        item["reason"] = reason
    return item


def build_error(code: str, message: str) -> dict[str, Any]:
    return {"type": "mcp_error", "code": code, "message": message}


# ---------------------------------------------------------------------------
# 7. 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpOutcome:
    """一次 MCP 操作的完整产物：item + 该发的事件序列。

    :meth:`McpClient.call_tool` **不抛异常**，一律返回本对象 —— 判据③要求所有
    失败都变成官方事件，而不是让调用方去 try/except 里自己拼错误体。
    """

    kind: str
    item: dict[str, Any]
    events: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None
    replayed_from: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == OUTCOME_COMPLETED

    @property
    def event_types(self) -> tuple[str, ...]:
        return tuple(str(e.get("type") or "") for e in self.events)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """:meth:`McpClient.submit_approval` 的结果（往返的后半程）。"""

    response_id: str
    tool_seq: int
    approved: bool
    item: dict[str, Any]
    reason: str = ""


# ---------------------------------------------------------------------------
# 8. 幂等租约
# ---------------------------------------------------------------------------

LEASE_PROCEED: str = "proceed"
LEASE_DUPLICATE: str = "duplicate"
LEASE_CONFLICT: str = "conflict"
LEASE_WAIT_TIMEOUT: str = "wait_timeout"


@dataclass(frozen=True, slots=True)
class _Lease:
    state: str
    holder: str = ""


# ---------------------------------------------------------------------------
# 9. 客户端
# ---------------------------------------------------------------------------


class McpClient:
    """Remote MCP 执行器：真的把请求发出去，并把结果变成官方 item 与事件。"""

    def __init__(
        self,
        *,
        executions: ToolExecutionStore | None = None,
        idempotency: IdempotencyStore | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        idempotency_wait_seconds: float = DEFAULT_IDEMPOTENCY_WAIT_SECONDS,
        idempotency_poll_seconds: float = DEFAULT_IDEMPOTENCY_POLL_SECONDS,
        reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
        result_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        arguments_chunk_size: int = 0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: 审计与审批状态的落库位置（判据②）。``None`` 时只发事件不落库 ——
        #: 供纯协议层单测使用，生产链路必须注入。
        self._executions = executions
        self._idempotency = idempotency
        self._timeout = float(timeout_seconds)
        self._wait_seconds = float(idempotency_wait_seconds)
        self._poll_seconds = max(float(idempotency_poll_seconds), 0.0)
        self._reservation_ttl = int(reservation_ttl_seconds)
        self._result_ttl = int(result_ttl_seconds)
        self._chunk = int(arguments_chunk_size)
        self._sleep = sleep
        self._clock = clock

    # -- mcp_list_tools ---------------------------------------------------

    async def list_tools(
        self,
        session: McpSession,
        config: McpServerConfig,
        *,
        response_id: str,
        workspace_id: str = "",
        output_index: int = 0,
        tool_seq: int = -1,
    ) -> McpOutcome:
        """拉取远端工具清单，产出 ``mcp_list_tools`` item 与事件族。

        ``allowed_tools`` 非空时在这里就把清单裁掉：让模型看见一个它无权调用的
        工具，只会换来一次必然失败的 ``tools/call``。
        """
        item_id = make_list_tools_item_id(response_id, output_index)
        base = {"output_index": output_index, "item_id": item_id,
                "server_label": config.server_label}
        events: list[dict[str, Any]] = [
            dict(base, type=EVENT_LIST_TOOLS_IN_PROGRESS),
        ]
        try:
            tools = await asyncio.wait_for(
                session.list_tools(), timeout=self._timeout,
            )
        except TimeoutError:
            return await self._list_tools_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                ERROR_TIMEOUT,
                "mcp_list_tools timed out after {0}s".format(self._timeout),
            )
        except McpError as exc:
            return await self._list_tools_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                exc.code, str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 判据③：不静默丢弃
            return await self._list_tools_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                ERROR_TRANSPORT, "{0}: {1}".format(type(exc).__name__, exc),
            )

        visible = [t for t in tools if config.tool_allowed(str(t.get("name") or ""))]
        item = build_list_tools_item(item_id, config.server_label, visible)
        events.append(dict(base, type=EVENT_LIST_TOOLS_COMPLETED, item=item))
        await self._audit(
            response_id, workspace_id, tool_seq,
            status=STATUS_COMPLETED, approval_state=APPROVAL_NONE,
        )
        return McpOutcome(kind=OUTCOME_COMPLETED, item=item, events=tuple(events))

    async def _list_tools_failed(
        self,
        base: Mapping[str, Any],
        events: list[dict[str, Any]],
        config: McpServerConfig,
        response_id: str,
        workspace_id: str,
        tool_seq: int,
        code: str,
        message: str,
    ) -> McpOutcome:
        error = build_error(code, message)
        item = {
            "id": base["item_id"],
            "type": ItemType.MCP_LIST_TOOLS.value,
            "status": "incomplete",
            "server_label": config.server_label,
            "tools": [],
            "error": error,
        }
        events.append(dict(base, type=EVENT_LIST_TOOLS_FAILED,
                           item=item, error=error))
        await self._audit(
            response_id, workspace_id, tool_seq,
            status=STATUS_FAILED, approval_state=APPROVAL_NONE,
        )
        return McpOutcome(
            kind=OUTCOME_FAILED, item=item, events=tuple(events), error=error,
        )

    # -- 审批往返（偿还 T26 STUB #1 / #2）---------------------------------

    async def request_approval(
        self,
        config: McpServerConfig,
        *,
        response_id: str,
        tool_seq: int,
        name: str,
        arguments: Mapping[str, Any],
        workspace_id: str = "",
        output_index: int = 0,
    ) -> McpOutcome:
        """产出 ``mcp_approval_request`` item 并把审批状态推到 ``pending``。

        T26 的 :meth:`HostedToolRecognizer.persist` 恒写 ``approval_state=none``
        并在文档里点名「翻成 pending 是 T27」。这里就是那一步：状态落库之后，
        :meth:`ToolExecutionStore.get_pending_approvals` 才能在租户内查到它，
        审批往返才有一个跨进程可见的锚点。
        """
        request_id = make_approval_request_id(response_id, tool_seq)
        item = build_approval_request_item(
            request_id, config.server_label, name, arguments,
        )
        await self._audit(
            response_id, workspace_id, tool_seq,
            status=STATUS_AWAITING_APPROVAL, approval_state=APPROVAL_PENDING,
        )
        event = {
            "type": EVENT_APPROVAL_REQUEST,
            "output_index": output_index,
            "item_id": request_id,
            "server_label": config.server_label,
            "item": item,
        }
        return McpOutcome(
            kind=OUTCOME_PENDING_APPROVAL, item=item, events=(event,),
        )

    async def submit_approval(
        self,
        item: Mapping[str, Any],
        *,
        workspace_id: str = "",
    ) -> ApprovalDecision:
        """消费客户端回传的 ``mcp_approval_response`` input item。

        ``approve`` 缺省按 **拒绝** 处理：一个没写 ``approve`` 的审批回执是畸形
        输入，把它当成「同意」就等于让畸形请求触发副作用。
        """
        request_id = str(item.get("approval_request_id") or "")
        response_id, tool_seq = parse_approval_request_id(request_id)
        approved = bool(item.get("approve"))
        reason = str(item.get("reason") or "")
        decision = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
        if self._executions is not None:
            await self._executions.set_approval(response_id, tool_seq, decision)
        return ApprovalDecision(
            response_id=response_id,
            tool_seq=tool_seq,
            approved=approved,
            item=build_approval_response_item(
                request_id, approve=approved, reason=reason,
            ),
            reason=reason,
        )

    # -- mcp_call ---------------------------------------------------------

    async def call_tool(
        self,
        session: McpSession,
        config: McpServerConfig,
        *,
        response_id: str,
        tool_seq: int,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        workspace_id: str = "",
        idempotency_key: str = "",
        approved: bool | None = None,
        output_index: int = 0,
    ) -> McpOutcome:
        """执行一次远端工具调用。

        ``approved``：``None`` 表示「还没问过」—— 若该工具需要审批则返回
        :data:`OUTCOME_PENDING_APPROVAL`，调用方发出 ``mcp_approval_request``
        后等客户端回执，再带 ``approved=True`` 重新调本方法。

        五种失败（不在白名单 / 审批被拒 / 幂等冲突 / 幂等等待超时 / 执行超时或
        传输故障）全部走 ``response.mcp_call.failed``，各带独立错误码。
        """
        args = dict(arguments or {})
        item_id = make_call_item_id(response_id, tool_seq)
        base = {"output_index": output_index, "item_id": item_id,
                "server_label": config.server_label, "name": name}

        if not config.tool_allowed(name):
            return await self._call_failed(
                base, [], config, response_id, workspace_id, tool_seq, name, args,
                ERROR_TOOL_NOT_ALLOWED,
                "tool '{0}' is not in allowed_tools of server '{1}'".format(
                    name, config.server_label,
                ),
                approval_state=APPROVAL_NONE, status=STATUS_FAILED,
            )

        needs_approval = config.approval_required(name)
        if needs_approval and approved is None:
            return await self.request_approval(
                config,
                response_id=response_id, tool_seq=tool_seq, name=name,
                arguments=args, workspace_id=workspace_id,
                output_index=output_index,
            )
        if needs_approval and not approved:
            return await self._call_failed(
                base, [], config, response_id, workspace_id, tool_seq, name, args,
                ERROR_APPROVAL_REJECTED,
                "approval was rejected for tool '{0}'".format(name),
                approval_state=APPROVAL_REJECTED, status=STATUS_REJECTED,
            )
        approval_state = APPROVAL_APPROVED if needs_approval else APPROVAL_NONE

        digest = request_digest(config.server_label, name, args)
        lease = await self._acquire(
            idempotency_key,
            workspace_id=workspace_id, digest=digest,
            response_id=response_id, tool_seq=tool_seq,
        )
        if lease.state == LEASE_CONFLICT:
            return await self._call_failed(
                base, [], config, response_id, workspace_id, tool_seq, name, args,
                ERROR_IDEMPOTENCY_CONFLICT,
                "idempotency key '{0}' was already used with a different "
                "request body".format(idempotency_key),
                approval_state=approval_state, status=STATUS_FAILED,
            )
        if lease.state == LEASE_WAIT_TIMEOUT:
            return await self._call_failed(
                base, [], config, response_id, workspace_id, tool_seq, name, args,
                ERROR_IDEMPOTENCY_WAIT_TIMEOUT,
                "another execution still holds idempotency key '{0}' after "
                "{1}s".format(idempotency_key, self._wait_seconds),
                approval_state=approval_state, status=STATUS_FAILED,
            )

        events: list[dict[str, Any]] = [dict(base, type=EVENT_CALL_IN_PROGRESS)]
        events += self._argument_events(base, args)

        if lease.state == LEASE_DUPLICATE:
            # 判据「同一幂等键重复请求不二次执行」：**一次 tools/call 都不发**。
            # 工具输出本身不在本层重放 —— 它属于原 response，调用方凭
            # ``replayed_from`` 去 ResponseStore 取整条（输出体不在幂等表里，
            # 那张表只存 (response_id, status_code, state)）。
            item = build_call_item(
                item_id, config.server_label, name, args,
                status="completed", output=None,
            )
            item["duplicate_of"] = lease.holder
            events.append(dict(base, type=EVENT_CALL_COMPLETED, item=item))
            await self._audit(
                response_id, workspace_id, tool_seq,
                status=STATUS_DUPLICATE, approval_state=approval_state,
                idempotency_key=idempotency_key,
            )
            return McpOutcome(
                kind=OUTCOME_DUPLICATE, item=item, events=tuple(events),
                replayed_from=lease.holder,
            )

        await self._audit(
            response_id, workspace_id, tool_seq,
            status=STATUS_IN_PROGRESS, approval_state=approval_state,
            idempotency_key=idempotency_key,
        )

        try:
            result = await asyncio.wait_for(
                session.call_tool(name, args), timeout=self._timeout,
            )
        except TimeoutError:
            return await self._call_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                name, args, ERROR_TIMEOUT,
                "mcp_call '{0}' timed out after {1}s".format(name, self._timeout),
                approval_state=approval_state, status=STATUS_FAILED,
            )
        except McpError as exc:
            return await self._call_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                name, args, exc.code, str(exc),
                approval_state=approval_state, status=STATUS_FAILED,
            )
        except Exception as exc:  # noqa: BLE001 - 判据③：不静默丢弃
            return await self._call_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                name, args, ERROR_TRANSPORT,
                "{0}: {1}".format(type(exc).__name__, exc),
                approval_state=approval_state, status=STATUS_FAILED,
            )

        if result.get("isError"):
            return await self._call_failed(
                base, events, config, response_id, workspace_id, tool_seq,
                name, args, ERROR_TOOL_FAILED,
                _content_text(result.get("content")) or "remote tool reported "
                "an error",
                approval_state=approval_state, status=STATUS_FAILED,
            )

        item = build_call_item(
            item_id, config.server_label, name, args,
            status="completed", output=result.get("content"),
        )
        events.append(dict(base, type=EVENT_CALL_COMPLETED, item=item))
        if idempotency_key and self._idempotency is not None:
            await self._idempotency.mark_executed(
                idempotency_key,
                workspace_id=workspace_id,
                response_id=execution_id_for(response_id, tool_seq),
                status_code=200,
                request_digest=digest,
                ttl_seconds=self._result_ttl,
            )
        await self._audit(
            response_id, workspace_id, tool_seq,
            status=STATUS_COMPLETED, approval_state=approval_state,
            idempotency_key=idempotency_key,
        )
        return McpOutcome(kind=OUTCOME_COMPLETED, item=item, events=tuple(events))

    # -- 内部：失败收口 ----------------------------------------------------

    async def _call_failed(
        self,
        base: Mapping[str, Any],
        events: list[dict[str, Any]],
        config: McpServerConfig,
        response_id: str,
        workspace_id: str,
        tool_seq: int,
        name: str,
        args: Mapping[str, Any],
        code: str,
        message: str,
        *,
        approval_state: str,
        status: str,
    ) -> McpOutcome:
        """判据③的唯一出口：任何失败都在这里变成 ``mcp_call.failed``。"""
        error = build_error(code, message)
        item = build_call_item(
            base["item_id"], config.server_label, name, args,
            status="incomplete", error=error,
        )
        events = list(events)
        events.append(dict(base, type=EVENT_CALL_FAILED, item=item, error=error))
        await self._audit(
            response_id, workspace_id, tool_seq,
            status=status, approval_state=approval_state,
        )
        return McpOutcome(
            kind=OUTCOME_FAILED, item=item, events=tuple(events), error=error,
        )

    def _argument_events(
        self, base: Mapping[str, Any], args: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """``arguments.delta`` * N -> ``arguments.done``（永远成对）。"""
        text = canonical_json(dict(args))
        size = self._chunk if self._chunk > 0 else len(text) or 1
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or [""]
        events = [
            dict(base, type=EVENT_CALL_ARGUMENTS_DELTA, delta=chunk)
            for chunk in chunks
        ]
        events.append(dict(base, type=EVENT_CALL_ARGUMENTS_DONE, arguments=text))
        return events

    # -- 内部：幂等租约（偿还 T26 STUB #3）--------------------------------

    async def _acquire(
        self,
        key: str,
        *,
        workspace_id: str,
        digest: str,
        response_id: str,
        tool_seq: int,
    ) -> _Lease:
        """占位；占不到就轮询等原执行者，并区分 duplicate / conflict / 超时。

        conflict 时**不覆写**那条记录：改写它会把原执行者的租约放掉，第三个
        并发请求就能占位并真的执行一次副作用 —— 为了记一笔冲突而制造一次重复
        执行，是净损失。冲突只往上报，不动存储。
        """
        if not key or self._idempotency is None:
            return _Lease(LEASE_PROCEED)
        holder = execution_id_for(response_id, tool_seq)
        waited = 0.0
        while True:
            if await self._idempotency.reserve(
                key,
                workspace_id=workspace_id,
                response_id=holder,
                request_digest=digest,
                ttl_seconds=self._reservation_ttl,
            ):
                return _Lease(LEASE_PROCEED)
            record = await self._idempotency.lookup(key, workspace_id=workspace_id)
            if record is not None:
                other = str(record.get("request_digest") or "")
                if other and digest and other != digest:
                    return _Lease(LEASE_CONFLICT, holder=str(record["response_id"]))
                if record["state"] == STATE_DONE:
                    return _Lease(LEASE_DUPLICATE, holder=str(record["response_id"]))
            if waited >= self._wait_seconds:
                return _Lease(LEASE_WAIT_TIMEOUT)
            await self._sleep(self._poll_seconds)
            waited += self._poll_seconds or self._wait_seconds

    # -- 内部：审计落库（判据②）------------------------------------------

    async def _audit(
        self,
        response_id: str,
        workspace_id: str,
        tool_seq: int,
        *,
        status: str,
        approval_state: str,
        idempotency_key: str = "",
    ) -> None:
        """把执行状态写进 ``tool_executions``。

        ``tool_seq < 0`` 表示调用方不要求持久化（例如纯协议层单测）；
        ``workspace_id`` 逐条透传，租户隔离由存储层的主键与查询条件保证。
        """
        if self._executions is None or tool_seq < 0:
            return
        await self._executions.record(
            response_id=response_id,
            workspace_id=workspace_id,
            tool_seq=tool_seq,
            tool_type=MCP_TOOL_TYPE,
            capability=MCP_CAPABILITY,
            status=status,
            approval_state=approval_state,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# 10. 归一化工具
# ---------------------------------------------------------------------------


def request_digest(
    server_label: str, name: str, arguments: Mapping[str, Any],
) -> str:
    """幂等键的「同键异体」判据。

    用 §10.7 的 :func:`canonical_json`（排序键、无空白）而不是 ``str(dict)``：
    只是换了键序的同一个请求必须算作**同一体**，否则幂等保护会被 Python 的
    dict 插入序随手绕过。
    """
    payload = canonical_json({
        "server_label": server_label, "name": name, "arguments": dict(arguments),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_tool(tool: Any) -> dict[str, Any]:
    """SDK 模型 / 原始 dict -> 官方 ``mcp_list_tools.tools[]`` 条目。"""
    if isinstance(tool, Mapping):
        raw: Mapping[str, Any] = tool
        get = raw.get
    else:
        get = lambda attr, default=None: getattr(tool, attr, default)  # noqa: E731
    schema = get("inputSchema", None)
    if schema is None:
        schema = get("input_schema", None)
    return {
        "name": str(get("name", "") or ""),
        "description": str(get("description", "") or ""),
        "input_schema": _to_plain(schema) if schema is not None else {},
    }


def _normalize_call_result(result: Any) -> dict[str, Any]:
    """SDK ``CallToolResult`` / 原始 dict -> ``{"isError": bool, "content": [...]}``。"""
    if isinstance(result, Mapping):
        content = result.get("content")
        is_error = bool(result.get("isError") or result.get("is_error"))
    else:
        content = getattr(result, "content", None)
        is_error = bool(
            getattr(result, "isError", None) or getattr(result, "is_error", False)
        )
    return {
        "isError": is_error,
        "content": [_to_plain(part) for part in (content or ())],
    }


def _to_plain(value: Any) -> Any:
    """把 pydantic 模型 / dataclass 之类降成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    for attr in ("model_dump", "dict"):
        dumper = getattr(value, attr, None)
        if callable(dumper):
            try:
                return _to_plain(dumper())
            except TypeError:  # pragma: no cover - 签名不兼容的第三方对象
                pass
    return str(value)


def _content_text(content: Any) -> str:
    """把 MCP ``content`` 数组里的文本块拼成一行（错误消息用）。"""
    if not isinstance(content, (list, tuple)):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("text")
    ]
    return " ".join(p for p in parts if p)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_to_plain(value), ensure_ascii=False)


__all__ = [
    # 事件族
    "EVENT_LIST_TOOLS_IN_PROGRESS",
    "EVENT_LIST_TOOLS_COMPLETED",
    "EVENT_LIST_TOOLS_FAILED",
    "EVENT_CALL_IN_PROGRESS",
    "EVENT_CALL_ARGUMENTS_DELTA",
    "EVENT_CALL_ARGUMENTS_DONE",
    "EVENT_CALL_COMPLETED",
    "EVENT_CALL_FAILED",
    "EVENT_APPROVAL_REQUEST",
    "MCP_EVENT_TYPES",
    # 错误码与状态
    "MCP_ERROR_CODES",
    "ERROR_TIMEOUT",
    "ERROR_TRANSPORT",
    "ERROR_TOOL_FAILED",
    "ERROR_APPROVAL_REJECTED",
    "ERROR_TOOL_NOT_ALLOWED",
    "ERROR_IDEMPOTENCY_CONFLICT",
    "ERROR_IDEMPOTENCY_WAIT_TIMEOUT",
    "ERROR_DEPENDENCY_MISSING",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_REJECTED",
    "STATUS_DUPLICATE",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_PENDING_APPROVAL",
    "OUTCOME_DUPLICATE",
    # 异常
    "McpError",
    "McpDependencyError",
    "McpTransportError",
    "McpToolError",
    # transport / session
    "McpTransport",
    "McpSession",
    "JsonRpcMcpSession",
    "HttpMcpTransport",
    "SdkMcpSession",
    "InMemoryMcpServer",
    "connect",
    "load_mcp_sdk",
    "MCP_PROTOCOL_VERSION",
    "DEPENDENCY_HINT",
    # 配置与结果
    "McpServerConfig",
    "McpOutcome",
    "ApprovalDecision",
    "McpClient",
    # 构造器
    "make_list_tools_item_id",
    "make_call_item_id",
    "make_approval_request_id",
    "parse_approval_request_id",
    "build_list_tools_item",
    "build_call_item",
    "build_approval_request_item",
    "build_approval_response_item",
    "build_error",
    "request_digest",
]
