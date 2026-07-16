"""OpenAI Chat Completions -> Anthropic Messages API translation.

Pure functions: translate OpenAI requests to Anthropic requests, and Anthropic
non-streaming responses back to OpenAI format.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger


# Anthropic stop_reason -> OpenAI finish_reason
MAP_STOP_REASON_A2O: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}


def _content_to_text(content: Any) -> str:
    """Coerce OpenAI content (string or list of parts) to a single text string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    parts.append(str(part["text"]))
                elif "text" in part:
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


def _convert_content_o2a(content: Any) -> list[dict]:
    """Convert OpenAI message content (string or array) to Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                if isinstance(part, str) and part:
                    blocks.append({"type": "text", "text": part})
                continue
            ptype = part.get("type")
            if ptype == "text":
                txt = part.get("text", "")
                if txt:
                    blocks.append({"type": "text", "text": str(txt)})
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    # data:<mime>;base64,<data>
                    try:
                        header, _, data = url.partition(",")
                        # header looks like "data:image/png;base64"
                        mime_part = header[5:].split(";")[0] if header.startswith("data:") else ""
                        media_type = mime_part or "image/png"
                    except Exception:
                        media_type = "image/png"
                        data = url
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    })
                else:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
        return blocks
    return [{"type": "text", "text": str(content)}]


def _convert_tools_o2a(tools: list[dict]) -> list[dict]:
    """Convert OpenAI tools to Anthropic tools (unwrap the function wrapper)."""
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        # OpenAI shape: {type:"function", function:{name, description, parameters}}
        fn = t.get("function")
        if isinstance(fn, dict):
            entry: dict[str, Any] = {"name": fn.get("name", "")}
            if "description" in fn:
                entry["description"] = fn["description"]
            if "parameters" in fn:
                entry["input_schema"] = fn["parameters"]
            else:
                entry["input_schema"] = {"type": "object", "properties": {}}
            out.append(entry)
        else:
            # Already in Anthropic-ish shape — pass through.
            entry = dict(t)
            if "parameters" in entry and "input_schema" not in entry:
                entry["input_schema"] = entry.pop("parameters")
            out.append(entry)
    return out


def _convert_tool_choice_o2a(tool_choice: Any) -> Any:
    """Convert OpenAI tool_choice to Anthropic tool_choice."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "none":
            logger.warning(
                "translate_request_o2a: OpenAI tool_choice='none' has no Anthropic "
                "equivalent; mapping to {type:auto}"
            )
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            name = (tool_choice.get("function") or {}).get("name", "")
            return {"type": "tool", "name": name}
        if tool_choice.get("type") == "auto":
            return {"type": "auto"}
    return None


def _merge_consecutive_same_role(messages: list[dict]) -> list[dict]:
    """Merge consecutive messages with the same role.

    Anthropic requires strict alternation of user/assistant. We merge by
    concatenating content block lists. ``role:"tool"`` messages are kept
    separate (they become ``role:"user"`` with tool_result blocks before this
    step is applied — handled by caller).
    """
    if not messages:
        return []
    merged: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        # Normalize content to a list of blocks.
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}] if content else []
        elif isinstance(content, list):
            blocks = list(content)
        elif content is None:
            blocks = []
        else:
            blocks = [{"type": "text", "text": str(content)}]

        if merged and merged[-1].get("role") == role:
            merged[-1]["content"].extend(blocks)
        else:
            merged.append({"role": role, "content": list(blocks)})
    # Drop messages with empty content (e.g. empty assistant turns).
    return [m for m in merged if m.get("content")]


