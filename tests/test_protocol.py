"""Unit tests for protocol translation modules (OpenAI <-> Anthropic)."""

import json

import pytest

from zhongzhuan.proxy.protocol.detect import detect_inbound_protocol
from zhongzhuan.proxy.protocol.translate_a2o import (
    translate_request_a2o,
    translate_response_o2a,
)
from zhongzhuan.proxy.protocol.translate_o2a import (
    translate_request_o2a,
    translate_response_a2o,
)
from zhongzhuan.proxy.protocol.errors import (
    translate_error_a2o,
    translate_error_o2a,
)
from zhongzhuan.proxy.protocol.stream_o2a import StreamO2A
from zhongzhuan.proxy.protocol.stream_a2o import StreamA2O


# ---------------------------------------------------------------------------
# detect_inbound_protocol
# ---------------------------------------------------------------------------
class TestDetect:
    def test_path_messages_is_anthropic(self):
        assert detect_inbound_protocol("/v1/messages", {}) == "anthropic"

    def test_path_messages_count_tokens_is_anthropic(self):
        assert detect_inbound_protocol("/v1/messages/count_tokens", {}) == "anthropic"

    def test_path_chat_completions_is_openai(self):
        assert detect_inbound_protocol("/v1/chat/completions", {}) == "openai"

    def test_x_api_key_header_triggers_anthropic(self):
        assert detect_inbound_protocol("/v1/chat/completions", {"x-api-key": "sk-x"}) == "anthropic"

    def test_anthropic_version_header_triggers_anthropic(self):
        assert detect_inbound_protocol("/foo", {"anthropic-version": "2023-06-01"}) == "anthropic"

    def test_header_lookup_case_insensitive(self):
        assert detect_inbound_protocol("/foo", {"X-Api-Key": "sk-x"}) == "anthropic"
        assert detect_inbound_protocol("/foo", {"Anthropic-Version": "2023-06-01"}) == "anthropic"

    def test_default_openai(self):
        assert detect_inbound_protocol("/v1/models", {}) == "openai"

    def test_empty_path_openai(self):
        assert detect_inbound_protocol("", {}) == "openai"


