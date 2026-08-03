"""Responses Bridge v3 shared model layer: enums, dataclasses and constants.

This module is the **single source of truth** for every cross-file type used by
the v3 Responses bridge (T09).  It contains *data structures and constants only*
-- no business logic, no IO, no imports from any other ``zhongzhuan`` module.
Keeping it dependency-free is what allows T10/T11/T14/T15/T16/T23 to import it
without creating cycles.

Authoritative source: ``docs/v3/02-架构设计与任务分解.md`` §3.1 / §3.3 / §4.1 /
§10.1 / §10.7.  Deviations from the document are marked with ``DEVIATION:``.

Conventions (§10.7):
    * All dataclasses are ``frozen=True, slots=True`` unless mutation is
      genuinely required by the consumer.
    * All enums subclass ``str`` so that ``Enum.MEMBER == "raw_value"`` holds --
      this keeps existing string-based call sites (e.g. ``detect.py``) working
      unchanged during the incremental migration.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 1. Protocol / endpoint / status enums
# ---------------------------------------------------------------------------


class InboundProtocol(str, Enum):
    """Inbound wire protocol detected from the request path + headers.

    ``str`` subclass on purpose: ``InboundProtocol.RESPONSES == "responses"``
    is ``True``, so ``detect.py`` can keep returning plain ``str`` until T12
    switches it over without breaking any comparison.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    RESPONSES = "responses"


class ResponsesEndpoint(str, Enum):
    """The six ``/v1/responses`` endpoints resolved by ``proxy/router.py``."""

    CREATE = "create"              # POST   /v1/responses
    RETRIEVE = "retrieve"          # GET    /v1/responses/{id}
    DELETE = "delete"              # DELETE /v1/responses/{id}
    CANCEL = "cancel"              # POST   /v1/responses/{id}/cancel
    COMPACT = "compact"            # POST   /v1/responses/compact
    INPUT_ITEMS = "input_items"    # GET    /v1/responses/{id}/input_items


