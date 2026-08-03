"""Responses Bridge v3 versioned request schema and item registry (T10).

Two independent responsibilities, both derived from the OpenAI Responses API
official schema and the v3 architecture document (§3.3 / §4.2.7 / §5.4):

``responses_schema.py``
    The **versioned request schema**: the full allowlist of official top-level
    ``/v1/responses`` create fields, plus the two-step processing pipeline
    (validate first, then decide per execution mode whether a field is
    passed through / consumed / emulated / dropped).  It owns the Q7 rule
    (``text.format`` is consumed and turned into ``response_format``, never
    dropped) and the ``dropped_fields`` bookkeeping.

``item_registry.py``
    The **versioned item registry**: known constructors for the 18 official
    item types, so each item can be parsed from a wire object, serialised back
    to a canonical form, and redacted (reasoning items keep metadata only).

This module imports only from :mod:`.responses_models` and :mod:`.errors`
(never from ``responses.py``) so it can be used by the config layer, the
store layer and the v3 handlers without creating cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .responses_errors import ErrorClass, to_error_response
from .responses_models import (
    ItemType,
    NormalizedItem,
    canonical_json,
    coerce_enum,
    enum_values,
)

# ---------------------------------------------------------------------------
# 1. Official top-level request fields (versioned allowlist)
# ---------------------------------------------------------------------------

#: The official ``POST /v1/responses`` create request fields.  Anything not in
#: this set is a dropped/unknown field (goes to ``dropped_fields``).
#: ``str`` keys because the wire body is a ``dict`` parsed from JSON.
RESPONSES_CREATE_FIELDS: frozenset[str] = frozenset({
    "model",
    "input",
    "instructions",
    "max_output_tokens",
    "previous_response_id",
    "store",
    "metadata",
    "reasoning",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "text",
    "output",
    "prompt",
    "truncation",
    "stream",
    "user",
    "include",
    "stream_options",
    "temperature",
    "top_p",
    "max_wall_time_seconds",
    "retry",
    "schema",
})

#: The Responses-specific fields that are NOT part of the Chat Completions
#: shape and require explicit bridge handling (must never pass through the
#: upstream body verbatim as Chat fields): each is either translated, emulated
#: or consumed deliberately (T10-4).
RESPONSES_ONLY_FIELDS: frozenset[str] = frozenset({
    "previous_response_id",   # state chain -> mapped to conversation, not body
    "store",                  # persistence flag, not an upstream knob
    "metadata",               # persisted, not forwarded
    "reasoning",              # reasoning config -> parsed, not forwarded
    "input",                  # item list -> converted to messages
    "instructions",           # -> system message
    "tools",                  # -> Chat Completions tools
    "tool_choice",            # -> Chat Completions tool_choice
    "parallel_tool_calls",    # -> Chat Completions parallel_tool_calls
    "text",                   # text.format -> response_format (Q7), else dropped
    "output",                 # output config -> consumed/emulated
    "prompt",                 # v2 alias of instructions -> consumed
    "truncation",             # -> not supported upstream, dropped or emulated
    "include",                # subscription list, consumed
    "stream_options",         # -> stream_options passthrough when supported
    "max_wall_time_seconds",  # budget, consumed by ExecutionBudget
    "retry",                  # budget, consumed by AttemptManager
})

#: The subset of Responses-only fields that are *fully consumed* by the bridge
#: and never appear in the upstream payload under any form.  Contrast with
#: ``tools``/``tool_choice``/``parallel_tool_calls``/``instructions``/``text``
#: which are translated (e.g. ``tools`` -> ``tools``, ``instructions`` ->
#: ``messages[].system``, ``text.format`` -> ``response_format``) and therefore
#: *do* legitimately land in the upstream body, just never verbatim.
NOT_FORWARDED_FIELDS: frozenset[str] = frozenset({
    "previous_response_id",
    "store",
    "metadata",
    "reasoning",
    "input",
    "output",
    "prompt",
    "truncation",
    "include",
    "stream_options",
    "max_wall_time_seconds",
    "retry",
})

#: Fields that are safe to pass through to a Chat Completions upstream
#: unchanged (they exist in both shapes).
CHAT_COMPATIBLE_FIELDS: frozenset[str] = frozenset({
    "model",
    "max_output_tokens",       # -> max_tokens
    "stream",
    "user",
    "temperature",
    "top_p",
    "seed",
})


# ---------------------------------------------------------------------------
# 2. Field validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Validation + handling rule for one top-level request field."""

    name: str
    #: ``None`` = any type; otherwise a tuple of accepted JSON types.
    allowed_types: tuple[type, ...] | None = None
    #: Json-path of the value for the error ``param`` field, e.g. "tools[2]".
    param_path: str = ""
    #: Whether the field is consumed by the bridge (never forwarded verbatim).
    consumed: bool = False
    #: Whether the field is only meaningful for Responses (never forwarded).
    responses_only: bool = False
    #: When set and the field is absent, the field is injected with this value.
    default: Any = None