# ---------------------------------------------------------------------------
# translate_request_a2o (Anthropic -> OpenAI)
# ---------------------------------------------------------------------------
class TestRequestA2O:
    def test_simple_text_message(self):
        body = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = translate_request_a2o(body)
        assert out["model"] == "claude-sonnet-4-5"
        assert out["max_tokens"] == 1024
        assert out["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_field_becomes_system_message(self):
        body = {
            "model": "claude",
            "max_tokens": 100,
            "system": "You are a bot",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = translate_request_a2o(body)
        assert out["messages"][0] == {"role": "system", "content": "You are a bot"}
        assert out["messages"][1] == {"role": "user", "content": "hi"}

    def test_system_as_block_list(self):
        body = {
            "max_tokens": 100,
            "system": [{"type": "text", "text": "sys1"}, {"type": "text", "text": "sys2"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = translate_request_a2o(body)
        assert out["messages"][0] == {"role": "system", "content": "sys1sys2"}

    def test_max_tokens_default_when_missing(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out = translate_request_a2o(body, max_tokens_default=2048)
        assert out["max_tokens"] == 2048

    def test_content_blocks_text_concatenated(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "World"},
                    ],
                }
            ],
        }
        out = translate_request_a2o(body)
        assert out["messages"][0] == {"role": "user", "content": "Hello World"}

    def test_assistant_tool_use_becomes_tool_calls(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "calling tool"},
                        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}},
                    ],
                }
            ],
        }
        out = translate_request_a2o(body)
        msg = out["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "calling tool"
        assert msg["tool_calls"] == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": json.dumps({"city": "SF"}, ensure_ascii=False),
                },
            }
        ]

    def test_user_tool_result_becomes_tool_message(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny, 22C"},
                    ],
                }
            ],
        }
        out = translate_request_a2o(body)
        assert out["messages"][0] == {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": "sunny, 22C",
        }

    def test_tool_result_with_block_content(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                        }
                    ],
                }
            ],
        }
        out = translate_request_a2o(body)
        assert out["messages"][0]["content"] == "ab"

    def test_image_block_becoves_image_url(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "BASE64DATA",
                            },
                        },
                    ],
                }
            ],
        }
        out = translate_request_a2o(body)
        msg = out["messages"][0]
        assert isinstance(msg["content"], list)
        assert msg["content"][0] == {"type": "text", "text": "what is this?"}
        assert msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,BASE64DATA"},
        }

    def test_tools_translation(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get the weather",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
        }
        out = translate_request_a2o(body)
        assert out["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]

    def test_tool_choice_translation(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        out = translate_request_a2o(body)
        assert out["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}

    def test_tool_choice_any_to_required(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "any"},
        }
        out = translate_request_a2o(body)
        assert out["tool_choice"] == "required"

    def test_stop_sequences_to_stop(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["END", "STOP"],
        }
        out = translate_request_a2o(body)
        assert out["stop"] == ["END", "STOP"]

    def test_metadata_user_id(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"user_id": "user123"},
        }
        out = translate_request_a2o(body)
        assert out["user"] == "user123"

    def test_stream_flag_passed_through(self):
        body = {"max_tokens": 100, "messages": [{"role": "user", "content": "hi"}], "stream": True}
        out = translate_request_a2o(body)
        assert out["stream"] is True


# ---------------------------------------------------------------------------
# translate_response_o2a (OpenAI -> Anthropic)
# ---------------------------------------------------------------------------
class TestResponseO2A:
    def test_simple_text_response(self):
        resp = {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        out = translate_response_o2a(resp, model="claude")
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["model"] == "claude"
        assert out["content"] == [{"type": "text", "text": "Hello!"}]
        assert out["stop_reason"] == "end_turn"
        assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_calls_response(self):
        resp = {
            "id": "chatcmpl-2",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        out = translate_response_o2a(resp, model="claude")
        assert out["content"][0] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "SF"},
        }
        assert out["stop_reason"] == "tool_use"

    def test_finish_reason_length_maps_to_max_tokens(self):
        resp = {
            "id": "x",
            "choices": [{"message": {"role": "assistant", "content": "..."}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        out = translate_response_o2a(resp, model="m")
        assert out["stop_reason"] == "max_tokens"

    def test_text_and_tool_use_in_one_response(self):
        resp = {
            "id": "x",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me check.",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        out = translate_response_o2a(resp, model="m")
        assert len(out["content"]) == 2
        assert out["content"][0] == {"type": "text", "text": "Let me check."}
        assert out["content"][1]["type"] == "tool_use"


# ---------------------------------------------------------------------------
# translate_request_o2a (OpenAI -> Anthropic)
# ---------------------------------------------------------------------------
class TestRequestO2A:
    def test_simple_text_message(self):
        body = {
            "model": "gpt-4o",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = translate_request_o2a(body)
        assert out["model"] == "gpt-4o"
        assert out["max_tokens"] == 500
        assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]

    def test_system_message_extracted(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "hi"},
            ],
        }
        out = translate_request_o2a(body)
        assert out["system"] == "you are helpful"
        assert len(out["messages"]) == 1
        assert out["messages"][0]["role"] == "user"

    def test_max_tokens_default(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out = translate_request_o2a(body)
        assert out["max_tokens"] == 4096

    def test_assistant_tool_calls_become_tool_use(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                        }
                    ],
                }
            ],
        }
        out = translate_request_o2a(body)
        msg = out["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"][0] == {"type": "text", "text": "calling"}
        assert msg["content"][1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "SF"},
        }

    def test_tool_role_becomes_tool_result(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "what's the weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "w", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
            ],
        }
        out = translate_request_o2a(body)
        # Last message should be a user with tool_result block.
        last = out["messages"][-1]
        assert last["role"] == "user"
        assert last["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "c1",
            "content": "sunny",
        }

    def test_stop_to_stop_sequences(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["END"],
        }
        out = translate_request_o2a(body)
        assert out["stop_sequences"] == ["END"]

    def test_stop_string_wraps_in_list(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "stop": "END",
        }
        out = translate_request_o2a(body)
        assert out["stop_sequences"] == ["END"]

    def test_tools_translation(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
        out = translate_request_o2a(body)
        assert out["tools"] == [
            {
                "name": "get_weather",
                "description": "weather",
                "input_schema": {"type": "object"},
            }
        ]

    def test_tool_choice_required_to_any(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "required",
        }
        out = translate_request_o2a(body)
        assert out["tool_choice"] == {"type": "any"}

    def test_tool_choice_function_to_tool(self):
        body = {
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
        out = translate_request_o2a(body)
        assert out["tool_choice"] == {"type": "tool", "name": "get_weather"}

    def test_consecutive_same_role_merged(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "Hello "},
                {"role": "user", "content": "World"},
            ],
        }
        out = translate_request_o2a(body)
        assert len(out["messages"]) == 1
        assert out["messages"][0]["role"] == "user"
        assert out["messages"][0]["content"] == [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "World"},
        ]

    def test_image_url_to_anthropic_image(self):
        body = {
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,XYZ"}},
                    ],
                }
            ],
        }
        out = translate_request_o2a(body)
        msg = out["messages"][0]
        assert msg["content"][0] == {"type": "text", "text": "what is this?"}
        assert msg["content"][1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "XYZ"},
        }


