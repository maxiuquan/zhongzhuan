"""Unit tests for the OpenAI Responses API (Codex) protocol support.

Covers:
  * inbound protocol detection for ``/v1/responses``
  * Responses request -> Chat Completions request conversion
  * Chat Completions response -> Responses response conversion (non-streaming)
  * Chat Completions SSE -> Responses SSE streaming translation
"""
import json
import re

import pytest

from zhongzhuan.proxy.protocol.detect import detect_inbound_protocol
from zhongzhuan.proxy.protocol.responses import (
    CompositeStreamTranslator,
    ResponsesStreamTranslator,
    chatcompletions_to_responses,
    convert_responses_request_to_chatcompletions,
    normalize_responses_input,
)


# ---------------------------------------------------------------------------
# detect_inbound_protocol
# ---------------------------------------------------------------------------
class TestDetectResponses:
    def test_v1_responses_is_responses(self):
        assert detect_inbound_protocol("/v1/responses", {}) == "responses"

    def test_v1_responses_subpath_is_responses(self):
        assert detect_inbound_protocol("/v1/responses/resp_123", {}) == "responses"

    def test_responses_path_wins_over_anthropic_headers(self):
        # Path has higher priority than headers.
        assert detect_inbound_protocol("/v1/responses", {"x-api-key": "sk-x"}) == "responses"

    def test_chat_completions_still_openai(self):
        assert detect_inbound_protocol("/v1/chat/completions", {}) == "openai"

    def test_messages_still_anthropic(self):
        assert detect_inbound_protocol("/v1/messages", {}) == "anthropic"


# ---------------------------------------------------------------------------
# normalize_responses_input
# ---------------------------------------------------------------------------
class TestNormalizeInput:
    def test_string_input_becomes_user_message(self):
        items = normalize_responses_input("hello")
        assert items == [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }]

    def test_list_input_passthrough(self):
        src = [{"type": "message", "role": "user", "content": "hi"}]
        assert normalize_responses_input(src) == src

    def test_invalid_input_returns_none(self):
        assert normalize_responses_input(123) is None


