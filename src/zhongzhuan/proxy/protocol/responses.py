"""OpenAI Responses API (Codex) <-> Chat Completions translation.

对外兼容门面 (§2.10)：保留四个公开符号，真实逻辑都在新模块。
* ``convert_responses_request_to_chatcompletions`` / ``chatcompletions_to_responses``
  -- 纯请求/响应转换（体积小且自包含，保留在此）；
* ``ResponsesStreamTranslator`` -- 委托 :class:`~.responses_bridge.ResponsesTurnBridge` 的薄适配壳；
* ``CompositeStreamTranslator`` -- 把上游翻译器输出管道进下游。
流式引擎 / turn 累积 / 事件发射在 ``responses_bridge.py`` / ``turn_accumulator.py`` /
``responses_emitter.py``。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .responses_bridge import ResponsesTurnBridge
from .responses_models import HOSTED_TOOL_CAPABILITY, Capability, NAMESPACE_TOOL_TYPE, ReasoningEventMode
from .tool_accumulator import split_namespace_name
from .translator_base import finish_translator

# ---- OpenAI / Responses API constants (stable string values) ----
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

BLOCK_TEXT = "text"
BLOCK_IMAGE_URL = "image_url"
BLOCK_FUNCTION = "function"

ITEM_MESSAGE = "message"
ITEM_INPUT_TEXT = "input_text"
ITEM_OUTPUT_TEXT = "output_text"
ITEM_INPUT_IMAGE = "input_image"
ITEM_FUNCTION_CALL = "function_call"
ITEM_FUNCTION_CALL_OUTPUT = "function_call_output"
ITEM_REASONING = "reasoning"
ITEM_SUMMARY_TEXT = "summary_text"

# 上游透传的 hosted 工具类型：中继不执行、原样转发给上游由上游执行。当前仅
# web_search 系列（见 ``HOSTED_TOOL_CAPABILITY`` 里映射到 ``Capability.WEB_SEARCH``
# 的全部别名）。翻译到 Chat Completions 时也保留这些工具，否则上游收不到搜索请求。
WEB_SEARCH_TOOL_TYPES: frozenset[str] = frozenset(
    t for t, cap in HOSTED_TOOL_CAPABILITY.items() if cap is Capability.WEB_SEARCH
)


def _normalize_tool_parameters(params: Any) -> dict:
    if not params:
        return {"type": "object", "properties": {}}
    if params.get("type") == "object" and "properties" not in params:
        return {**params, "properties": {}}
    return params


def normalize_responses_input(input_val: Any) -> list | None:
    """Responses ``input`` may be a string or an array. Returns a list, or None."""
    if isinstance(input_val, str):
        text = input_val.strip() or "..."
        return [{"type": ITEM_MESSAGE, "role": ROLE_USER, "content": [{"type": ITEM_INPUT_TEXT, "text": text}]}]
    if isinstance(input_val, list):
        if len(input_val) == 0:
            return [{"type": ITEM_MESSAGE, "role": ROLE_USER, "content": [{"type": ITEM_INPUT_TEXT, "text": "..."}]}]
        return input_val
    return None


def _convert_content_block(c: Any) -> Any:
    if not isinstance(c, dict):
        return c
    t = c.get("type")
    if t in (ITEM_INPUT_TEXT, ITEM_OUTPUT_TEXT):
        return {"type": BLOCK_TEXT, "text": c.get("text", "")}
    if t == ITEM_INPUT_IMAGE:
        url = c.get("image_url") or c.get("file_id") or ""
        return {"type": BLOCK_IMAGE_URL, "image_url": {"url": url, "detail": c.get("detail", "auto")}}
    return c


def _convert_tool(tool: Any) -> Any:
    if not isinstance(tool, dict):
        return tool
    # Already in Chat Completions format: { type: "function", function: {...} }
    if tool.get("function"):
        return tool
    name = tool.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        # Hosted tools (e.g. request_user_input) have no name -> drop.
        return None
    return {
        "type": BLOCK_FUNCTION,
        "function": {
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": _normalize_tool_parameters(tool.get("parameters")),
            **({"strict": tool["strict"]} if "strict" in tool else {}),
        },
    }


#: namespace 摊平分隔符。Codex 桌面版 26.x 用 ``type:"namespace"`` 容器声明
#: MCP 子代理工具组（如 ``mcp__subagents__`` 下的 ``spawn_agent``）。Chat
#: Completions / Messages 上游没有 namespace 概念，必须摊平成普通 function。
#: 命名 ``mcp__{server}__{subtool}``——但 **必须用连字符 ``-`` 分隔**
#: ``mcp__{server}`` 与子工具名：
#:
#: * 上游硬约束：OpenAI Chat Completions 的 function name 正则
#:   ``^[a-zA-Z0-9_-]+$``，**点 ``.`` 不合法**（2026-08-07 实测 macc.eu.cc
#:   直接 400 ``Invalid 'tools[0].name': string does not match pattern``）；
#: * 无歧义：``mcp__subagents__`` 内不含 ``-``，用 ``rpartition("-")`` 一定能
#:   把 ``mcp__subagents__-spawn_agent`` 拆回 ``(namespace, subtool)``；
#: * 别无分隔符拼接（``mcp__subagents__spawn_agent``）会让还原逻辑分不清
#:   边界（``__`` 在 namespace 名里已经出现）。
NAMESPACE_FLAT_SEP: str = "-"


def _flatten_namespace_tool(tool: dict) -> list[dict]:
    """把一条 ``type:"namespace"`` 工具摊平成若干普通 function 工具。

    返回的每个 function 名形如 ``{namespace_name}{sep}{subtool_name}``，例如
    ``mcp__subagents__-spawn_agent``。子工具可能本身也是 namespace（嵌套），
    这里只摊平一层（Codex 的 MCP 桥接最多一层）；非 function 子工具（hosted
    之类）跳过，与上层 hosted tool drop 语义一致。
    """
    ns_name = str(tool.get("name") or "").strip()
    if not ns_name:
        return []
    out: list[dict] = []
    for sub in tool.get("tools") or []:
        if not isinstance(sub, dict):
            continue
        sub_type = str(sub.get("type") or "")
        if sub_type == NAMESPACE_TOOL_TYPE:
            # 嵌套 namespace：递归摊平（前缀叠加）。
            for nested in _flatten_namespace_tool(sub):
                out.append(_prefix_function_name(nested, ns_name))
            continue
        if sub_type != "function" and not sub.get("function"):
            # 子工具不是 function 形态（如 hosted 子工具）：Chat Completions
            # 无合法表达，与顶层 hosted drop 一致。
            continue
        fn = sub.get("function") if isinstance(sub.get("function"), dict) else sub
        sub_name = str(fn.get("name") or "").strip()
        if not sub_name:
            continue
        out.append(
            {
                "type": BLOCK_FUNCTION,
                "function": {
                    "name": "{0}{1}{2}".format(ns_name, NAMESPACE_FLAT_SEP, sub_name),
                    "description": str(fn.get("description") or ""),
                    "parameters": _normalize_tool_parameters(fn.get("parameters")),
                },
            }
        )
    return out


def _prefix_function_name(tool: dict, prefix: str) -> dict:
    """给一个已摊平的 function 工具名加前缀（嵌套 namespace 用）。"""
    if not isinstance(tool, dict):
        return tool
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        tool = {**tool, "function": {**fn, "name": "{0}{1}{2}".format(prefix, NAMESPACE_FLAT_SEP, fn["name"])}}
    return tool


def convert_responses_request_to_chatcompletions(body: dict) -> dict:
    """Convert an OpenAI Responses API request body to Chat Completions format."""
    if not isinstance(body, dict) or not body.get("input"):
        return body

    result: dict = dict(body)
    result["messages"] = []

    if body.get("instructions"):
        result["messages"].append({"role": ROLE_SYSTEM, "content": body["instructions"]})

    current_assistant: dict | None = None
    pending_tool_results: list[dict] = []

    input_items = normalize_responses_input(body["input"])
    if input_items is None:
        return body

    for item in input_items:
        item_type = item.get("type") or (item.get("role") and ITEM_MESSAGE or None)

        if item_type == ITEM_MESSAGE:
            if current_assistant is not None:
                result["messages"].append(current_assistant)
                current_assistant = None
            if pending_tool_results:
                result["messages"].extend(pending_tool_results)
                pending_tool_results = []
            content = item.get("content")
            if isinstance(content, list):
                content = [_convert_content_block(c) for c in content]
            # Chat Completions has no `developer` role -- only `system`.  Remap
            # it so strict non-OpenAI upstreams don't reject the request with a
            # 400.  This covers BOTH the current turn AND replayed transcript
            # items from previous_response_id chains, which are flattened into
            # body["input"] (see chain.build_upstream_input) and carry
            # role: "developer" verbatim from the native Responses store.
            # The OpenAI-native Responses path (gpt-5.6-sol) is unaffected: it
            # never enters this translator and keeps developer as-is.
            role = ROLE_SYSTEM if item["role"] == "developer" else item["role"]
            result["messages"].append({"role": role, "content": content})

        elif item_type == ITEM_FUNCTION_CALL:
            name = item.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                # Nameless tool calls are rejected upstream. Skip before creating
                # an assistant shell, otherwise we emit `tool_calls: []` which is
                # itself invalid for strict upstreams.
                continue
            if current_assistant is None:
                current_assistant = {"role": ROLE_ASSISTANT, "content": None, "tool_calls": []}
            current_assistant["tool_calls"].append(
                {
                    "id": item.get("call_id"),
                    "type": BLOCK_FUNCTION,
                    "function": {"name": name, "arguments": item.get("arguments")},
                }
            )

        elif item_type == ITEM_FUNCTION_CALL_OUTPUT:
            if current_assistant is not None:
                result["messages"].append(current_assistant)
                current_assistant = None
            if pending_tool_results:
                result["messages"].extend(pending_tool_results)
                pending_tool_results = []
            output = item.get("output")
            result["messages"].append(
                {
                    "role": ROLE_TOOL,
                    "tool_call_id": item.get("call_id"),
                    "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                }
            )

    if current_assistant is not None:
        result["messages"].append(current_assistant)
    if pending_tool_results:
        result["messages"].extend(pending_tool_results)

    # Convert tools format.  Hosted tools (web_search / file_search / computer /
    # code_interpreter / mcp / image_generation / ...) are declared by the
    # Responses API but have **no legal representation** in Chat Completions or
    # Messages -- they cannot be forwarded verbatim.  A strict upstream rejects
    # ``{"type": "web_search"}`` inside ``tools`` with HTTP 400 (2026-08-06 实测
    # 第三方 relay macc.eu.cc)，which then surfaces to Codex as a hard error /
    # empty reply.  The ONLY correct translation is to **drop** them here.
    #
    # 注意 R-P1-45 的边界：这里 drop 之后上游返回的仍是模型**真实**文本（只是
    # 没有被 web_search 增强），并非「空洞文本假装成功」。真正需要 hosted tool
    # 的请求会被能力路由器送到 NATIVE 模式的 key（走 /v1/responses、原样保留
    # 工具），根本不会进入这条 translate 路径；能进来的 translate 请求说明当前
    # 没有任何原生 route 能承载该能力，此时降级掉 hosted 工具、让主对话照常进行
    # 是比硬 400 更优的失败语义（用户侧不再看到空消息 / 400）。
    if isinstance(body.get("tools"), list):
        converted_tools: list[dict] = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                mapped = _convert_tool(tool)
                if mapped:
                    converted_tools.append(mapped)
                continue
            if tool.get("type") in HOSTED_TOOL_CAPABILITY:
                continue  # hosted tool: illegal in Chat Completions, drop
            if tool.get("type") == NAMESPACE_TOOL_TYPE:
                # Codex 26.x namespace 容器（MCP 子代理工具组）：Chat Completions
                # 无 namespace 概念，摊平成普通 function（点分隔名）。子工具丢失
                # 的 hosted 能力语义与顶层 hosted drop 一致。
                converted_tools.extend(_flatten_namespace_tool(tool))
                continue
            mapped = _convert_tool(tool)
            if mapped:
                converted_tools.append(mapped)
        result["tools"] = converted_tools

    # Map Responses-only max_output_tokens -> Chat max_tokens.
    if "max_output_tokens" in result:
        if "max_tokens" not in result:
            result["max_tokens"] = result["max_output_tokens"]
        del result["max_output_tokens"]

    # Responses ``reasoning`` (effort) -> Chat Completions ``reasoning_effort``
    # (a portable param); the remaining Responses-only ``reasoning`` object is
    # then dropped below.
    reasoning_cfg = result.get("reasoning")
    if isinstance(reasoning_cfg, dict) and isinstance(reasoning_cfg.get("effort"), str):
        result["reasoning_effort"] = reasoning_cfg["effort"]

    # Responses ``text`` (output text config) -> Chat Completions
    # ``response_format``.  Only the structured-output ``format`` is portable;
    # the plain ``{"type": "text"}`` default maps to nothing.
    text_cfg = result.pop("text", None)
    if isinstance(text_cfg, dict):
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict) and fmt.get("type") not in (None, "text"):
            result["response_format"] = fmt

    # Drop every remaining Responses-only field that has no Chat Completions /
    # Messages equivalent.  Forwarding them verbatim 400s on strict upstreams.
    for f in (
        "input",
        "instructions",
        "include",
        "prompt_cache_key",
        "store",
        "client_metadata",
        "reasoning",
        "truncation",
        "background",
        "previous_response_id",
    ):
        result.pop(f, None)

    return result


def chatcompletions_to_responses(resp: Any, model: str = "") -> Any:
    """Convert a non-streaming Chat Completions JSON response to Responses API JSON."""
    if not isinstance(resp, dict):
        return resp

    choices = resp.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    resp_id = str(resp.get("id") or "")
    msg_id_base = f"resp_{resp_id}" if resp_id else f"resp_{int(time.time())}"

    output: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append(
            {
                "id": f"msg_{msg_id_base}_0",
                "type": ITEM_MESSAGE,
                "role": ROLE_ASSISTANT,
                "content": [{"type": ITEM_OUTPUT_TEXT, "text": content, "annotations": []}],
                "status": "completed",
            }
        )
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        flat_name = str(fn.get("name", "") or "")
        # Codex 26.x MCP 子代理（namespace 工具）还原（与流式 pipeline 一致）：
        # 请求侧把 ``type:"namespace"`` 容器摊平成 ``mcp__subagents__-spawn_agent``
        # 发给 Chat 上游，上游回包里的 function.name 就是这个摊平名。回程必须
        # 拆回裸名 + 补回 ``namespace`` 字段，否则 Codex 的 router 不知道把这次
        # function_call 路由回哪个 MCP server（``unsupported call``，codex-relay
        # #17 / Palantir 修法）。
        ns, bare_name = split_namespace_name(flat_name)
        fc_item: dict = {
            "id": f"fc_{tc.get('id', '')}",
            "type": ITEM_FUNCTION_CALL,
            "call_id": tc.get("id", ""),
            "name": bare_name if ns else flat_name,
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        }
        if ns:
            # 关键：补回 namespace 字段，Codex 才能路由回对应 MCP server。
            fc_item["namespace"] = ns
        output.append(fc_item)

    usage = resp.get("usage") or {}
    return {
        "id": msg_id_base,
        "object": "response",
        "created_at": resp.get("created", int(time.time())),
        "status": "completed",
        # The client asked for ``model``; the upstream may rewrite it
        # (alias -> real id).  The Responses object must reflect the model the
        # CLIENT requested, falling back to the upstream's when unset.
        "model": model or resp.get("model", ""),
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming: Chat Completions SSE -> Responses SSE
# ---------------------------------------------------------------------------


class ResponsesStreamTranslator:
    """Chat Completions SSE -> Responses SSE 翻译器。

    §2.10 门面 + 组合：委托 :class:`~.responses_bridge.ResponsesTurnBridge`。
    对外契约不变：``ResponsesStreamTranslator(model="")``、
    ``await feed(chunk) -> list[bytes]``、``done``、``finish_safely()``、``usage``。
    """

    def __init__(
        self,
        model: str = "",
        *,
        reasoning_event_mode: str = ReasoningEventMode.SUMMARY_TEXT.value,
    ) -> None:
        self.model = model
        self._bridge = ResponsesTurnBridge(
            model=model,
            reasoning_event_mode=reasoning_event_mode,
        )
        self.usage: dict = self._bridge.usage

    @property
    def done(self) -> bool:
        return self._bridge.done

    def finish_safely(self) -> list[bytes]:
        return self._bridge.finish_safely()

    async def finish(self) -> list[bytes]:
        return await self._bridge.afinish()

    async def feed(self, chunk: bytes) -> list[bytes]:
        out = await self._bridge.feed(chunk)
        self.usage = self._bridge.usage
        return out


class CompositeStreamTranslator:
    """把上游翻译器输出管道进下游（如 Anthropic SSE -> OpenAI SSE -> Responses SSE）。

    接口与其他流翻译器一致：``feed``（async）、``done``、``finish_safely``、``usage``。
    """

    def __init__(self, first, second) -> None:
        self.first = first
        self.second = second

    @property
    def done(self) -> bool:
        return self.first.done and self.second.done

    @property
    def usage(self) -> dict:
        return getattr(self.second, "usage", {"prompt_tokens": 0, "completion_tokens": 0})

    async def finish_safely(self) -> list[bytes]:
        """Finish the pipeline, flushing both translators (async per §13).

        通过统一收尾入口 :func:`finish_translator` 收尾上游，再把上游产出的
        字节逐条喂给下游，最后同样走统一入口收尾下游。这里遍历的是
        ``finish_translator`` 返回的新列表，R-P1-65 禁止边遍历边 append 同一
        列表，此处安全。
        """
        out: list[bytes] = []
        for c in await finish_translator(self.first):
            out.extend(await self.second.feed(c))
        out.extend(await finish_translator(self.second))
        return out

    async def feed(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        for c in await self.first.feed(chunk):
            out.extend(await self.second.feed(c))
        return out