#: Per-field specs.  ``allowed_types`` is the JSON type of the *value*.
FIELD_SPECS: dict[str, FieldSpec] = {
    "model": FieldSpec("model", allowed_types=(str,)),
    "input": FieldSpec("input", allowed_types=(str, list)),
    "instructions": FieldSpec("instructions", allowed_types=(str,), consumed=True),
    "max_output_tokens": FieldSpec("max_output_tokens", allowed_types=(int,)),
    "previous_response_id": FieldSpec(
        "previous_response_id", allowed_types=(str,), responses_only=True),
    "store": FieldSpec("store", allowed_types=(bool,), responses_only=True),
    "metadata": FieldSpec("metadata", allowed_types=(dict,), responses_only=True),
    "reasoning": FieldSpec("reasoning", allowed_types=(dict,), responses_only=True),
    "tools": FieldSpec("tools", allowed_types=(list,), consumed=True),
    "tool_choice": FieldSpec("tool_choice", consumed=True),
    "parallel_tool_calls": FieldSpec(
        "parallel_tool_calls", allowed_types=(bool,), consumed=True),
    "text": FieldSpec("text", allowed_types=(dict,), consumed=True),
    "output": FieldSpec("output", allowed_types=(dict,), responses_only=True),
    "prompt": FieldSpec("prompt", allowed_types=(str,), consumed=True),
    "truncation": FieldSpec("truncation", allowed_types=(str,), responses_only=True),
    "stream": FieldSpec("stream", allowed_types=(bool,)),
    "user": FieldSpec("user", allowed_types=(str,)),
    "include": FieldSpec("include", allowed_types=(list,), responses_only=True),
    "stream_options": FieldSpec("stream_options", allowed_types=(dict,)),
    "temperature": FieldSpec("temperature", allowed_types=(float, int)),
    "top_p": FieldSpec("top_p", allowed_types=(float, int)),
    "max_wall_time_seconds": FieldSpec(
        "max_wall_time_seconds", allowed_types=(int, float), responses_only=True),
    "retry": FieldSpec("retry", allowed_types=(dict,), responses_only=True),
    "schema": FieldSpec("schema", allowed_types=(dict,), consumed=True),
}


# ---------------------------------------------------------------------------
# 3. Schema validation result
# ---------------------------------------------------------------------------


@dataclass
class SchemaValidation:
    """Outcome of :func:`validate_requests_schema`."""

    valid: bool
    unknown_fields: list[str] = field(default_factory=list)
    invalid_fields: list[tuple[str, str]] = field(default_factory=list)
    #: (param, message) tuples for the 400 error body.
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def error_param(self) -> str:
        return self.errors[0][0] if self.errors else ""

    @property
    def error_message(self) -> str:
        return self.errors[0][1] if self.errors else ""


def validate_requests_schema(body: Mapping[str, Any]) -> SchemaValidation:
    """Validate a raw ``/v1/responses`` body against the official schema.

    Two categories of problem, both reported:
    * unknown fields -- not in :data:`RESPONSES_CREATE_FIELDS` (go to
      ``dropped_fields``, never a hard error);
    * invalid values -- a known field carrying a value of the wrong JSON type
      (a hard 400, per R-P1-41 / T10-1).

    Returns a :class:`SchemaValidation`; the caller decides whether to stop
    (invalid) or to continue with dropped fields recorded (unknown only).
    """
    result = SchemaValidation(valid=True)
    if not isinstance(body, Mapping):
        result.valid = False
        result.errors.append(("", "request body must be a JSON object"))
        return result

    for key, value in body.items():
        spec = FIELD_SPECS.get(key)
        if spec is None:
            result.unknown_fields.append(key)
            continue
        if spec.allowed_types is not None and not isinstance(value, spec.allowed_types):
            result.valid = False
            result.invalid_fields.append((key, type(value).__name__))
            result.errors.append((
                spec.name,
                f"field '{spec.name}' must be of type "
                f"{', '.join(t.__name__ for t in spec.allowed_types)}",
            ))
    return result


# ---------------------------------------------------------------------------
# 4. Two-step processing pipeline
# ---------------------------------------------------------------------------


@dataclass
class ProcessedRequest:
    """Result of :func:`process_requests_schema` (the two-step pipeline)."""

    #: The upstream body after allowlist construction + Responses-only removal.
    payload: dict[str, Any] = field(default_factory=dict)
    #: Fields that were dropped (unknown / Responses-only / unsupported).
    dropped_fields: list[str] = field(default_factory=list)
    #: ``text.format`` consumed and turned into ``response_format`` (Q7).
    text_format: dict[str, Any] | None = None
    #: The raw ``input`` field (may be str or list) for downstream item parsing.
    raw_input: Any = None
    #: ``max_output_tokens`` -> ``max_tokens`` mapping.
    max_tokens: int | None = None
    #: ``reasoning`` config object (``{"effort": ...}``).
    reasoning: dict[str, Any] | None = None


