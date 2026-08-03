"""OpenAI Responses API (Codex) <-> Chat Completions translation.

Faithful Python port of 9router_research's Responses support
(open-sse/translator/.../openai-responses.js, responsesTransformer.js,
streamToJsonConverter.js). zhongzhuan speaks Chat Completions upstream, so a
Responses (Codex) request is converted to Chat Completions, forwarded
upstream, and the response is converted back to the Responses API format
(streaming SSE + non-streaming JSON).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

# ---- OpenAI / Responses API constants (stable string values) ----
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
ROLE_DEVELOPER = "developer"

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

_MAX_CALL_ID_LEN = 64


def _clamp_call_id(call_id: Any) -> str:
    s = call_id if isinstance(call_id, str) else str(call_id or "")
    if len(s) > _MAX_CALL_ID_LEN:
        return s[:_MAX_CALL_ID_LEN]
    return s


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
    pending_reasoning = ""
    pending_reasoning_encrypted = ""

    input_items = normalize_responses_input(body["input"])
    if input_items is None:
        return body

    def extract_reasoning_text(item: dict) -> str:
        if isinstance(item.get("summary"), list):
            txt = "\n".join((s.get("text") or "") for s in item["summary"] if s.get("text"))
            if txt:
                return txt
        if isinstance(item.get("content"), list):
            txt = "\n".join((c.get("text") or "") for c in item["content"] if c.get("text"))
            if txt:
                return txt
        return ""

    def attach_pending_reasoning(msg: dict) -> None:
        nonlocal pending_reasoning, pending_reasoning_encrypted
        if pending_reasoning:
            msg["reasoning_content"] = pending_reasoning
        if pending_reasoning_encrypted:
            msg["encrypted_content"] = pending_reasoning_encrypted
        pending_reasoning = ""
        pending_reasoning_encrypted = ""

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
            msg = {"role": item["role"], "content": content}
            if item["role"] == ROLE_ASSISTANT:
                attach_pending_reasoning(msg)
            else:
                pending_reasoning = ""
                pending_reasoning_encrypted = ""
            result["messages"].append(msg)

        elif item_type == ITEM_FUNCTION_CALL:
            name = item.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                # Nameless tool calls are rejected upstream. Skip before creating
                # an assistant shell, otherwise we emit `tool_calls: []` which is
                # itself invalid for strict upstreams.
                continue
            if current_assistant is None:
                current_assistant = {"role": ROLE_ASSISTANT, "content": None, "tool_calls": []}
                attach_pending_reasoning(current_assistant)
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

        elif item_type == ITEM_REASONING:
            txt = extract_reasoning_text(item)
            if txt:
                pending_reasoning = f"{pending_reasoning}\n{txt}" if pending_reasoning else txt
            enc = item.get("encrypted_content")
            if isinstance(enc, str) and enc:
                pending_reasoning_encrypted = enc
            continue

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
# Streaming: Chat Completions SSE -> Responses API SSE
# ---------------------------------------------------------------------------

_DATA_RE = re.compile(r"^data:\s*(.+)$", re.MULTILINE)


class ResponsesStreamTranslator:
    """Translates a Chat Completions SSE byte stream into OpenAI Responses SSE.

    Implements the same interface as StreamA2O / StreamO2A used by the proxy:
    ``await feed(chunk) -> list[bytes]``, ``done`` (property), ``finish_safely() -> list[bytes]``,
    and ``usage`` (chat-completions style dict for token accounting).
    """

    def __init__(self, model: str = "") -> None:
        self.model = model
        self._seq = 0
        self._response_id = f"resp_{int(time.time() * 1000)}"
        self._created = int(time.time())
        self._started = False
        self._buffer = ""
        self._finished = False
        self._completed_sent = False
        self._done_emitted = False

        self._msg_text_buf: dict[int, str] = {}
        self._msg_item_added: dict[int, bool] = {}
        self._msg_content_added: dict[int, bool] = {}
        self._msg_item_done: dict[int, bool] = {}

        self._reasoning_id = ""
        self._reasoning_index = -1
        self._reasoning_buf = ""
        self._reasoning_part_added = False
        self._reasoning_done = False
        self._in_thinking = False

        self._func_args_buf: dict[int, str] = {}
        self._func_names: dict[int, str] = {}
        self._func_call_ids: dict[int, str] = {}
        self._func_args_done: dict[int, bool] = {}
        self._func_item_done: dict[int, bool] = {}

        self.usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}

    # -- interface --
    @property
    def done(self) -> bool:
        return self._finished

    def finish_safely(self) -> list[bytes]:
        if self._finished:
            return []
        return self._finish()

    async def feed(self, chunk: bytes) -> list[bytes]:
        if self._finished:
            return []
        self._buffer += chunk.decode("utf-8", errors="replace")
        messages = self._buffer.split("\n\n")
        self._buffer = messages.pop() or ""
        out: list[bytes] = []
        for msg in messages:
            if not msg.strip():
                continue
            m = _DATA_RE.search(msg)
            if not m:
                continue
            data_str = m.group(1).strip()
            if data_str == "[DONE]":
                continue
            try:
                parsed = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            out.extend(self._process(parsed))
        return out

    # -- internals --
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: str, data: dict) -> bytes:
        data["sequence_number"] = self._next_seq()
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")

    def _start_reasoning(self, idx: int) -> None:
        if self._reasoning_id:
            return
        self._reasoning_id = f"rs_{self._response_id}_{idx}"
        self._reasoning_index = idx
        self._reasoning_part_added = True
        # Note: emits are collected via the caller; we return them from _process.
        # We store pending emits in self._pending.
        self._pending.append(self._emit("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": idx,
            "item": {"id": self._reasoning_id, "type": "reasoning", "summary": []},
        }))
        self._pending.append(self._emit("response.reasoning_summary_part.added", {
            "type": "response.reasoning_summary_part.added",
            "item_id": self._reasoning_id,
            "output_index": idx,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        }))

    def _emit_reasoning_delta(self, text: str) -> None:
        if not text:
            return
        self._reasoning_buf += text
        self._pending.append(self._emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta",
            "item_id": self._reasoning_id,
            "output_index": self._reasoning_index,
            "summary_index": 0,
            "delta": text,
        }))

    def _close_reasoning(self) -> None:
        if self._reasoning_id and not self._reasoning_done:
            self._reasoning_done = True
            self._pending.append(self._emit("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "item_id": self._reasoning_id,
                "output_index": self._reasoning_index,
                "summary_index": 0,
                "text": self._reasoning_buf,
            }))
            self._pending.append(self._emit("response.reasoning_summary_part.done", {
                "type": "response.reasoning_summary_part.done",
                "item_id": self._reasoning_id,
                "output_index": self._reasoning_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": self._reasoning_buf},
            }))
            self._pending.append(self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": self._reasoning_index,
                "item": {"id": self._reasoning_id, "type": "reasoning",
                         "summary": [{"type": "summary_text", "text": self._reasoning_buf}]},
            }))

    def _close_message(self, idx: int) -> None:
        if self._msg_item_added.get(idx) and not self._msg_item_done.get(idx):
            self._msg_item_done[idx] = True
            full = self._msg_text_buf.get(idx, "")
            msg_id = f"msg_{self._response_id}_{idx}"
            self._pending.append(self._emit("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": msg_id, "output_index": idx, "content_index": 0,
                "text": full, "logprobs": [],
            }))
            self._pending.append(self._emit("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": msg_id, "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": full},
            }))
            self._pending.append(self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": {"id": msg_id, "type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "annotations": [],
                                      "logprobs": [], "text": full}]},
            }))

    def _close_tool_call(self, idx: int) -> None:
        call_id = self._func_call_ids.get(idx)
        if call_id and not self._func_item_done.get(idx):
            args = self._func_args_buf.get(idx) or "{}"
            self._pending.append(self._emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": f"fc_{call_id}", "output_index": idx, "arguments": args,
            }))
            self._pending.append(self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": {"id": f"fc_{call_id}", "type": "function_call", "arguments": args,
                         "call_id": call_id, "name": self._func_names.get(idx, "")},
            }))
            self._func_item_done[idx] = True
            self._func_args_done[idx] = True

    def _send_completed(self) -> None:
        if self._completed_sent:
            return
        self._completed_sent = True
        self._pending.append(self._emit("response.completed", {
            "type": "response.completed",
            "response": {
                "id": self._response_id,
                "object": "response",
                "created_at": self._created,
                "status": "completed",
                "background": False,
                "error": None,
            },
        }))

    def _process(self, parsed: dict) -> list[bytes]:
        self._pending: list[bytes] = []

        # Capture usage from the final usage-only chunk (include_usage).
        u = parsed.get("usage")
        if isinstance(u, dict):
            pt = u.get("prompt_tokens", 0)
            ct = u.get("completion_tokens", 0)
            if pt or ct:
                self.usage = {"prompt_tokens": pt, "completion_tokens": ct}

        choices = parsed.get("choices") or []
        if not choices:
            return self._pending

        choice = choices[0]
        idx = choice.get("index", 0) or 0
        delta = choice.get("delta") or {}

        if not self._started:
            self._started = True
            if parsed.get("id"):
                self._response_id = f"resp_{parsed['id']}"
            self._pending.append(self._emit("response.created", {
                "type": "response.created",
                "response": {
                    "id": self._response_id, "object": "response",
                    "created_at": self._created, "status": "in_progress",
                    "background": False, "error": None, "output": [],
                },
            }))
            self._pending.append(self._emit("response.in_progress", {
                "type": "response.in_progress",
                "response": {"id": self._response_id, "object": "response",
                             "created_at": self._created, "status": "in_progress"},
            }))

        # Reasoning content (native or <think> wrapped)
        if delta.get("reasoning_content"):
            self._start_reasoning(idx)
            self._emit_reasoning_delta(delta["reasoning_content"])

        if delta.get("content"):
            content = delta["content"]
            if "<think>" in content:
                self._in_thinking = True
                content = content.replace("<think>", "")
                self._start_reasoning(idx)
            if "</think>" in content:
                parts = content.split("</think>")
                think_part = parts[0]
                text_part = "</think>".join(parts[1:])
                if think_part:
                    self._emit_reasoning_delta(think_part)
                self._close_reasoning()
                self._in_thinking = False
                content = text_part
            if self._in_thinking and content:
                self._emit_reasoning_delta(content)
            else:
                if content:
                    if not self._msg_item_added.get(idx):
                        self._msg_item_added[idx] = True
                        msg_id = f"msg_{self._response_id}_{idx}"
                        self._pending.append(self._emit("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {"id": msg_id, "type": "message", "content": [], "role": "assistant"},
                        }))
                    if not self._msg_content_added.get(idx):
                        self._msg_content_added[idx] = True
                        msg_id = f"msg_{self._response_id}_{idx}"
                        self._pending.append(self._emit("response.content_part.added", {
                            "type": "response.content_part.added",
                            "item_id": msg_id, "output_index": idx, "content_index": 0,
                            "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                        }))
                    self._pending.append(self._emit("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": f"msg_{self._response_id}_{idx}",
                        "output_index": idx, "content_index": 0,
                        "delta": content, "logprobs": [],
                    }))
                    self._msg_text_buf[idx] = (self._msg_text_buf.get(idx, "") + content)

        if delta.get("tool_calls"):
            self._close_message(idx)
            for tc in delta["tool_calls"]:
                tc_idx = tc.get("index", 0) or 0
                new_call_id = tc.get("id")
                func_name = tc.get("function", {}).get("name")
                if func_name:
                    self._func_names[tc_idx] = func_name
                if not self._func_call_ids.get(tc_idx) and new_call_id:
                    self._func_call_ids[tc_idx] = new_call_id
                    self._pending.append(self._emit("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": tc_idx,
                        "item": {"id": f"fc_{new_call_id}", "type": "function_call",
                                 "arguments": "", "call_id": new_call_id,
                                 "name": self._func_names.get(tc_idx, "")},
                    }))
                if not self._func_args_buf.get(tc_idx):
                    self._func_args_buf[tc_idx] = ""
                if tc.get("function", {}).get("arguments"):
                    ref_call_id = self._func_call_ids.get(tc_idx) or new_call_id
                    if ref_call_id:
                        self._pending.append(self._emit("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": f"fc_{ref_call_id}", "output_index": tc_idx,
                            "delta": tc["function"]["arguments"],
                        }))
                    self._func_args_buf[tc_idx] += tc["function"]["arguments"]

        if choice.get("finish_reason"):
            for i in list(self._msg_item_added.keys()):
                self._close_message(i)
            self._close_reasoning()
            for i in list(self._func_call_ids.keys()):
                self._close_tool_call(i)
            self._send_completed()

        return self._pending

    def _finish(self) -> list[bytes]:
        self._pending = []
        for i in list(self._msg_item_added.keys()):
            self._close_message(i)
        self._close_reasoning()
        for i in list(self._func_call_ids.keys()):
            self._close_tool_call(i)
        self._send_completed()
        out = self._pending
        if not self._done_emitted:
            out.append(b"data: [DONE]\n\n")
            self._done_emitted = True
        self._finished = True
        return out


class CompositeStreamTranslator:
    """Pipes one stream translator's output into another (e.g. Anthropic SSE -> OpenAI SSE -> Responses SSE).

    Implements the same interface as the other stream translators:
    ``feed`` (async), ``done`` (property), ``finish_safely``, ``usage``.
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
        """Finish the pipeline, flushing both translators.

        ``async`` per §13: a synchronous method must never call an async
        ``feed()``.  The first translator is finished, its output is piped into
        the second via ``await second.feed()``, then the second is finished.
        """
        out: list[bytes] = []
        for c in self.first.finish_safely():
            out.extend(await self.second.feed(c))
        out.extend(self.second.finish_safely())
        return out

    async def feed(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        for c in await self.first.feed(chunk):
            out.extend(await self.second.feed(c))
        return out
