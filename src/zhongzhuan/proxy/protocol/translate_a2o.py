"""Anthropic Messages API -> OpenAI Chat Completions translation.

Pure functions: translate Anthropic requests to OpenAI requests, and OpenAI
non-streaming responses back to Anthropic format.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger


# OpenAI finish_reason -> Anthropic stop_reason
MAP_FINISH_REASON_O2A: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def _system_to_text(system: Any) -> str:
    """Convert Anthropic ``system`` field (string or content block list) to text."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "".join(parts)
    return str(system)


def _convert_content_blocks_a2o(
    blocks: list[dict], role: str
) -> tuple[Any, list[dict]]:
    """Convert a list of Anthropic content blocks to OpenAI content + tool_calls.

    Returns ``(content, tool_calls)`` where ``content`` is either a string or a
    list of OpenAI content parts (or None if empty), and ``tool_calls`` is a
    list of OpenAI tool_call objects (empty list if none).
    """
    text_parts: list[str] = []
    parts: list[dict] = []
    # `has_image` triggers list-mode content (multi-modal OpenAI parts).
    # tool_use does NOT trigger list-mode — it goes into tool_calls and the
    # text parts are returned as a concatenated string (per spec §8.3).
    has_image = False
    tool_calls: list[dict] = []

    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
            parts.append({"type": "text", "text": str(block.get("text", ""))})
        elif btype == "image":
            has_image = True
            source = block.get("source") or {}
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                url = f"data:{media_type};base64,{data}"
            else:
                url = source.get("url", "")
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_use" and role == "assistant":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(
                        block.get("input") or {}, ensure_ascii=False
                    ),
                },
            })
        elif btype == "tool_result" and role == "user":
            # tool_result is handled separately by the caller (becomes a
            # dedicated role:"tool" message); skip here.
            continue
        else:
            # Unknown block type — preserve as text if it has text.
            if "text" in block:
                text_parts.append(str(block.get("text", "")))
                parts.append({"type": "text", "text": str(block.get("text", ""))})

    if has_image:
        content: Any = parts if parts else None
    else:
        content = "".join(text_parts) if text_parts else None
    return content, tool_calls


def _convert_tool_results_a2o(blocks: list[dict]) -> list[dict]:
    """Extract tool_result blocks from a user message as OpenAI tool messages."""
    out: list[dict] = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                # Concatenate text parts of the tool_result content.
                parts = []
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(str(sub.get("text", "")))
                    elif isinstance(sub, str):
                        parts.append(sub)
                content = "".join(parts)
            elif not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False) if content is not None else ""
            out.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": content,
            })
    return out


def _convert_tools_a2o(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tools to OpenAI tools."""
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        function: dict[str, Any] = {"name": t.get("name", "")}
        if "description" in t:
            function["description"] = t["description"]
        if "input_schema" in t:
            function["parameters"] = t["input_schema"]
        out.append({"type": "function", "function": function})
    return out


def _convert_tool_choice_a2o(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to OpenAI tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        # Already a string like "auto" — pass through.
        return tool_choice
    if isinstance(tool_choice, dict):
        tctype = tool_choice.get("type")
        if tctype == "auto":
            return "auto"
        if tctype == "any":
            return "required"
        if tctype == "tool":
            name = tool_choice.get("name", "")
            return {"type": "function", "function": {"name": name}}
        if tctype == "none":
            return "none"
    return None


def translate_request_a2o(body: dict, max_tokens_default: int = 4096) -> dict:
    """Translate an Anthropic Messages API request body to OpenAI Chat format.

    Pure function — no side effects beyond logging warnings for dropped fields.
    """
    if not isinstance(body, dict):
        raise TypeError("body must be a dict")

    out: dict[str, Any] = {}

    if "model" in body:
        out["model"] = body["model"]

    # system -> first system message
    messages: list[dict] = []
    system = body.get("system")
    if system is not None:
        sys_text = _system_to_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    # Convert messages
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            # First, extract tool_result blocks as separate role:"tool" messages
            tool_msgs = _convert_tool_results_a2o(content)
            messages.extend(tool_msgs)

            # Then convert remaining blocks (text / image / tool_use)
            oai_content, tool_calls = _convert_content_blocks_a2o(content, role)
            if role == "assistant":
                am: dict[str, Any] = {"role": "assistant"}
                if oai_content is not None:
                    am["content"] = oai_content
                else:
                    am["content"] = None
                if tool_calls:
                    am["tool_calls"] = tool_calls
                # Only append if there is something to send (avoid empty msgs)
                if oai_content is not None or tool_calls:
                    messages.append(am)
            else:  # user
                if oai_content is not None:
                    messages.append({"role": "user", "content": oai_content})
            continue

        # Unknown content type — pass through as None content.
        messages.append({"role": role, "content": None})

    out["messages"] = messages

    # max_tokens (required in Anthropic; OpenAI optional)
    if "max_tokens" in body and body["max_tokens"] is not None:
        out["max_tokens"] = body["max_tokens"]
    else:
        logger.info(
            "translate_request_a2o: max_tokens missing, using default {}",
            max_tokens_default,
        )
        out["max_tokens"] = max_tokens_default

    # temperature / top_p
    if "temperature" in body and body["temperature"] is not None:
        out["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        out["top_p"] = body["top_p"]

    # stop_sequences -> stop
    if "stop_sequences" in body and body["stop_sequences"]:
        out["stop"] = body["stop_sequences"]

    # stream
    if "stream" in body:
        out["stream"] = bool(body["stream"])

    # tools
    if body.get("tools"):
        out["tools"] = _convert_tools_a2o(body["tools"])

    # tool_choice
    if body.get("tool_choice") is not None:
        tc = _convert_tool_choice_a2o(body["tool_choice"])
        if tc is not None:
            out["tool_choice"] = tc

    # metadata.user_id -> user
    meta = body.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        out["user"] = meta["user_id"]

    # Dropped fields — log a warning.
    dropped = []
    if "top_k" in body and body["top_k"] is not None:
        dropped.append("top_k")
    if "cache_control" in body:
        dropped.append("cache_control")
    # Detect anthropic-beta features by scanning content blocks for cache_control
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and "cache_control" in block:
                    if "cache_control" not in dropped:
                        dropped.append("cache_control")
                    break
    if dropped:
        logger.warning(
            "translate_request_a2o: dropped fields with no OpenAI equivalent: {}",
            dropped,
        )

    return out


def translate_response_o2a(resp: dict, model: str = "") -> dict:
    """Translate an OpenAI Chat Completions response to Anthropic format.

    Pure function.
    """
    if not isinstance(resp, dict):
        raise TypeError("resp must be a dict")

    choices = resp.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    content: list[dict] = []
    msg_content = message.get("content")
    if msg_content:
        content.append({"type": "text", "text": str(msg_content)})

    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args_obj = json.loads(args_str) if args_str else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "translate_response_o2a: failed to parse tool_call arguments, "
                "falling back to empty object"
            )
            args_obj = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": args_obj,
        })

    stop_reason = MAP_FINISH_REASON_O2A.get(finish_reason, "end_turn")

    usage_in = resp.get("usage") or {}
    input_tokens = usage_in.get("prompt_tokens", 0) or 0
    output_tokens = usage_in.get("completion_tokens", 0) or 0

    out: dict[str, Any] = {
        "id": resp.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": model or resp.get("model", ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    return out