# ---------------------------------------------------------------------------
# Request conversion: Responses -> Chat Completions
# ---------------------------------------------------------------------------
class TestRequestConversion:
    @staticmethod
    def _codex_request():
        return {
            "model": "gpt-4o",
            "instructions": "You are a coding agent.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "List files"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "shell",
                    "arguments": '{"cmd":"ls"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_abc",
                    "output": "file1.txt\nfile2.txt",
                },
                {"type": "message", "role": "user", "content": "Now edit"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "run",
                    "parameters": {"type": "object"},
                },
                # Hosted tool without a name -> must be dropped.
                {"type": "request_user_input"},
            ],
            "max_output_tokens": 1024,
            "stream": True,
        }

    def test_responses_only_fields_are_stripped(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        for field in ("input", "instructions", "max_output_tokens", "include", "store"):
            assert field not in cc

    def test_instructions_become_system_message(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        assert cc["messages"][0] == {"role": "system", "content": "You are a coding agent."}

    def test_max_output_tokens_maps_to_max_tokens(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        assert cc["max_tokens"] == 1024

    def test_function_call_becomes_assistant_tool_calls(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        assistant = cc["messages"][2]
        assert assistant["role"] == "assistant"
        assert assistant["content"] is None
        assert assistant["tool_calls"][0]["id"] == "call_abc"
        assert assistant["tool_calls"][0]["function"]["name"] == "shell"

    def test_function_call_output_becomes_tool_message(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        tool_msg = cc["messages"][3]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_abc"
        assert "file1.txt" in tool_msg["content"]

    def test_input_text_block_converted(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        user_msg = cc["messages"][1]
        assert user_msg["role"] == "user"
        assert user_msg["content"][0]["type"] == "text"
        assert user_msg["content"][0]["text"] == "List files"

    def test_hosted_tools_dropped_and_parameters_normalized(self):
        cc = convert_responses_request_to_chatcompletions(self._codex_request())
        assert len(cc["tools"]) == 1
        fn = cc["tools"][0]["function"]
        assert fn["name"] == "shell"
        # Missing "properties" must be filled in, otherwise strict upstreams 400.
        assert fn["parameters"] == {"type": "object", "properties": {}}

    def test_nameless_function_call_skipped(self):
        body = {
            "model": "m",
            "input": [
                {"type": "function_call", "call_id": "c1", "name": "", "arguments": "{}"},
                {"type": "message", "role": "user", "content": "hi"},
            ],
        }
        cc = convert_responses_request_to_chatcompletions(body)
        assert all("tool_calls" not in m for m in cc["messages"])

    def test_reasoning_effort_extracted(self):
        body = {
            "model": "m",
            "input": "hi",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        cc = convert_responses_request_to_chatcompletions(body)
        assert cc["reasoning_effort"] == "high"
        assert "reasoning" not in cc

    def test_reasoning_item_not_replayed_into_upstream(self):
        """Reasoning is out-only (R-P0-14 铁律 1): never replayed upstream.

        v3 removed the old ``:115-140`` reasoning replay (attaching the
        reasoning text to the assistant message as ``reasoning_content``).
        A reasoning input item is dropped and must not leak into the Chat
        Completions body.
        """
        body = {
            "model": "m",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
                {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            ],
        }
        cc = convert_responses_request_to_chatcompletions(body)
        for m in cc["messages"]:
            assert "reasoning_content" not in m
            assert "encrypted_content" not in m

    def test_non_responses_body_passthrough(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        assert convert_responses_request_to_chatcompletions(body) is body


# ---------------------------------------------------------------------------
# Non-streaming response conversion: Chat Completions -> Responses
# ---------------------------------------------------------------------------
class TestResponseConversion:
    def test_text_response(self):
        cc = {
            "id": "chatcmpl-1",
            "created": 1700000000,
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        out = chatcompletions_to_responses(cc, "gpt-4o")
        assert out["object"] == "response"
        assert out["status"] == "completed"
        assert out["model"] == "gpt-4o"
        assert out["output"][0]["type"] == "message"
        assert out["output"][0]["content"][0]["type"] == "output_text"
        assert out["output"][0]["content"][0]["text"] == "done"

    def test_usage_renamed_to_responses_shape(self):
        cc = {
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        out = chatcompletions_to_responses(cc)
        assert out["usage"] == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}

    def test_tool_call_response(self):
        cc = {
            "id": "chatcmpl-2",
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"a":1}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
        out = chatcompletions_to_responses(cc, "gpt-4o")
        assert len(out["output"]) == 1
        item = out["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_x"
        assert item["name"] == "shell"
        assert item["arguments"] == '{"a":1}'

    def test_non_dict_passthrough(self):
        assert chatcompletions_to_responses("oops") == "oops"


# ---------------------------------------------------------------------------
# Streaming: Chat Completions SSE -> Responses SSE
# ---------------------------------------------------------------------------
def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


async def _run_stream(chunks: list[bytes], model: str = "gpt-4o"):
    tr = ResponsesStreamTranslator(model=model)
    out: list[bytes] = []
    for c in chunks:
        out.extend(await tr.feed(c))
    out.extend(tr.finish_safely())
    return tr, b"".join(out).decode()


def _event_names(text: str) -> list[str]:
    return [ln.split("event:", 1)[1].strip() for ln in text.splitlines() if ln.startswith("event:")]


class TestStreaming:
    async def test_text_stream_lifecycle(self):
        chunks = [
            _sse({"id": "c1", "model": "gpt-4o",
                  "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
            _sse({"id": "c1", "model": "gpt-4o",
                  "choices": [{"index": 0, "delta": {"content": "Hello"}}]}),
            _sse({"id": "c1", "model": "gpt-4o",
                  "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": "stop"}]}),
            _sse({"id": "c1", "model": "gpt-4o", "choices": [],
                  "usage": {"prompt_tokens": 5, "completion_tokens": 3}}),
            b"data: [DONE]\n\n",
        ]
        tr, text = await _run_stream(chunks)
        names = _event_names(text)
        for expected in (
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ):
            assert expected in names, f"missing event {expected}: {names}"
        # Codex waits for the sentinel; without it the client hangs.
        assert text.rstrip().endswith("data: [DONE]")

    async def test_text_deltas_accumulate(self):
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hello"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": " world"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run_stream(chunks)
        deltas = [json.loads(m)["delta"]
                  for m in re.findall(r"data: (\{.*?\})\n", text, re.S)
                  if '"response.output_text.delta"' in m]
        assert "".join(deltas) == "Hello world"

    async def test_usage_captured_for_billing(self):
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "hi"},
                                           "finish_reason": "stop"}]}),
            _sse({"id": "c1", "choices": [],
                  "usage": {"prompt_tokens": 5, "completion_tokens": 3}}),
            b"data: [DONE]\n\n",
        ]
        tr, _ = await _run_stream(chunks)
        assert tr.usage.get("prompt_tokens") == 5
        assert tr.usage.get("completion_tokens") == 3

    async def test_all_emitted_events_are_valid_json(self):
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "b"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run_stream(chunks)
        payloads = re.findall(r"^data: (.+)$", text, re.MULTILINE)
        for p in payloads:
            if p.strip() == "[DONE]":
                continue
            json.loads(p)  # must not raise

    async def test_tool_call_stream(self):
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "shell", "arguments": ""}}]}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"cmd":'}}]}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"ls"}'}}]},
                "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run_stream(chunks)
        names = _event_names(text)
        assert "response.function_call_arguments.delta" in names
        assert "response.function_call_arguments.done" in names
        done_payload = [json.loads(m) for m in re.findall(r"data: (\{.*?\})\n", text, re.S)
                        if '"response.function_call_arguments.done"' in m][0]
        assert done_payload["arguments"] == '{"cmd":"ls"}'

    async def test_finish_safely_is_idempotent(self):
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "x"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        tr, text = await _run_stream(chunks)
        assert tr.done is True
        # A second call must not emit a duplicate terminator.
        assert tr.finish_safely() == []
        assert text.count("data: [DONE]") == 1

    async def test_truncated_stream_still_terminated(self):
        """Upstream dies mid-stream: we must still close out the Responses stream."""
        tr = ResponsesStreamTranslator(model="gpt-4o")
        out = await tr.feed(_sse({"id": "c1", "choices": [{"index": 0,
                                                           "delta": {"content": "partial"}}]}))
        out.extend(tr.finish_safely())
        text = b"".join(out).decode()
        assert "response.completed" in _event_names(text)
        assert text.rstrip().endswith("data: [DONE]")

    async def test_responses_input_yields_sticky_session_key(self):
        """Codex sends the conversation in `input`, not `messages`.

        Without this the sticky-session fingerprint is empty and multi-turn
        Codex conversations get scattered across different upstream keys.
        """
        from zhongzhuan.proxy.handler import ProxyHandler

        class _Req:
            headers: dict = {}

        body = {
            "model": "gpt-4o",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {"type": "message", "role": "assistant", "content": "hello"},
            ],
        }
        key = ProxyHandler._session_key(_Req(), body)
        assert key.startswith("conv:")
        # Same conversation -> same key (stable routing).
        assert key == ProxyHandler._session_key(_Req(), body)

    async def test_split_chunk_boundaries(self):
        """SSE frames arriving split across TCP chunks must be buffered correctly."""
        full = _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hello"},
                                              "finish_reason": "stop"}]})
        tr = ResponsesStreamTranslator(model="gpt-4o")
        out = await tr.feed(full[:20])
        out.extend(await tr.feed(full[20:]))
        out.extend(await tr.feed(b"data: [DONE]\n\n"))
        out.extend(tr.finish_safely())
        text = b"".join(out).decode()
        assert "response.output_text.delta" in _event_names(text)
        assert "Hello" in text


