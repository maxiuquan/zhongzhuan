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
from .responses_models import ReasoningEventMode
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
        return [{"type": ITEM_MESSAGE, "role": ROLE_USER,
                 "content": [{"type": ITEM_INPUT_TEXT, "text": text}]}]
    if isinstance(input_val, list):
        if len(input_val) == 0:
            return [{"type": ITEM_MESSAGE, "role": ROLE_USER,
                     "content": [{"type": ITEM_INPUT_TEXT, "text": "..."}]}]
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
            result["messages"].append({"role": item["role"], "content": content})

        elif item_type == ITEM_FUNCTION_CALL:
            name = item.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                # Nameless tool calls are rejected upstream. Skip before creating
                # an assistant shell, otherwise we emit `tool_calls: []` which is
                # itself invalid for strict upstreams.
                continue
            if current_assistant is None:
                current_assistant = {"role": ROLE_ASSISTANT, "content": None, "tool_calls": []}
            current_assistant["tool_calls"].append({
                "id": item.get("call_id"),
                "type": BLOCK_FUNCTION,
                "function": {"name": name, "arguments": item.get("arguments")},
            })

        elif item_type == ITEM_FUNCTION_CALL_OUTPUT:
            if current_assistant is not None:
                result["messages"].append(current_assistant)
                current_assistant = None
            if pending_tool_results:
                result["messages"].extend(pending_tool_results)
                pending_tool_results = []
            output = item.get("output")
            result["messages"].append({
                "role": ROLE_TOOL,
                "tool_call_id": item.get("call_id"),
                "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
            })

    if current_assistant is not None:
        result["messages"].append(current_assistant)
    if pending_tool_results:
        result["messages"].extend(pending_tool_results)

    # Convert tools format (drop hosted tools with no name).
    if isinstance(body.get("tools"), list):
        result["tools"] = [t for t in (_convert_tool(t) for t in body["tools"]) if t]

    # Map Responses-only max_output_tokens -> Chat max_tokens.
    if "max_output_tokens" in result:
        if "max_tokens" not in result:
            result["max_tokens"] = result["max_output_tokens"]
        del result["max_output_tokens"]

    for f in ("input", "instructions", "include", "prompt_cache_key", "store", "client_metadata"):
        result.pop(f, None)
    if isinstance(result.get("reasoning"), dict) and isinstance(result["reasoning"].get("effort"), str):
        result["reasoning_effort"] = result["reasoning"]["effort"]
    result.pop("reasoning", None)

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
        output.append({
            "id": f"msg_{msg_id_base}_0",
            "type": ITEM_MESSAGE,
            "role": ROLE_ASSISTANT,
            "content": [{"type": ITEM_OUTPUT_TEXT, "text": content, "annotations": []}],
            "status": "completed",
        })
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        output.append({
            "id": f"fc_{tc.get('id', '')}",
            "type": ITEM_FUNCTION_CALL,
            "call_id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        })

    usage = resp.get("usage") or {}
    return {
        "id": msg_id_base,
        "object": "response",
        "created_at": resp.get("created", int(time.time())),
        "status": "completed",
        "model": resp.get("model", model),
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