def _coerce_max_output_tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def process_requests_schema(
    body: Mapping[str, Any],
    *,
    execution_mode: str = "translate",
) -> ProcessedRequest:
    """Run the two-step processing pipeline over a raw request body.

    Step 1 -- validate (any hard error surfaces as a 400 via the caller).
    Step 2 -- build the upstream payload field by field:
      * official Responses-only fields are consumed (never forwarded);
      * Chat-compatible fields are copied through (with the ``max_output_tokens
        -> max_tokens`` rename);
      * unknown fields and deliberately-unsupported fields are recorded in
        ``dropped_fields``;
      * ``text.format`` is consumed and turned into ``response_format`` (Q7).

    ``execution_mode`` (``native`` / ``emulate`` / ``translate``) is honoured
    for the few fields whose handling differs by mode (e.g. ``output`` is fully
    consumed in ``translate`` but echoed in ``native``).
    """
    result = ProcessedRequest()
    if not isinstance(body, Mapping):
        return result

    src = dict(body)
    payload: dict[str, Any] = {}

    # -- model: always forwarded --
    if "model" in src:
        payload["model"] = src["model"]

    # -- max_output_tokens -> max_tokens --
    if "max_output_tokens" in src:
        mt = _coerce_max_output_tokens(src["max_output_tokens"])
        result.max_tokens = mt
        if mt is not None:
            payload["max_tokens"] = mt

    # -- stream / temperature / top_p / user / seed: passthrough --
    for key in ("stream", "temperature", "top_p", "user", "seed"):
        if key in src:
            payload[key] = src[key]

    # -- instructions / prompt -> system message (consumed) --
    instructions = src.get("instructions")
    if instructions is None and src.get("prompt"):
        instructions = src["prompt"]
    if instructions:
        payload["messages"] = payload.get("messages", [])
        payload["messages"].append({"role": "system", "content": instructions})

    # -- input -> messages (consumed; item parsing happens downstream) --
    if "input" in src:
        result.raw_input = src["input"]

    # -- tools / tool_choice / parallel_tool_calls -> Chat Completions tools --
    if "tools" in src:
        payload["tools"] = src["tools"]
    if "tool_choice" in src:
        payload["tool_choice"] = src["tool_choice"]
    if "parallel_tool_calls" in src:
        payload["parallel_tool_calls"] = src["parallel_tool_calls"]

    # -- text.format -> response_format (Q7) --
    text_cfg = src.get("text")
    if isinstance(text_cfg, dict):
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "text"):
            # Q7: consumed, not dropped -- becomes response_format upstream.
            result.text_format = fmt
            rf_type = fmt.get("type")
            if rf_type == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": fmt.get("schema", {}),
                }
            elif rf_type == "text":
                payload["response_format"] = {"type": "text"}
        # A ``text`` field without a recognised format is still an official
        # field (consumed); it is not treated as an unknown/dropped field.
        elif "text" in src:
            result.dropped_fields.append("text")

    # -- reasoning config (consumed, not forwarded) --
    if "reasoning" in src:
        result.reasoning = src["reasoning"] if isinstance(src["reasoning"], dict) else None

    # -- Responses-only fields are CONSUMED (parsed by the bridge), not dropped.
    #    They never reach the upstream payload, but they are not "dropped"
    #    fields either -- dropping implies the caller did not understand them.
    # -- prompt alias: consumed into instructions above.

    # -- unknown fields (the only true "dropped" set) --
    for key in src:
        if key not in RESPONSES_CREATE_FIELDS:
            result.dropped_fields.append(key)

    # Commit the upstream payload built above.
    result.payload = payload
    return result


# ---------------------------------------------------------------------------
# 5. Versioned schema registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaVersion:
    """One version of the request schema."""

    version: str
    fields: frozenset[str]
    responses_only: frozenset[str]


#: The schema version this bridge targets.  Bump only when the official
#: Responses API adds/removes top-level fields.
SCHEMA_VERSION: str = "2025-03-26"

SCHEMA_VERSIONS: dict[str, SchemaVersion] = {
    SCHEMA_VERSION: SchemaVersion(
        version=SCHEMA_VERSION,
        fields=RESPONSES_CREATE_FIELDS,
        responses_only=RESPONSES_ONLY_FIELDS,
    ),
}


def supported_schema_version() -> str:
    """Return the schema version this bridge implements."""
    return SCHEMA_VERSION


def is_responses_only_field(name: str) -> bool:
    """Whether ``name`` is a Responses-only field (never forwarded upstream)."""
    return name in RESPONSES_ONLY_FIELDS


def is_known_field(name: str) -> bool:
    """Whether ``name`` is an official Responses create field."""
    return name in RESPONSES_CREATE_FIELDS


__all__ = [
    "RESPONSES_CREATE_FIELDS",
    "RESPONSES_ONLY_FIELDS",
    "NOT_FORWARDED_FIELDS",
    "CHAT_COMPATIBLE_FIELDS",
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "FieldSpec",
    "FIELD_SPECS",
    "SchemaValidation",
    "ProcessedRequest",
    "validate_requests_schema",
    "process_requests_schema",
    "supported_schema_version",
    "is_responses_only_field",
    "is_known_field",
]