class ResponseStatus(str, Enum):
    """Official ``response.status`` values (also the background job status)."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"


TERMINAL_RESPONSE_STATUSES: frozenset[ResponseStatus] = frozenset({
    ResponseStatus.COMPLETED,
    ResponseStatus.FAILED,
    ResponseStatus.INCOMPLETE,
    ResponseStatus.CANCELLED,
})


class ExecutionMode(str, Enum):
    """Capability router decision (§3.9): native > emulate > translate."""

    NATIVE = "native"
    EMULATE = "emulate"
    TRANSLATE = "translate"


class ReasoningEventMode(str, Enum):
    """``responses_bridge.reasoning_event_mode`` (Q1)."""

    SUMMARY_TEXT = "reasoning_summary_text"
    TEXT = "reasoning_text"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# 2. Emitter state machine (§4.1)
# ---------------------------------------------------------------------------


class EmitterState(str, Enum):
    """``ResponsesEventEmitter`` lifecycle state.

    DEVIATION (§3.1): the document declares 7 states and folds every terminal
    outcome into ``COMPLETED``.  Three explicit terminal states are added --
    ``FAILED`` / ``INCOMPLETE`` / ``CANCELLED`` -- so that the emitter state
    alone tells which terminal event was written.  The documented transition
    ``COMPLETING -> COMPLETED`` remains legal, so §4.1's table is a strict
    subset of :data:`ALLOWED_TRANSITIONS` and no documented behaviour changes.
    """

    INIT = "init"
    QUEUED = "queued"
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    STREAMING = "streaming"
    COMPLETING = "completing"
    # -- terminal --
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"


TERMINAL_EMITTER_STATES: frozenset[EmitterState] = frozenset({
    EmitterState.COMPLETED,
    EmitterState.FAILED,
    EmitterState.INCOMPLETE,
    EmitterState.CANCELLED,
})

#: Legal state transitions (§4.1 "合法转换全表").  ``heartbeat()`` never
#: transitions (R-P0-21) and therefore does not appear here.
ALLOWED_TRANSITIONS: dict[EmitterState, frozenset[EmitterState]] = {
    EmitterState.INIT: frozenset({EmitterState.QUEUED, EmitterState.CREATED}),
    EmitterState.QUEUED: frozenset({EmitterState.CREATED}),
    EmitterState.CREATED: frozenset({EmitterState.IN_PROGRESS}),
    EmitterState.IN_PROGRESS: frozenset({
        EmitterState.STREAMING,
        EmitterState.COMPLETING,
    }),
    # Self loop: delta() / open_item() / close_item() keep STREAMING.
    EmitterState.STREAMING: frozenset({
        EmitterState.STREAMING,
        EmitterState.COMPLETING,
    }),
    EmitterState.COMPLETING: frozenset(TERMINAL_EMITTER_STATES),
    EmitterState.COMPLETED: frozenset(),
    EmitterState.FAILED: frozenset(),
    EmitterState.INCOMPLETE: frozenset(),
    EmitterState.CANCELLED: frozenset(),
}

#: ``response.status`` -> emitter terminal state, used by ``terminate()``.
RESPONSE_STATUS_TO_EMITTER_STATE: dict[ResponseStatus, EmitterState] = {
    ResponseStatus.QUEUED: EmitterState.QUEUED,
    ResponseStatus.IN_PROGRESS: EmitterState.IN_PROGRESS,
    ResponseStatus.COMPLETED: EmitterState.COMPLETED,
    ResponseStatus.FAILED: EmitterState.FAILED,
    ResponseStatus.INCOMPLETE: EmitterState.INCOMPLETE,
    ResponseStatus.CANCELLED: EmitterState.CANCELLED,
}


def is_legal_transition(src: EmitterState, dst: EmitterState) -> bool:
    """Return ``True`` when ``src -> dst`` is allowed by the state machine."""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def transition_label(src: EmitterState, dst: EmitterState) -> str:
    """Metric label for ``responses_illegal_transitions_total{transition}``."""
    return "{0}->{1}".format(src.value, dst.value)


# ---------------------------------------------------------------------------
# 3. Delta kinds / capabilities
# ---------------------------------------------------------------------------


class DeltaKind(str, Enum):
    """Streaming delta families accepted by ``ResponsesEventEmitter.delta()``.

    DEVIATION (§3.1): the document lists the first five.  Two hosted-tool delta
    families required by §10.3's event list are appended so T26/T27 do not have
    to widen the enum later (widening is source-compatible, narrowing is not).
    """

    OUTPUT_TEXT = "output_text"
    REFUSAL = "refusal"
    REASONING_SUMMARY_TEXT = "reasoning_summary_text"
    REASONING_TEXT = "reasoning_text"
    FUNCTION_CALL_ARGUMENTS = "function_call_arguments"
    # -- hosted tool deltas (§10.3) --
    CODE_INTERPRETER_CODE = "code_interpreter_call_code"
    MCP_CALL_ARGUMENTS = "mcp_call_arguments"


class Capability(str, Enum):
    """Upstream capabilities used by ``CapabilityRouter``.

    Nine members: the eight of §4.2.9 plus ``TOOL_SEARCH`` (deviation B6).
    """

    STATEFUL_RESPONSES = "stateful_responses"
    BACKGROUND = "background"
    WEB_SEARCH = "web_search"
    FILE_SEARCH = "file_search"
    COMPUTER = "computer"
    CODE_INTERPRETER = "code_interpreter"
    IMAGE_GENERATION = "image_generation"
    REMOTE_MCP = "remote_mcp"
    TOOL_SEARCH = "tool_search"


#: Hosted tool ``type`` string -> capability required to serve it.
#: Consumed by the request schema (T10), the capability router (T25) and the
#: hosted tool layer (T26) so the mapping is never duplicated.
HOSTED_TOOL_CAPABILITY: dict[str, Capability] = {
    "web_search": Capability.WEB_SEARCH,
    "web_search_preview": Capability.WEB_SEARCH,
    "web_search_preview_2025_03_11": Capability.WEB_SEARCH,
    "file_search": Capability.FILE_SEARCH,
    "computer": Capability.COMPUTER,
    "computer_use_preview": Capability.COMPUTER,
    "code_interpreter": Capability.CODE_INTERPRETER,
    "image_generation": Capability.IMAGE_GENERATION,
    "mcp": Capability.REMOTE_MCP,
    "tool_search": Capability.TOOL_SEARCH,
}


# ---------------------------------------------------------------------------
# 4. Error classes (§10.2) and terminal reasons (§9.4)
# ---------------------------------------------------------------------------


class ErrorClass(str, Enum):
    """The 14 internal error classes (R-P1-51: 12 base + Q4's 2 capability)."""

    INVALID_CLIENT_REQUEST = "invalid_client_request"
    UNSUPPORTED_INPUT_BLOCK = "unsupported_input_block"
    UPSTREAM_CONNECT_ERROR = "upstream_connect_error"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_SERVER_ERROR = "upstream_server_error"
    FIRST_TOKEN_TIMEOUT = "first_token_timeout"
    READ_IDLE_TIMEOUT = "read_idle_timeout"
    UPSTREAM_TRUNCATED = "upstream_truncated"
    INVALID_SSE_FRAME = "invalid_sse_frame"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    CLIENT_DISCONNECTED = "client_disconnected"
    INTERNAL_TRANSLATION_ERROR = "internal_translation_error"
    UNSUPPORTED_TOOL_CAPABILITY = "unsupported_tool_capability"
    CAPABILITY_ROUTE_UNAVAILABLE = "capability_route_unavailable"


class TerminalReason(str, Enum):
    """Why a response stream ended.  Persisted in ``responses.terminal_reason``.

    DEVIATION (§3.1): three timeout reasons are added -- ``UPSTREAM_CONNECT``,
    ``FIRST_TOKEN_TIMEOUT``, ``READ_IDLE_TIMEOUT``.  R-P1-26 / T28-4 require the
    four timeout classes (connect / first-token / read-idle / total) to map to
    *mutually distinct* ``terminal_reason`` values; §3.1 only provided
    ``MAX_RESPONSE_TIME`` and the catch-all ``UPSTREAM_ERROR``, which would make
    three of the four collide.  See :data:`TIMEOUT_REASONS`.
    """

    NORMAL_FINISH = "normal_finish"
    # -- §9.4: the ten circuit-breaker reasons (R-P0-32) --
    MAX_TOOL_ROUNDS = "max_tool_rounds"
    MAX_TOTAL_TOOL_CALLS = "max_total_tool_calls"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    REPEATED_TOOL_FAILURE = "repeated_tool_failure"
    MAX_RESPONSE_TIME = "max_response_time"
    MAX_OUTPUT_BUDGET = "max_output_budget"
    RESPONSE_CHAIN_CYCLE = "response_chain_cycle"
    RESPONSE_CHAIN_TOO_DEEP = "response_chain_too_deep"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    BACKGROUND_BUDGET_EXHAUSTED = "background_budget_exhausted"
    # -- non circuit-breaker terminations --
    UPSTREAM_TRUNCATED = "upstream_truncated"
    CAPABILITY_ROUTE_UNAVAILABLE = "capability_route_unavailable"
    CLIENT_DISCONNECTED = "client_disconnected"
    UPSTREAM_ERROR = "upstream_error"
    CANCELLED_BY_CLIENT = "cancelled_by_client"
    # -- timeout classification (DEVIATION, see docstring) --
    UPSTREAM_CONNECT = "upstream_connect"
    FIRST_TOKEN_TIMEOUT = "first_token_timeout"
    READ_IDLE_TIMEOUT = "read_idle_timeout"


#: Exactly the ten circuit-breaker reasons enumerated by R-P0-32 / §9.4.
CIRCUIT_BREAKER_REASONS: frozenset[TerminalReason] = frozenset({
    TerminalReason.MAX_TOOL_ROUNDS,
    TerminalReason.MAX_TOTAL_TOOL_CALLS,
    TerminalReason.REPEATED_TOOL_CALL,
    TerminalReason.REPEATED_TOOL_FAILURE,
    TerminalReason.MAX_RESPONSE_TIME,
    TerminalReason.MAX_OUTPUT_BUDGET,
    TerminalReason.RESPONSE_CHAIN_CYCLE,
    TerminalReason.RESPONSE_CHAIN_TOO_DEEP,
    TerminalReason.RETRY_BUDGET_EXHAUSTED,
    TerminalReason.BACKGROUND_BUDGET_EXHAUSTED,
})

#: The four timeout classes of R-P1-26; all four values differ (T28-4).
TIMEOUT_REASONS: frozenset[TerminalReason] = frozenset({
    TerminalReason.UPSTREAM_CONNECT,
    TerminalReason.FIRST_TOKEN_TIMEOUT,
    TerminalReason.READ_IDLE_TIMEOUT,
    TerminalReason.MAX_RESPONSE_TIME,
})

#: Reasons that must be reported through ``incomplete_details.reason`` rather
#: than an HTTP error body (compatibility mode, Q2).
INCOMPLETE_REASONS: frozenset[TerminalReason] = frozenset(
    CIRCUIT_BREAKER_REASONS
    | {
        TerminalReason.UPSTREAM_TRUNCATED,
        TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE,
    }
)


# ---------------------------------------------------------------------------
# 5. Item model (§3.3, §5.4)
# ---------------------------------------------------------------------------


class ItemType(str, Enum):
    """The 18 official Responses item types (input and output share the enum)."""

    MESSAGE = "message"
    REASONING = "reasoning"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    CUSTOM_TOOL_CALL = "custom_tool_call"
    CUSTOM_TOOL_CALL_OUTPUT = "custom_tool_call_output"
    FILE_SEARCH_CALL = "file_search_call"
    WEB_SEARCH_CALL = "web_search_call"
    COMPUTER_CALL = "computer_call"
    COMPUTER_CALL_OUTPUT = "computer_call_output"
    CODE_INTERPRETER_CALL = "code_interpreter_call"
    IMAGE_GENERATION_CALL = "image_generation_call"
    LOCAL_SHELL_CALL = "local_shell_call"
    LOCAL_SHELL_CALL_OUTPUT = "local_shell_call_output"
    MCP_CALL = "mcp_call"
    MCP_LIST_TOOLS = "mcp_list_tools"
    MCP_APPROVAL_REQUEST = "mcp_approval_request"
    MCP_APPROVAL_RESPONSE = "mcp_approval_response"


#: Item types produced by hosted tools -- never generated by the local bridge.
HOSTED_TOOL_ITEM_TYPES: frozenset[ItemType] = frozenset({
    ItemType.FILE_SEARCH_CALL,
    ItemType.WEB_SEARCH_CALL,
    ItemType.COMPUTER_CALL,
    ItemType.CODE_INTERPRETER_CALL,
    ItemType.IMAGE_GENERATION_CALL,
    ItemType.LOCAL_SHELL_CALL,
    ItemType.MCP_CALL,
    ItemType.MCP_LIST_TOOLS,
    ItemType.MCP_APPROVAL_REQUEST,
})


class ItemStatus(str, Enum):
    """Official ``item.status`` values."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class NormalizedItem:
    """A canonical input item, ready to be persisted or replayed.

    ``redacted`` is permanently ``True`` for ``reasoning`` items: only metadata
    survives in ``payload`` and the raw text is never written anywhere
    (R-P0-14 / R-P1-29 / R-P1-40).
    """

    id: str
    seq: int
    item_type: str                                # ItemType value; str for forward compat
    role: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class OutputItem:
    """Descriptor handed to ``ResponsesEventEmitter.open_item()``.

    DEVIATION: §3.1 names ``OutputItem`` but never defines it.  The shape below
    is the minimum the emitter needs to build ``response.output_item.added`` /
    ``.done`` payloads without reaching back into the accumulators.  It carries
    **no** reasoning text on purpose -- reasoning deltas travel through
    ``emitter.delta()`` and are released as soon as the stream ends (R-P1-04).
    """

    id: str
    output_index: int
    item_type: ItemType
    status: ItemStatus = ItemStatus.IN_PROGRESS
    role: str = ""                                # message items only
    call_id: str = ""                             # function/tool call items only
    name: str = ""                                # tool name
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HostedToolSpec:
    """A hosted tool requested by the client (§3.3)."""

    tool_type: str
    raw: dict[str, Any]
    required_capability: Capability
    param_path: str = ""                          # e.g. "tools[2].type"


@dataclass(frozen=True, slots=True)
class SanitizedRequest:
    """Result of ``RequestSanitizer.sanitize()`` (§3.3).

    The first four fields are the frozen public contract from the design doc
    (§5.1); everything below is the v3 extension.  The dataclass is frozen but
    the ``list`` / ``dict`` members stay mutable so the sanitizer can accumulate
    into them while building.
    """

    payload: dict[str, Any]                       # allowlist-constructed upstream body
    dropped_fields: list[str] = field(default_factory=list)
    normalized_call_ids: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # -- v3 extension --
    input_items: list[NormalizedItem] = field(default_factory=list)
    reasoning_items_dropped: int = 0
    hosted_tools: list[HostedToolSpec] = field(default_factory=list)
    required_capabilities: frozenset[Capability] = frozenset()
    reasoning_event_mode: str = ReasoningEventMode.SUMMARY_TEXT.value
    text_format: dict[str, Any] | None = None     # Q7: consumed -> response_format
    max_output_tokens: int | None = None


# ---------------------------------------------------------------------------
# 6. Upstream payload allowlists (§3.3)
# ---------------------------------------------------------------------------

#: The only keys that may ever appear in an upstream Chat Completions body.
#: Providers may **narrow** this set, never widen it.
CHAT_BASE_ALLOWED: frozenset[str] = frozenset({
    "model", "temperature", "top_p", "max_tokens", "stop", "stream", "tools",
    "tool_choice", "response_format", "seed", "user", "reasoning_effort",
})

#: Per-provider narrowing of :data:`CHAT_BASE_ALLOWED`.  T11 owns any further
#: refinement; every value here is asserted to be a subset by the unit tests.
PROVIDER_CAPABILITIES: dict[str, frozenset[str]] = {
    "openai_compatible": CHAT_BASE_ALLOWED,
    "openai": CHAT_BASE_ALLOWED,
    # DeepSeek rejects seed/user and has no reasoning_effort knob.
    "deepseek": CHAT_BASE_ALLOWED - {"seed", "user", "reasoning_effort"},
    # Anthropic (post-translation) has no seed/response_format/reasoning_effort.
    "anthropic": CHAT_BASE_ALLOWED - {
        "seed", "response_format", "reasoning_effort", "user",
    },
}

DEFAULT_PROVIDER: str = "openai_compatible"


def resolve_provider_allowlist(provider: str) -> frozenset[str]:
    """Return the upstream key allowlist for ``provider`` (safe fallback)."""
    return PROVIDER_CAPABILITIES.get(
        (provider or "").strip().lower(),
        PROVIDER_CAPABILITIES[DEFAULT_PROVIDER],
    )


# ---------------------------------------------------------------------------
# 7. Wire constants and ID helpers (§10.1, §10.3, §10.7)
# ---------------------------------------------------------------------------

SSE_DONE_FRAME: bytes = b"data: [DONE]\n\n"
SSE_HEARTBEAT_FRAME: bytes = b": hb\n\n"
SSE_CONTENT_TYPE: str = "text/event-stream"

#: ``json.dumps`` keyword arguments mandated by §10.7 for any cross-turn stable
#: serialisation (tool signatures, idempotency digests, golden fixtures).
CANONICAL_JSON_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
}


def canonical_json(obj: Any) -> str:
    """Serialise ``obj`` deterministically (sorted keys, no padding, UTF-8)."""
    return json.dumps(obj, **CANONICAL_JSON_KWARGS)


def make_message_item_id(response_id: str, output_index: int) -> str:
    """``msg_{response_id}_{output_index}`` (§10.1)."""
    return "msg_{0}_{1}".format(response_id, output_index)


def make_reasoning_item_id(response_id: str, output_index: int) -> str:
    """``rs_{response_id}_{output_index}`` (§10.1)."""
    return "rs_{0}_{1}".format(response_id, output_index)


def make_function_call_item_id(call_id: str) -> str:
    """``fc_{call_id}`` (§10.1)."""
    return "fc_{0}".format(call_id)


def make_synthetic_call_id(response_id: str, source_index: int) -> str:
    """``call_{response_id}_{source_index}`` -- stable, reproducible (R-P1-07)."""
    return "call_{0}_{1}".format(response_id, source_index)


def coerce_enum(enum_cls: type, value: Any, default: Any = None) -> Any:
    """Best-effort ``value`` -> ``enum_cls`` member, returning ``default``.

    Used at trust boundaries (DB rows, upstream JSON) where a value may be an
    unknown string from a newer API version; never raises.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, KeyError, TypeError):
        return default