def translate_request_o2a(
    body: dict, anthropic_version: str = "2023-06-01"
) -> dict:
    """Translate an OpenAI Chat Completions request body to Anthropic format.

    Pure function — no side effects beyond logging warnings for dropped fields.
    ``anthropic_version`` is informational here; the upstream client sets the
    ``anthropic-version`` header from this value.
    """
    if not isinstance(body, dict):
        raise TypeError("body must be a dict")

    out: dict[str, Any] = {}

    if "model" in body:
        out["model"] = body["model"]

    # Extract system messages -> top-level system field
    system_parts: list[str] = []
    converted_messages: list[dict] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            system_parts.append(_content_to_text(msg.get("content")))
            continue

        if role == "tool":
            # OpenAI tool result -> Anthropic user message with tool_result block
            tool_call_id = msg.get("tool_call_id", "")
            content_text = _content_to_text(msg.get("content"))
            converted_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content_text,
                }],
            })
            continue

        if role == "assistant":
            blocks = _convert_content_o2a(msg.get("content"))
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or "{}"
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "translate_request_o2a: failed to parse tool_call "
                        "arguments, using empty object"
                    )
                    args_obj = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args_obj,
                })
            if blocks:
                converted_messages.append({"role": "assistant", "content": blocks})
            continue

        # user (or any other role treated as user)
        blocks = _convert_content_o2a(msg.get("content"))
        if blocks:
            converted_messages.append({"role": "user", "content": blocks})

    if system_parts:
        system_text = "".join(system_parts)
        if system_text:
            out["system"] = system_text

    # Merge consecutive same-role messages for Anthropic alternation.
    out["messages"] = _merge_consecutive_same_role(converted_messages)

    # max_tokens (required in Anthropic)
    if "max_tokens" in body and body["max_tokens"] is not None:
        out["max_tokens"] = body["max_tokens"]
    else:
        logger.info(
            "translate_request_o2a: max_tokens missing, using default 4096"
        )
        out["max_tokens"] = 4096

    if "temperature" in body and body["temperature"] is not None:
        out["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        out["top_p"] = body["top_p"]

    # stop -> stop_sequences
    if "stop" in body and body["stop"] is not None:
        stop = body["stop"]
        if isinstance(stop, str):
            out["stop_sequences"] = [stop]
        elif isinstance(stop, list):
            out["stop_sequences"] = list(stop)

    if "stream" in body:
        out["stream"] = bool(body["stream"])

    if body.get("tools"):
        out["tools"] = _convert_tools_o2a(body["tools"])

    if body.get("tool_choice") is not None:
        tc = _convert_tool_choice_o2a(body["tool_choice"])
        if tc is not None:
            out["tool_choice"] = tc

    # Dropped fields with no Anthropic equivalent.
    dropped = []
    for field in (
        "n", "presence_penalty", "frequency_penalty", "logprobs",
        "top_logprobs", "seed", "stream_options",
    ):
        if field in body and body[field] is not None:
            dropped.append(field)
    if "response_format" in body and body["response_format"] is not None:
        dropped.append("response_format")
    if dropped:
        logger.warning(
            "translate_request_o2a: dropped fields with no Anthropic equivalent: {}",
            dropped,
        )

    return out


def translate_response_a2o(resp: dict, model: str = "") -> dict:
    """Translate an Anthropic Messages response to OpenAI Chat Completions format.

    Pure function.
    """
    if not isinstance(resp, dict):
        raise TypeError("resp must be a dict")

    content_blocks = resp.get("content") or []
    text_parts: list[str] = []
    tool_uses: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            tool_uses.append(block)

    message: dict[str, Any] = {"role": "assistant"}
    message["content"] = "".join(text_parts) if text_parts else None
    if tool_uses:
        message["tool_calls"] = [
            {
                "id": t.get("id", ""),
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "arguments": json.dumps(
                        t.get("input") or {}, ensure_ascii=False
                    ),
                },
            }
            for t in tool_uses
        ]
    else:
        message["tool_calls"] = None

    stop_reason = resp.get("stop_reason") or "end_turn"
    finish_reason = MAP_STOP_REASON_A2O.get(stop_reason, "stop")

    usage_in = resp.get("usage") or {}
    input_tokens = usage_in.get("input_tokens", 0) or 0
    output_tokens = usage_in.get("output_tokens", 0) or 0

    import time
    created = int(time.time())

    out: dict[str, Any] = {
        "id": resp.get("id", ""),
        "object": "chat.completion",
        "created": created,
        "model": model or resp.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return out