# ---------------------------------------------------------------------------
# translate_response_a2o (Anthropic -> OpenAI)
# ---------------------------------------------------------------------------
class TestResponseA2O:
    def test_simple_text_response(self):
        resp = {
            "id": "msg_1",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        out = translate_response_a2o(resp, model="gpt-4o")
        assert out["object"] == "chat.completion"
        assert out["model"] == "gpt-4o"
        assert out["choices"][0]["message"]["content"] == "Hello!"
        assert out["choices"][0]["message"]["tool_calls"] is None
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_tool_use_response(self):
        resp = {
            "id": "msg_2",
            "model": "claude",
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        out = translate_response_a2o(resp, model="gpt-4o")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "calling"
        assert msg["tool_calls"] == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": json.dumps({"city": "SF"}, ensure_ascii=False),
                },
            }
        ]
        assert out["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------
class TestErrorTranslation:
    def test_a2o_envelope_shape(self):
        status, body = translate_error_a2o(429, "slow down")
        assert status == 429
        assert body == {
            "error": {
                "message": "slow down",
                "type": "rate_limit_error",
                "param": None,
                "code": None,
            }
        }

    def test_a2o_unknown_status_defaults_api_error(self):
        status, body = translate_error_a2o(599, "boom")
        assert status == 599
        assert body["error"]["type"] == "api_error"

    def test_o2a_envelope_shape(self):
        status, body = translate_error_o2a(401, "bad key")
        assert status == 401
        assert body["type"] == "error"
        assert body["error"] == {"type": "authentication_error", "message": "bad key"}

    def test_o2a_overloaded(self):
        status, body = translate_error_o2a(529, "overloaded")
        assert body["error"]["type"] == "overloaded_error"


# ---------------------------------------------------------------------------
# Stream translation: OpenAI -> Anthropic (StreamO2A)
# ---------------------------------------------------------------------------
def _openai_sse_chunk(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode("utf-8")


def _done_chunk() -> bytes:
    return b"data: [DONE]\n\n"


def _parse_anthropic_sse(data: bytes) -> list[tuple[str, dict]]:
    """Parse Anthropic SSE bytes into list of (event_type, data_dict)."""
    events = []
    text = data.decode("utf-8")
    # Events are separated by \n\n
    for raw_event in text.split("\n\n"):
        if not raw_event.strip():
            continue
        etype = None
        edata = None
        for line in raw_event.split("\n"):
            if line.startswith("event:"):
                etype = line[len("event:") :].strip()
            elif line.startswith("data:"):
                edata = json.loads(line[len("data:") :].strip())
        if etype is not None:
            events.append((etype, edata))
    return events


class TestStreamO2A:
    @pytest.mark.asyncio
    async def test_text_stream(self):
        sm = StreamO2A(model="claude")
        chunks = [
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            ),
        ]
        all_out = b""
        for c in chunks:
            out = await sm.feed(c)
            for o in out:
                all_out += o
        assert sm.done()
        events = _parse_anthropic_sse(all_out)
        types = [e[0] for e in events]
        assert "message_start" in types
        assert "content_block_start" in types
        assert "content_block_delta" in types
        assert "message_delta" in types
        assert "message_stop" in types
        # Find the message_delta event and check stop_reason.
        msg_delta = next(e[1] for e in events if e[0] == "message_delta")
        assert msg_delta["delta"]["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_done_marker_only(self):
        sm = StreamO2A(model="claude")
        out = await sm.feed(_done_chunk())
        assert sm.done()
        # Should still emit a message_start (INIT -> STARTED) + finish events.
        all_out = b"".join(out)
        events = _parse_anthropic_sse(all_out)
        types = [e[0] for e in events]
        assert "message_start" in types
        assert "message_stop" in types

    @pytest.mark.asyncio
    async def test_tool_call_stream(self):
        sm = StreamO2A(model="claude")
        chunks = [
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "get_weather", "arguments": ""},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '{"city":"SF"}'},
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            _openai_sse_chunk(
                {
                    "id": "c1",
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
            ),
        ]
        all_out = b""
        for c in chunks:
            for o in await sm.feed(c):
                all_out += o
        assert sm.done()
        events = _parse_anthropic_sse(all_out)
        types = [e[0] for e in events]
        # Should have content_block_start with type tool_use.
        block_starts = [e[1] for e in events if e[0] == "content_block_start"]
        tool_starts = [b for b in block_starts if b.get("content_block", {}).get("type") == "tool_use"]
        assert len(tool_starts) == 1
        assert tool_starts[0]["content_block"]["name"] == "get_weather"
        # message_delta should have stop_reason=tool_use.
        msg_delta = next(e[1] for e in events if e[0] == "message_delta")
        assert msg_delta["delta"]["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_buffering_across_chunks(self):
        """A single SSE event split across feed() calls should be reassembled."""
        sm = StreamO2A(model="claude")
        full = _openai_sse_chunk(
            {
                "id": "c1",
                "model": "gpt-4o",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}],
            }
        )
        # Split into halves
        out1 = await sm.feed(full[: len(full) // 2])
        out2 = await sm.feed(full[len(full) // 2 :])
        all_out = b"".join(out1 + out2)
        assert b"message_start" in all_out
        assert b"content_block_delta" in all_out


# ---------------------------------------------------------------------------
# Stream translation: Anthropic -> OpenAI (StreamA2O)
# ---------------------------------------------------------------------------
def _anthropic_sse_event(event: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _parse_openai_sse(data: bytes) -> list[dict]:
    """Parse OpenAI SSE bytes into list of parsed data dicts (excluding [DONE])."""
    out = []
    text = data.decode("utf-8")
    for raw in text.split("\n\n"):
        for line in raw.split("\n"):
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    continue
                out.append(json.loads(payload))
    return out


class TestStreamA2O:
    @pytest.mark.asyncio
    async def test_text_stream(self):
        sm = StreamA2O(model="gpt-4o")
        chunks = [
            _anthropic_sse_event(
                "message_start",
                {
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            _anthropic_sse_event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _anthropic_sse_event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            ),
            _anthropic_sse_event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                },
            ),
            _anthropic_sse_event("content_block_stop", {"index": 0}),
            _anthropic_sse_event(
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
            ),
            _anthropic_sse_event("message_stop", {}),
        ]
        all_out = b""
        for c in chunks:
            for o in await sm.feed(c):
                all_out += o
        assert sm.done()
        parsed = _parse_openai_sse(all_out)
        # First chunk should have role:assistant.
        assert parsed[0]["choices"][0]["delta"]["role"] == "assistant"
        # Text content concatenated.
        text_content = "".join(
            c["choices"][0]["delta"].get("content", "") for c in parsed if c["choices"][0]["delta"].get("content")
        )
        assert text_content == "Hello world"
        # Last chunk should have finish_reason=stop.
        assert parsed[-1]["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_tool_use_stream(self):
        sm = StreamA2O(model="gpt-4o")
        chunks = [
            _anthropic_sse_event(
                "message_start",
                {
                    "message": {
                        "id": "m1",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
            ),
            _anthropic_sse_event(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
                },
            ),
            _anthropic_sse_event(
                "content_block_delta",
                {
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":"SF"}'},
                },
            ),
            _anthropic_sse_event("content_block_stop", {"index": 0}),
            _anthropic_sse_event(
                "message_delta",
                {
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
            ),
            _anthropic_sse_event("message_stop", {}),
        ]
        all_out = b""
        for c in chunks:
            for o in await sm.feed(c):
                all_out += o
        parsed = _parse_openai_sse(all_out)
        # Should contain tool_calls delta.
        tool_chunks = [c for c in parsed if c["choices"][0]["delta"].get("tool_calls")]
        assert tool_chunks
        first_tc = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert first_tc["function"]["name"] == "get_weather"
        # Final finish_reason should be tool_calls.
        assert parsed[-1]["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_buffering_across_chunks(self):
        sm = StreamA2O(model="gpt-4o")
        full = _anthropic_sse_event(
            "message_start",
            {
                "message": {
                    "id": "m1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        out1 = await sm.feed(full[: len(full) // 2])
        out2 = await sm.feed(full[len(full) // 2 :])
        all_out = b"".join(out1 + out2)
        assert b"role" in all_out
        assert b"assistant" in all_out