# ---------------------------------------------------------------------------
# CompositeStreamTranslator async finish_safely (T13)
# ---------------------------------------------------------------------------
class TestCompositeFinishAsync:
    async def test_composite_finish_safely_is_awaitable(self):
        """Composite.finish_safely is async and pipes Anthropic->Responses."""
        from zhongzhuan.proxy.protocol.stream_a2o import StreamA2O

        # Anthropic SSE chunk -> StreamA2O -> Chat Completions SSE -> Responses.
        anthropic_chunk = (
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        )
        composite = CompositeStreamTranslator(
            StreamA2O(model="claude-3-5"), ResponsesStreamTranslator(model="gpt-4o")
        )
        out = await composite.feed(anthropic_chunk)
        closing = await composite.finish_safely()
        all_bytes = b"".join(out + closing).decode()
        assert "response.output_text.delta" in _event_names(all_bytes)
        assert "response.completed" in _event_names(all_bytes)

    async def test_composite_finish_safely_finalizes_second_only(self):
        """First translator already finished; second must still complete."""
        from zhongzhuan.proxy.protocol.stream_a2o import StreamA2O

        first = StreamA2O(model="claude-3-5")
        # Feed and finish the first so it is marked done.
        first.finish_safely()
        second = ResponsesStreamTranslator(model="gpt-4o")
        composite = CompositeStreamTranslator(first, second)
        closing = await composite.finish_safely()
        text = b"".join(closing).decode()
        assert "response.completed" in _event_names(text)
        assert "[DONE]" in text