def enum_values(enum_cls: type[Enum]) -> frozenset[str]:
    """Return the raw ``str`` values of every member of ``enum_cls``."""
    return frozenset(str(member.value) for member in enum_cls)


def freeze_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a shallow copy of ``mapping`` (``{}`` when ``None``)."""
    return dict(mapping) if mapping else {}


__all__ = [
    # enums
    "InboundProtocol",
    "ResponsesEndpoint",
    "ResponseStatus",
    "ExecutionMode",
    "ReasoningEventMode",
    "EmitterState",
    "DeltaKind",
    "Capability",
    "ErrorClass",
    "TerminalReason",
    "ItemType",
    "ItemStatus",
    # dataclasses
    "NormalizedItem",
    "OutputItem",
    "HostedToolSpec",
    "SanitizedRequest",
    # constant tables
    "ALLOWED_TRANSITIONS",
    "TERMINAL_EMITTER_STATES",
    "TERMINAL_RESPONSE_STATUSES",
    "RESPONSE_STATUS_TO_EMITTER_STATE",
    "CIRCUIT_BREAKER_REASONS",
    "TIMEOUT_REASONS",
    "INCOMPLETE_REASONS",
    "HOSTED_TOOL_CAPABILITY",
    "HOSTED_TOOL_ITEM_TYPES",
    "CHAT_BASE_ALLOWED",
    "PROVIDER_CAPABILITIES",
    "DEFAULT_PROVIDER",
    "SSE_DONE_FRAME",
    "SSE_HEARTBEAT_FRAME",
    "SSE_CONTENT_TYPE",
    "CANONICAL_JSON_KWARGS",
    # helpers
    "is_legal_transition",
    "transition_label",
    "resolve_provider_allowlist",
    "canonical_json",
    "make_message_item_id",
    "make_reasoning_item_id",
    "make_function_call_item_id",
    "make_synthetic_call_id",
    "coerce_enum",
    "enum_values",
    "freeze_mapping",
]
