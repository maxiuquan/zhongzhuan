"""T09 unit tests: the v3 shared model layer (enums / dataclasses / constants).

Covers acceptance criterion (2) of T09 -- the terminal-reason enum is complete
-- plus signature-stability guards for every symbol later tasks import.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from zhongzhuan.proxy.protocol.responses_models import (
    ALLOWED_TRANSITIONS,
    CANONICAL_JSON_KWARGS,
    CHAT_BASE_ALLOWED,
    CIRCUIT_BREAKER_REASONS,
    DEFAULT_PROVIDER,
    HOSTED_TOOL_CAPABILITY,
    HOSTED_TOOL_ITEM_TYPES,
    INCOMPLETE_REASONS,
    PROVIDER_CAPABILITIES,
    RESPONSE_STATUS_TO_EMITTER_STATE,
    SSE_DONE_FRAME,
    SSE_HEARTBEAT_FRAME,
    TERMINAL_EMITTER_STATES,
    TERMINAL_RESPONSE_STATUSES,
    TIMEOUT_REASONS,
    Capability,
    DeltaKind,
    EmitterState,
    ErrorClass,
    ExecutionMode,
    HostedToolSpec,
    InboundProtocol,
    ItemStatus,
    ItemType,
    NormalizedItem,
    OutputItem,
    ReasoningEventMode,
    ResponsesEndpoint,
    ResponseStatus,
    SanitizedRequest,
    TerminalReason,
    canonical_json,
    coerce_enum,
    enum_values,
    freeze_mapping,
    is_legal_transition,
    make_function_call_item_id,
    make_message_item_id,
    make_reasoning_item_id,
    make_synthetic_call_id,
    resolve_provider_allowlist,
    transition_label,
)

# ---------------------------------------------------------------------------
# str-enum contract
# ---------------------------------------------------------------------------

ALL_ENUMS = [
    InboundProtocol,
    ResponsesEndpoint,
    ResponseStatus,
    ExecutionMode,
    ReasoningEventMode,
    EmitterState,
    DeltaKind,
    Capability,
    ErrorClass,
    TerminalReason,
    ItemType,
    ItemStatus,
]


@pytest.mark.parametrize("enum_cls", ALL_ENUMS)
def test_every_enum_is_a_str_subclass(enum_cls):
    """``x == "raw"`` must hold so string call sites keep working."""
    assert issubclass(enum_cls, str)
    for member in enum_cls:
        assert member == member.value
        assert isinstance(member.value, str)


@pytest.mark.parametrize("enum_cls", ALL_ENUMS)
def test_enum_values_are_unique(enum_cls):
    values = [m.value for m in enum_cls]
    assert len(values) == len(set(values))


def test_inbound_protocol_matches_detect_return_values():
    """detect.py still returns bare strings; comparison must stay true."""
    assert InboundProtocol.OPENAI == "openai"
    assert InboundProtocol.ANTHROPIC == "anthropic"
    assert InboundProtocol.RESPONSES == "responses"
    assert enum_values(InboundProtocol) == {"openai", "anthropic", "responses"}


def test_responses_endpoint_has_six_members():
    assert enum_values(ResponsesEndpoint) == {
        "create",
        "retrieve",
        "delete",
        "cancel",
        "compact",
        "input_items",
    }


def test_response_status_and_terminal_set():
    assert enum_values(ResponseStatus) == {
        "queued",
        "in_progress",
        "completed",
        "failed",
        "incomplete",
        "cancelled",
    }
    assert TERMINAL_RESPONSE_STATUSES == frozenset(
        {
            ResponseStatus.COMPLETED,
            ResponseStatus.FAILED,
            ResponseStatus.INCOMPLETE,
            ResponseStatus.CANCELLED,
        }
    )


def test_execution_mode_three_members():
    assert enum_values(ExecutionMode) == {"native", "emulate", "translate"}


# ---------------------------------------------------------------------------
# T09 acceptance criterion (2): TerminalReason completeness
# ---------------------------------------------------------------------------

#: R-P0-32 / §9.4 verbatim.
EXPECTED_CIRCUIT_BREAKER_REASONS = {
    "max_tool_rounds",
    "max_total_tool_calls",
    "repeated_tool_call",
    "repeated_tool_failure",
    "max_response_time",
    "max_output_budget",
    "response_chain_cycle",
    "response_chain_too_deep",
    "retry_budget_exhausted",
    "background_budget_exhausted",
}


def test_ten_circuit_breaker_terminal_reasons_are_complete():
    """T09 criterion (2): exactly the ten R-P0-32 circuit-breaker reasons."""
    assert len(CIRCUIT_BREAKER_REASONS) == 10
    assert {r.value for r in CIRCUIT_BREAKER_REASONS} == EXPECTED_CIRCUIT_BREAKER_REASONS
    for value in EXPECTED_CIRCUIT_BREAKER_REASONS:
        assert TerminalReason(value) in CIRCUIT_BREAKER_REASONS


def test_terminal_reason_full_value_set():
    """Full enum: 1 normal + 10 circuit breaker + 5 other + 3 timeout."""
    assert enum_values(TerminalReason) == {
        "normal_finish",
        # ten circuit breakers
        "max_tool_rounds",
        "max_total_tool_calls",
        "repeated_tool_call",
        "repeated_tool_failure",
        "max_response_time",
        "max_output_budget",
        "response_chain_cycle",
        "response_chain_too_deep",
        "retry_budget_exhausted",
        "background_budget_exhausted",
        # non circuit-breaker terminations
        "upstream_truncated",
        "capability_route_unavailable",
        "client_disconnected",
        "upstream_error",
        "cancelled_by_client",
        # timeout classification (deviation, see module docstring)
        "upstream_connect",
        "first_token_timeout",
        "read_idle_timeout",
    }
    assert len(list(TerminalReason)) == 19


def test_four_timeout_reasons_are_mutually_distinct():
    """R-P1-26 / T28-4: connect / first-token / read-idle / total differ."""
    assert len(TIMEOUT_REASONS) == 4
    assert {r.value for r in TIMEOUT_REASONS} == {
        "upstream_connect",
        "first_token_timeout",
        "read_idle_timeout",
        "max_response_time",
    }


def test_incomplete_reasons_superset_of_circuit_breakers():
    assert CIRCUIT_BREAKER_REASONS <= INCOMPLETE_REASONS
    assert TerminalReason.UPSTREAM_TRUNCATED in INCOMPLETE_REASONS
    assert TerminalReason.NORMAL_FINISH not in INCOMPLETE_REASONS


# ---------------------------------------------------------------------------
# Emitter state machine (§4.1)
# ---------------------------------------------------------------------------


def test_allowed_transitions_cover_every_state():
    assert set(ALLOWED_TRANSITIONS) == set(EmitterState)


@pytest.mark.parametrize(
    "src,dst",
    [
        (EmitterState.INIT, EmitterState.QUEUED),
        (EmitterState.INIT, EmitterState.CREATED),
        (EmitterState.QUEUED, EmitterState.CREATED),
        (EmitterState.CREATED, EmitterState.IN_PROGRESS),
        (EmitterState.IN_PROGRESS, EmitterState.STREAMING),
        (EmitterState.IN_PROGRESS, EmitterState.COMPLETING),
        (EmitterState.STREAMING, EmitterState.STREAMING),
        (EmitterState.STREAMING, EmitterState.COMPLETING),
        (EmitterState.COMPLETING, EmitterState.COMPLETED),
    ],
)
def test_documented_legal_transitions(src, dst):
    """Every row of the §4.1 table must be accepted."""
    assert is_legal_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        (EmitterState.INIT, EmitterState.COMPLETED),
        (EmitterState.INIT, EmitterState.STREAMING),
        (EmitterState.QUEUED, EmitterState.STREAMING),
        (EmitterState.CREATED, EmitterState.COMPLETED),
        (EmitterState.COMPLETED, EmitterState.STREAMING),
        (EmitterState.COMPLETED, EmitterState.COMPLETING),
    ],
)
def test_documented_illegal_transitions(src, dst):
    """The five rejection rows of §4.1 must be refused."""
    assert not is_legal_transition(src, dst)


def test_terminal_states_have_no_outgoing_edges():
    for state in TERMINAL_EMITTER_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_response_status_maps_onto_emitter_state():
    assert set(RESPONSE_STATUS_TO_EMITTER_STATE) == set(ResponseStatus)
    for status, state in RESPONSE_STATUS_TO_EMITTER_STATE.items():
        assert status.value == state.value
    for status in TERMINAL_RESPONSE_STATUSES:
        assert RESPONSE_STATUS_TO_EMITTER_STATE[status] in TERMINAL_EMITTER_STATES


def test_transition_label_format():
    assert transition_label(EmitterState.INIT, EmitterState.COMPLETED) == "init->completed"


# ---------------------------------------------------------------------------
# Capabilities (B6) / delta kinds / item types
# ---------------------------------------------------------------------------


def test_capability_has_nine_members_including_tool_search():
    assert len(list(Capability)) == 9
    assert enum_values(Capability) == {
        "stateful_responses",
        "background",
        "web_search",
        "file_search",
        "computer",
        "code_interpreter",
        "image_generation",
        "remote_mcp",
        "tool_search",
    }
    assert Capability.TOOL_SEARCH == "tool_search"


def test_hosted_tool_capability_values_are_capabilities():
    for tool_type, cap in HOSTED_TOOL_CAPABILITY.items():
        assert isinstance(tool_type, str) and tool_type
        assert isinstance(cap, Capability)
    # every tool-backed capability is reachable from at least one tool type
    reachable = set(HOSTED_TOOL_CAPABILITY.values())
    assert Capability.REMOTE_MCP in reachable
    assert Capability.TOOL_SEARCH in reachable


def test_delta_kind_contains_the_five_documented_families():
    assert {
        "output_text",
        "refusal",
        "reasoning_summary_text",
        "reasoning_text",
        "function_call_arguments",
    } <= enum_values(DeltaKind)


def test_item_type_has_eighteen_official_members():
    assert len(list(ItemType)) == 18
    assert HOSTED_TOOL_ITEM_TYPES <= set(ItemType)
    assert ItemType.MESSAGE == "message"
    assert ItemType.REASONING == "reasoning"
    assert ItemType.FUNCTION_CALL == "function_call"


def test_item_status_values():
    assert enum_values(ItemStatus) == {
        "in_progress",
        "completed",
        "incomplete",
    }


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

FROZEN_DATACLASSES = [NormalizedItem, OutputItem, HostedToolSpec, SanitizedRequest]


@pytest.mark.parametrize("cls", FROZEN_DATACLASSES)
def test_dataclasses_are_frozen_and_slotted(cls):
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True
    assert hasattr(cls, "__slots__")


def test_normalized_item_defaults():
    item = NormalizedItem(id="msg_1", seq=0, item_type="message")
    assert item.role == ""
    assert item.payload == {}
    assert item.redacted is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.seq = 3  # type: ignore[misc]


def test_normalized_item_default_payload_not_shared():
    a = NormalizedItem(id="a", seq=0, item_type="message")
    b = NormalizedItem(id="b", seq=1, item_type="message")
    a.payload["x"] = 1
    assert b.payload == {}


def test_output_item_defaults():
    item = OutputItem(id="fc_call_1", output_index=2, item_type=ItemType.FUNCTION_CALL)
    assert item.status is ItemStatus.IN_PROGRESS
    assert item.role == "" and item.call_id == "" and item.name == ""
    assert item.extra == {}


def test_hosted_tool_spec_shape():
    spec = HostedToolSpec(
        tool_type="web_search",
        raw={"type": "web_search"},
        required_capability=Capability.WEB_SEARCH,
        param_path="tools[2].type",
    )
    assert spec.required_capability is Capability.WEB_SEARCH
    assert spec.param_path == "tools[2].type"


def test_sanitized_request_contract_fields():
    """The four documented public fields keep their names and defaults."""
    req = SanitizedRequest(payload={"model": "gpt-4o"})
    assert req.payload == {"model": "gpt-4o"}
    assert req.dropped_fields == []
    assert req.normalized_call_ids == {}
    assert req.warnings == []
    # v3 extension defaults
    assert req.input_items == []
    assert req.reasoning_items_dropped == 0
    assert req.hosted_tools == []
    assert req.required_capabilities == frozenset()
    assert req.reasoning_event_mode == "reasoning_summary_text"
    assert req.text_format is None
    assert req.max_output_tokens is None


def test_sanitized_request_lists_stay_mutable_for_the_sanitizer():
    req = SanitizedRequest(payload={})
    req.dropped_fields.append("previous_response_id")
    req.warnings.append("dropped 1 field")
    req.normalized_call_ids["call_x"] = "call_y"
    assert req.dropped_fields == ["previous_response_id"]
    assert req.normalized_call_ids == {"call_x": "call_y"}


# ---------------------------------------------------------------------------
# Allowlists (§3.3)
# ---------------------------------------------------------------------------


def test_chat_base_allowed_exact_set():
    assert CHAT_BASE_ALLOWED == frozenset(
        {
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stop",
            "stream",
            "tools",
            "tool_choice",
            "response_format",
            "seed",
            "user",
            "reasoning_effort",
        }
    )


def test_providers_may_only_narrow_the_base_allowlist():
    for provider, keys in PROVIDER_CAPABILITIES.items():
        assert keys <= CHAT_BASE_ALLOWED, provider


def test_resolve_provider_allowlist_falls_back_safely():
    assert resolve_provider_allowlist("deepseek") == PROVIDER_CAPABILITIES["deepseek"]
    assert resolve_provider_allowlist("  DeepSeek ") == PROVIDER_CAPABILITIES["deepseek"]
    assert resolve_provider_allowlist("unknown-vendor") == PROVIDER_CAPABILITIES[DEFAULT_PROVIDER]
    assert resolve_provider_allowlist("") == PROVIDER_CAPABILITIES[DEFAULT_PROVIDER]


def test_no_responses_only_field_leaks_into_the_allowlist():
    """13 Responses-only fields must never be forwardable to Chat upstreams."""
    responses_only = {
        "input",
        "instructions",
        "previous_response_id",
        "store",
        "background",
        "reasoning",
        "text",
        "include",
        "truncation",
        "metadata",
        "parallel_tool_calls",
        "max_output_tokens",
        "service_tier",
    }
    assert CHAT_BASE_ALLOWED.isdisjoint(responses_only)


# ---------------------------------------------------------------------------
# Wire constants and helpers
# ---------------------------------------------------------------------------


def test_sse_frames_are_exact_bytes():
    assert SSE_DONE_FRAME == b"data: [DONE]\n\n"
    assert SSE_HEARTBEAT_FRAME == b": hb\n\n"


def test_canonical_json_is_stable_and_sorted():
    a = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    b = canonical_json({"a": {"c": 3, "d": 2}, "b": 1})
    assert a == b == '{"a":{"c":3,"d":2},"b":1}'
    assert canonical_json({"k": "\u4e2d\u6587"}) == '{"k":"\u4e2d\u6587"}'
    assert CANONICAL_JSON_KWARGS["sort_keys"] is True
    assert json.loads(a) == {"a": {"c": 3, "d": 2}, "b": 1}


def test_id_helpers_follow_naming_rules():
    assert make_message_item_id("resp_01hq", 0) == "msg_resp_01hq_0"
    assert make_reasoning_item_id("resp_01hq", 3) == "rs_resp_01hq_3"
    assert make_function_call_item_id("call_abc") == "fc_call_abc"
    assert make_synthetic_call_id("resp_01hq", 0) == "call_resp_01hq_0"


def test_synthetic_call_id_is_reproducible():
    first = make_synthetic_call_id("resp_x", 7)
    second = make_synthetic_call_id("resp_x", 7)
    assert first == second


def test_coerce_enum_never_raises():
    assert coerce_enum(ErrorClass, "invalid_sse_frame") is ErrorClass.INVALID_SSE_FRAME
    assert coerce_enum(ErrorClass, ErrorClass.CLIENT_DISCONNECTED) is ErrorClass.CLIENT_DISCONNECTED
    assert coerce_enum(ErrorClass, "brand_new_class_from_2027") is None
    assert coerce_enum(ErrorClass, None) is None
    assert coerce_enum(ErrorClass, 42, default=ErrorClass.INVALID_CLIENT_REQUEST) is ErrorClass.INVALID_CLIENT_REQUEST


def test_freeze_mapping_copies():
    src = {"a": 1}
    out = freeze_mapping(src)
    out["b"] = 2
    assert src == {"a": 1}
    assert freeze_mapping(None) == {}


def test_module_has_no_intra_package_imports():
    """The model layer must stay dependency-free to avoid import cycles."""
    import inspect

    from zhongzhuan.proxy.protocol import responses_models

    source = inspect.getsource(responses_models)
    assert "from zhongzhuan" not in source
    assert "from ." not in source
    assert "import zhongzhuan" not in source
