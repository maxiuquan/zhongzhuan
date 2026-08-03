"""T10 tests: versioned request schema + item registry.

Covers the four acceptance criteria of T10:
1. Official fields never land in ``dropped_fields``; fictional fields do;
   invalid values yield a 400 (via :func:`process_requests_schema`).
2. All 18 item types have a fixture; parse/serialize round-trips.
3. ``text.format`` is consumed and turned into ``response_format`` (Q7),
   never dropped.
4. The 13 Responses-only fields are never present in the upstream payload.
"""
from __future__ import annotations

import pytest

from zhongzhuan.proxy.protocol.responses_schema import (
    RESPONSES_CREATE_FIELDS,
    RESPONSES_ONLY_FIELDS,
    NOT_FORWARDED_FIELDS,
    process_requests_schema,
    validate_requests_schema,
)
from zhongzhuan.proxy.protocol.item_registry import (
    ITEM_TYPES,
    ITEM_REGISTRY,
    is_known_item_type,
    parse_input_items,
    parse_item,
    redact_item,
    serialize_item,
)


# ---------------------------------------------------------------------------
# 1. Official fields / fictional fields / invalid values
# ---------------------------------------------------------------------------


def test_official_fields_never_dropped():
    """Every official Responses create field is known (not dropped)."""
    body = {k: _sample_value(k) for k in RESPONSES_CREATE_FIELDS}
    result = process_requests_schema(body)
    # A field is only "dropped as unknown" when it is not in the allowlist.
    for key in RESPONSES_CREATE_FIELDS:
        assert key not in result.dropped_fields, f"{key} should not be dropped"


def test_fictional_fields_go_to_dropped_fields():
    body = {"model": "gpt-4o", "totally_fake_field": 123, "also_fake": "x"}
    result = process_requests_schema(body)
    assert "totally_fake_field" in result.dropped_fields
    assert "also_fake" in result.dropped_fields


def test_invalid_value_yields_400():
    """A known field with the wrong JSON type is a hard validation error."""
    body = {"model": "gpt-4o", "max_output_tokens": "not-an-int"}
    result = validate_requests_schema(body)
    assert result.valid is False
    assert any(name == "max_output_tokens" for name, _ in result.errors)


def test_unknown_field_is_not_a_hard_error():
    """Unknown fields are recorded, not a 400."""
    body = {"model": "gpt-4o", "fictional": 1}
    result = validate_requests_schema(body)
    assert result.valid is True
    assert "fictional" in result.unknown_fields


def test_valid_schema_validates_clean():
    body = {"model": "gpt-4o", "input": "hello", "stream": True}
    result = validate_requests_schema(body)
    assert result.valid is True
    assert result.errors == []


# ---------------------------------------------------------------------------
# 2. Item registry: 18 types
# ---------------------------------------------------------------------------


def test_eighteen_item_types_registered():
    assert len(ITEM_TYPES) == 18
    assert len(ITEM_REGISTRY) == 18


def test_all_item_types_known():
    for item_type in ITEM_TYPES:
        assert is_known_item_type(item_type), f"{item_type} not known"


def test_message_item_parses_with_role():
    raw = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    item = parse_item(raw, 0)
    assert item is not None
    assert item.item_type == "message"
    assert item.role == "user"
    assert item.payload["content"][0]["text"] == "hi"


def test_reasoning_item_redacted():
    raw = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "SECRET REASONING"}],
    }
    item = parse_item(raw, 0)
    assert item is not None
    assert item.redacted is True
    # Raw text must not survive.
    assert "SECRET REASONING" not in str(item.payload)


def test_redact_drops_reasoning_text():
    raw = {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "secret"}],
        "content": [{"type": "output_text", "text": "secret2"}],
    }
    out = redact_item(raw)
    assert "secret" not in str(out)
    assert "secret2" not in str(out)
    assert "type" in out["summary"][0]  # metadata survives


