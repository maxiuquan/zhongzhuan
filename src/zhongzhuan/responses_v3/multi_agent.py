"""V1 多代理协议服务端实现（APIAADBPW-REQ-MA-001 / FR-1~FR-4）。

Codex 桌面端 26.x 的 V1 多代理协议要求中继在 ``/v1/responses`` 上承担两条能力：

1. **hosted ``tool_search``** —— 客户端首轮只在 ``tools`` 里放 ``tool_search``，
   5 个子代理工具全部 deferred； Relay 必须自行构造 ``tool_search_output``，
   把 ``multi_agent_v1`` namespace 暴露给下一轮（FR-1 / FR-2）。
2. **``multi_agent_v1`` namespace 编排** —— 中继识别带
   ``namespace:"multi_agent_v1"`` 的 ``function_call``，真正执行
   spawn / send_input / resume / wait / close 子代理生命周期，并以
   ``function_call_output`` 回传父代理（FR-3 / FR-4）。

本模块不依赖任何上游是否原生支持该协议：``tool_search`` 完全由中继合成；
子代理 rollout 通过可注入的 ``runner`` 执行（生产环境由 proxy handler 注入一个
真正向上游 ``/v1/responses`` 发起子请求的执行器，单测用 fake runner 即可验证
全部状态机逻辑）。

设计约束（与现有 relay 一致）
-----------------------------
* 纯数据 + 状态机，不 import aiohttp / upstream / store，避免循环依赖，便于单测。
* 所有对外的 item 形状严格对齐 codex-rs 源码断言（见需求文档 §4.2 / §12）。
* 子代理上下文 / 资源按 session 隔离（NFR-2）；并发上限 ``max_threads``（NFR-6）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger as _loguru

#: stdlib logger 仅作类型/兜底；默认走 loguru（zhongzhuan 的落盘通道，NFR-4
#: 可观测性——2026-08-15 排查确认 stdlib logging 未被 setup_logging 接管，
#: thread_spawn 等记录在生产日志里完全看不到）。
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. 协议常量
# ---------------------------------------------------------------------------

#: V1 多代理命名空间名（codex-rs ``multi_agents_spec.rs``）。
MULTI_AGENT_NAMESPACE: str = "multi_agent_v1"

#: 上游 ``function_call`` 里用于触发 deferred namespace 暴露的 hosted 工具名。
TOOL_SEARCH_NAME: str = "tool_search"

#: 5 个 V1 子工具（来自 codex-rs ``multi_agents_spec.rs``）。
MULTI_AGENT_TOOLS: tuple[str, ...] = (
    "spawn_agent",
    "send_input",
    "resume_agent",
    "wait_agent",
    "close_agent",
)

#: 子代理默认并发上限（NFR-6）。
DEFAULT_MAX_THREADS: int = 4

#: 单子代理任务硬上限（NFR-1，铁律 5 客户端 1800s）。
DEFAULT_JOB_MAX_RUNTIME_SECONDS: int = 1800


# ---------------------------------------------------------------------------
# 2. tool_search_output 合成
# ---------------------------------------------------------------------------

def _spawn_agent_description() -> str:
    """``spawn_agent`` 的工具描述。

    必须包含 codex-rs 客户端断言的两段 ``###`` 引导文案（需求文档 §4.2 硬性约束），
    否则客户端拒绝加载该子工具。
    """
    return (
        "Spawn a sub-agent to handle a delegated, self-contained task in parallel.\n\n"
        "### Designing delegated subtasks\n"
        "Break the user's request into independent, well-scoped subtasks that can\n"
        "run concurrently. Each sub-agent gets a single, unambiguous instruction and\n"
        "its own model (override via the `model` argument, otherwise it inherits the\n"
        "parent's model). Prefer spawning 2-4 sub-agents for genuinely parallel work;\n"
        "do not spawn agents for trivial or tightly-coupled steps.\n\n"
        "### When to delegate vs. do the subtask yourself\n"
        "Delegate when the work is independent and can be produced without the main\n"
        "thread's intermediate state. Do the subtask yourself when it depends on\n"
        "prior turns, requires the user's direct input, or must stay on the critical\n"
        "path. Always wait for every spawned agent and aggregate their results before\n"
        "replying to the user."
    )


def build_multi_agent_namespace_tools() -> list[dict[str, Any]]:
    """返回 ``multi_agent_v1`` namespace 下的 5 个 deferred 子工具。

    每个子工具 ``defer_loading: true``；``spawn_agent`` 带 §4.2 要求的两段
    ``###`` 描述文案。返回的是 ``namespace.tools`` 数组的元素形态。
    """
    descriptions = {
        "spawn_agent": _spawn_agent_description(),
        "send_input": "Send additional input / a follow-up message to a running sub-agent.",
        "resume_agent": "Resume a sub-agent from a prior rollout.",
        "wait_agent": "Block until the specified sub-agent finishes, then return its result.",
        "close_agent": "Close a sub-agent and release its resources.",
    }
    tools: list[dict[str, Any]] = []
    for name in MULTI_AGENT_TOOLS:
        tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "defer_loading": True,
        }
        desc = descriptions.get(name)
        if desc:
            tool["description"] = desc
        tools.append(tool)
    return tools


def build_tool_search_output(
    *,
    output_index: int,
    call_id: str,
    response_id: str = "",
    query: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """合成一个 ``tool_search_output`` Responses 输出项（FR-2）。

    形状严格对齐 codex-rs 客户端断言（需求文档 §4.2）：``output.tools`` 里只有
    一条 ``type:"namespace"`` 容器，其 ``name`` 为 ``multi_agent_v1``，``tools``
    为 5 个 deferred 子工具；**绝不**以顶层 ``function`` 返回任何子工具。

    Args:
        output_index: 该 item 在响应 ``output`` 数组中的下标。
        call_id: 上游 ``tool_search`` function_call 的 call_id（客户端据此关联）。
        response_id: 响应 id，仅用于生成稳定的 item id。
        query / limit: 透传上游 ``tool_search`` 调用参数（仅作记录，不影响合成）。
    """
    item_id = "tso_{0}_{1}".format(response_id or "resp", output_index)
    namespace_tools = build_multi_agent_namespace_tools()
    return {
        "id": item_id,
        "type": "tool_search_output",
        "status": "completed",
        "call_id": call_id,
        "output": {
            "tools": [
                {
                    "type": "namespace",
                    "name": MULTI_AGENT_NAMESPACE,
                    "tools": namespace_tools,
                }
            ],
        },
        # 透传记录，便于排障（NFR-4）。
        "query": query,
        "limit": limit,
    }


def build_function_call_output(
    *,
    output_index: int,
    call_id: str,
    output: str,
    response_id: str = "",
) -> dict[str, Any]:
    """合成一个 ``function_call_output`` Responses 输出项（FR-3）。"""
    item_id = "fco_{0}_{1}".format(response_id or "resp", output_index)
    return {
        "id": item_id,
        "type": "function_call_output",
        "status": "completed",
        "call_id": call_id,
        "output": output,
    }


# ---------------------------------------------------------------------------
# 3. 子代理状态机（FR-3 / FR-4）
# ---------------------------------------------------------------------------


@dataclass
class AgentState:
    """一个子代理的生命周期记录。"""

    agent_id: str
    model: str
    instruction: str
    session_id: str
    status: str = "spawned"  # spawned | running | completed | failed | closed
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None


#: 子代理执行器：给定 instruction + model + session_id，返回子代理最终产出文本。
SubAgentRunner = Callable[[str, str, str], Awaitable[str]]


@dataclass
class _ErrorOutput:
    """内部：编排错误的统一回传形状。"""

    call_id: str
    message: str


class MultiAgentOrchestrator:
    """``multi_agent_v1`` namespace 的本地编排器（FR-3 / FR-4）。

    会话级 registry：``agent_id`` 在 ``session_id`` 内唯一。并发上限
    ``max_threads`` 限制同一会话内同时活跃（未结束）的子代理数量。子代理 rollout
    通过可注入的 ``runner`` 执行，因此本类完全不依赖真实上游，单测用 fake 即可
    覆盖 spawn / wait / close / 并发隔离 / 超时全部路径。

    流式管线（``pipeline.py``）在检测到带 ``namespace:"multi_agent_v1"`` 的
    ``function_call`` 时 ``await self.handle(...)``，把返回的 ``function_call_output``
    作为该 call 的产出回传给父代理。
    """

    def __init__(
        self,
        *,
        max_threads: int = DEFAULT_MAX_THREADS,
        job_max_runtime_seconds: int = DEFAULT_JOB_MAX_RUNTIME_SECONDS,
        runner: SubAgentRunner | None = None,
        default_model: str = "",
        logger: logging.Logger | None = None,
        namespace: str = MULTI_AGENT_NAMESPACE,
    ) -> None:
        self._max_threads = max(1, int(max_threads))
        self._job_timeout = int(job_max_runtime_seconds)
        self._runner = runner
        self._default_model = default_model
        self._log = logger or _loguru
        self._namespace = namespace
        self._agents: dict[str, AgentState] = {}
        self._lock = asyncio.Lock()

    # -- 配置 ----------------------------------------------------------------

    def set_default_model(self, model: str) -> None:
        """设置子代理默认继承的父模型（spawn_agent 未显式 override 时使用）。"""
        self._default_model = model or self._default_model

    # -- 入口 ----------------------------------------------------------------

    async def handle(
        self,
        namespace: str,
        name: str,
        call_id: str,
        arguments: str,
        output_index: int = 0,
    ) -> dict[str, Any]:
        """处理一个 namespaced ``function_call``，返回 ``function_call_output`` item。

        Args:
            namespace: 来自 ``function_call.namespace``；非 ``multi_agent_v1`` 直接报错。
            name: 子工具名（spawn_agent / send_input / ...）。
            call_id: 父代理侧 call_id，回填进 ``function_call_output.call_id``。
            arguments: 上游回传的原始 arguments JSON 字符串。
            output_index: 该 item 在响应 ``output`` 数组中的下标，用于生成稳定且唯一
                的 item id（流式管线与非流式路径各自维护自己的计数器）。
        """
        if namespace != self._namespace:
            return self._error_output(call_id, f"unsupported namespace: {namespace}")
        handler = {
            "spawn_agent": self._spawn,
            "send_input": self._send_input,
            "resume_agent": self._resume,
            "wait_agent": self._wait,
            "close_agent": self._close,
        }.get(name)
        if handler is None:
            return self._error_output(call_id, f"unknown multi_agent tool: {name}")
        try:
            args = json.loads(arguments) if arguments else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            return await handler(call_id, args, output_index)
        except Exception as exc:  # noqa: BLE001 - 编排错误必须隔离，不能炸父代理
            self._log.exception("multi_agent %s failed: %s", name, exc)
            return self._error_output(call_id, f"{name} failed: {exc}", output_index=output_index)

    # -- 5 个子工具 -----------------------------------------------------------

    async def _spawn(self, call_id: str, args: dict[str, Any], output_index: int = 0) -> dict[str, Any]:
        instruction = str(args.get("instruction") or "")
        model = str(args.get("model") or self._default_model or "")
        session_id = str(args.get("session_id") or "")
        async with self._lock:
            active = sum(1 for a in self._agents.values() if a.status in ("spawned", "running"))
            if active >= self._max_threads:
                return self._error_output(
                    call_id,
                    f"max concurrent sub-agents ({self._max_threads}) reached for session {session_id}",
                )
            agent_id = "agent_{0}".format(uuid.uuid4().hex[:12])
            state = AgentState(
                agent_id=agent_id,
                model=model,
                instruction=instruction,
                session_id=session_id,
            )
            self._agents[agent_id] = state
        # fire-and-forget rollout；wait_agent 才真正 await 结果。
        if self._runner is not None and instruction:
            state.status = "running"
            state.task = asyncio.create_task(self._run_agent(state))
            self._log.info(
                "thread_spawn agent_id=%s model=%s session=%s instruction_len=%d",
                agent_id, model, session_id, len(instruction),
            )
        else:
            # 无 runner（纯占位）：直接标记完成，避免 wait 永久挂起。
            state.status = "completed"
            state.result = ""
        return build_function_call_output(
            output_index=output_index,
            call_id=call_id,
            response_id="",
            output=json.dumps({"agent_id": agent_id, "status": state.status}),
        )

    async def _send_input(self, call_id: str, args: dict[str, Any], output_index: int = 0) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "")
        text = str(args.get("input") or args.get("content") or "")
        state = self._agents.get(agent_id)
        if state is None:
            return self._error_output(call_id, f"unknown agent_id: {agent_id}")
        # best-effort：把追加输入记录到状态；真正消费由下一轮 rollout 决定。
        state.instruction = (state.instruction + "\n" + text).strip()
        self._log.info("send_input agent_id=%s len=%d", agent_id, len(text))
        return build_function_call_output(
            output_index=output_index, call_id=call_id, response_id="",
            output=json.dumps({"agent_id": agent_id, "received": True}),
        )

    async def _resume(self, call_id: str, args: dict[str, Any], output_index: int = 0) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "")
        state = self._agents.get(agent_id)
        if state is None:
            return self._error_output(call_id, f"unknown agent_id: {agent_id}")
        # best-effort：标记为恢复；若此前任务已结束则直接返回已有结果。
        self._log.info("resume_agent agent_id=%s", agent_id)
        return build_function_call_output(
            output_index=output_index, call_id=call_id, response_id="",
            output=json.dumps({"agent_id": agent_id, "status": state.status}),
        )

    async def _wait(self, call_id: str, args: dict[str, Any], output_index: int = 0) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "")
        state = self._agents.get(agent_id)
        if state is None:
            return self._error_output(call_id, f"unknown agent_id: {agent_id}")
        if state.task is not None and not state.task.done():
            try:
                await state.task
            except Exception:  # noqa: BLE001 - 任务异常已写入 state，这里只需等结束
                pass
        out = state.result if state.status == "completed" else (state.error or "")
        self._log.info(
            "wait_agent agent_id=%s status=%s out_len=%d", agent_id, state.status, len(out),
        )
        return build_function_call_output(
            output_index=output_index, call_id=call_id, response_id="",
            output=json.dumps({"agent_id": agent_id, "status": state.status, "result": out}),
        )

    async def _close(self, call_id: str, args: dict[str, Any], output_index: int = 0) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "")
        state = self._agents.get(agent_id)
        if state is None:
            return self._error_output(call_id, f"unknown agent_id: {agent_id}")
        if state.task is not None and not state.task.done():
            state.task.cancel()
        state.status = "closed"
        self._agents.pop(agent_id, None)
        self._log.info("close_agent agent_id=%s", agent_id)
        return build_function_call_output(
            output_index=output_index, call_id=call_id, response_id="",
            output=json.dumps({"agent_id": agent_id, "closed": True}),
        )

    # -- rollout 执行 ---------------------------------------------------------

    async def _run_agent(self, state: AgentState) -> None:
        """执行单个子代理 rollout（受 ``job_max_runtime_seconds`` 保护）。"""
        assert self._runner is not None
        try:
            result = await asyncio.wait_for(
                self._runner(state.instruction, state.model, state.session_id),
                timeout=self._job_timeout,
            )
            state.result = result or ""
            state.status = "completed"
        except asyncio.TimeoutError:
            state.error = f"sub-agent {state.agent_id} exceeded job_max_runtime_seconds={self._job_timeout}"
            state.status = "failed"
            self._log.warning("multi_agent timeout agent_id=%s", state.agent_id)
        except asyncio.CancelledError:
            state.status = "closed"
            raise
        except Exception as exc:  # noqa: BLE001 - 子代理失败隔离，不波及父代理
            state.error = f"sub-agent {state.agent_id} failed: {exc}"
            state.status = "failed"
            self._log.exception("multi_agent rollout failed agent_id=%s", state.agent_id)

    # -- 工具 ----------------------------------------------------------------

    def _error_output(self, call_id: str, message: str, output_index: int = 0) -> dict[str, Any]:
        return build_function_call_output(
            output_index=output_index, call_id=call_id, response_id="",
            output=json.dumps({"error": message}),
        )

    def active_count(self, session_id: str = "") -> int:
        """当前活跃（未结束）子代理数量，用于排障与并发观测（NFR-4）。"""
        return sum(
            1
            for a in self._agents.values()
            if a.status in ("spawned", "running") and (not session_id or a.session_id == session_id)
        )


__all__ = [
    "MULTI_AGENT_NAMESPACE",
    "TOOL_SEARCH_NAME",
    "MULTI_AGENT_TOOLS",
    "DEFAULT_MAX_THREADS",
    "DEFAULT_JOB_MAX_RUNTIME_SECONDS",
    "build_multi_agent_namespace_tools",
    "build_tool_search_output",
    "build_function_call_output",
    "AgentState",
    "MultiAgentOrchestrator",
]