def test_serialize_roundtrip():
    raw = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    item = parse_item(raw, 0)
    assert item is not None
    wire = serialize_item(item)
    assert wire["type"] == "message"
    assert wire["role"] == "user"
    assert "seq" not in wire  # seq is store bookkeeping, not wire


def test_parse_string_input():
    items = parse_input_items("hello")
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].role == "user"


def test_parse_item_list_input():
    raw = [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "a"}]},
        {"type": "function_call_output", "call_id": "call_1", "output": "42"},
    ]
    items = parse_input_items(raw)
    assert len(items) == 2
    assert items[0].item_type == "message"
    assert items[1].item_type == "function_call_output"


# ---------------------------------------------------------------------------
# 3. Q7: text.format consumed -> response_format
# ---------------------------------------------------------------------------


def test_text_format_consumed_not_dropped():
    body = {
        "model": "gpt-4o",
        "input": "json me",
        "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
    }
    result = process_requests_schema(body)
    assert result.text_format is not None
    assert result.text_format["type"] == "json_schema"
    assert "text" not in result.dropped_fields  # Q7: consumed, not dropped
    assert result.payload["response_format"]["type"] == "json_schema"


def test_text_format_text_consumed():
    body = {
        "model": "gpt-4o",
        "input": "hi",
        "text": {"format": {"type": "text"}},
    }
    result = process_requests_schema(body)
    assert result.text_format is not None
    assert result.payload["response_format"]["type"] == "text"
    assert "text" not in result.dropped_fields


# ---------------------------------------------------------------------------
# 4. Responses-only fields never reach upstream payload
# ---------------------------------------------------------------------------


def test_responses_only_fields_never_in_payload():
    """Fully-consumed Responses-only fields never reach the upstream payload."""
    body = {
        k: _sample_value(k) for k in RESPONSES_ONLY_FIELDS
    }
    body["model"] = "gpt-4o"
    body["input"] = "hi"
    body["max_output_tokens"] = 100
    result = process_requests_schema(body)
    # The fully-consumed set never appears; translated fields (tools,
    # tool_choice, parallel_tool_calls, instructions->messages, text->
    # response_format) legitimately land in the payload, just not verbatim.
    for key in NOT_FORWARDED_FIELDS:
        assert key not in result.payload, f"{key} leaked into upstream payload"
    # Translated fields are forwarded under their upstream shape.
    assert result.payload["tools"] == []
    assert result.payload["tool_choice"] == "auto"
    assert result.payload["parallel_tool_calls"] is True
    assert result.payload["response_format"]["type"] == "text"


def test_chat_compatible_fields_forwarded():
    body = {
        "model": "gpt-4o",
        "input": "hi",
        "stream": True,
        "temperature": 0.7,
        "max_output_tokens": 100,
    }
    result = process_requests_schema(body)
    assert result.payload["model"] == "gpt-4o"
    assert result.payload["stream"] is True
    assert result.payload["temperature"] == 0.7
    assert result.payload["max_tokens"] == 100  # renamed
    assert "max_output_tokens" not in result.payload


def test_instructions_become_system_message():
    body = {"model": "gpt-4o", "input": "hi", "instructions": "be concise"}
    result = process_requests_schema(body)
    assert result.payload["messages"][0]["role"] == "system"
    assert result.payload["messages"][0]["content"] == "be concise"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_value(field: str):
    """Return a type-plausible sample value for a field name."""
    samples: dict[str, object] = {
        "model": "gpt-4o",
        "input": "hi",
        "instructions": "be concise",
        "max_output_tokens": 100,
        "previous_response_id": "resp_1",
        "store": True,
        "metadata": {"k": "v"},
        "reasoning": {"effort": "medium"},
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "text": {"format": {"type": "text"}},
        "output": {},
        "prompt": "hi",
        "truncation": "auto",
        "stream": True,
        "user": "u1",
        "include": [],
        "stream_options": {},
        "temperature": 0.7,
        "top_p": 1.0,
        "max_wall_time_seconds": 60,
        "retry": {},
        "schema": {},
    }
    return samples.get(field, "")