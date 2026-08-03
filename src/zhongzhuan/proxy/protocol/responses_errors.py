"""Responses Bridge v3 error mapping and secret redaction (T09).

Two responsibilities, both mandated by §10.2 of the architecture document:

1. Map each of the 14 :class:`~.responses_models.ErrorClass` members to an
   official OpenAI error object plus an HTTP status code::

       {"error": {"type": ..., "code": ..., "message": ..., "param": ...}}

2. Guarantee that **nothing** leaving the proxy carries upstream credentials,
   ``Authorization`` headers, raw reasoning text or raw tool output
   (R-P1-52).  Every message rendered by :func:`to_error_response` is passed
   through :func:`redact` and length-capped first.

This module imports only from :mod:`.responses_models` -- keep it that way so
it can be used from the config layer, the store layer and the handlers alike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .responses_models import ErrorClass, TerminalReason

# ---------------------------------------------------------------------------
# 1. Redaction (R-P1-52) -- defined first, everything else depends on it
# ---------------------------------------------------------------------------

REDACTED: str = "[REDACTED]"

#: Hard cap for any message rendered into a downstream error body.  Upstream
#: providers happily echo the whole prompt (and therefore reasoning text) back
#: inside their error strings; truncating bounds that leak even for patterns we
#: do not recognise.
MAX_MESSAGE_CHARS: int = 512

#: Ordered (pattern, replacement) pairs.  Order matters: header-shaped matches
#: run before bare-token matches so ``Authorization: Bearer sk-x`` collapses in
#: one step instead of leaving a dangling ``Authorization:``.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # -- credential-bearing headers (value = rest of the header line) --
    (re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*(?:\"|')?[^\r\n\"',}]+"),
     r"\1: " + REDACTED),
    (re.compile(r"(?i)\b(x-api-key|api-key|x-goog-api-key|anthropic-api-key)\b"
                r"\s*[:=]\s*(?:\"|')?[^\r\n\"',}]+"),
     r"\1: " + REDACTED),
    # -- JSON/kv shaped secrets: "api_key": "...", access_token=... --
    (re.compile(r"(?i)(\"?(?:api[_-]?key|access[_-]?token|secret[_-]?key|"
                r"client[_-]?secret|refresh[_-]?token)\"?)\s*[:=]\s*"
                r"(?:\"|')?[A-Za-z0-9_\-.~+/=]{6,}(?:\"|')?"),
     r"\1: " + REDACTED),
    # -- bearer tokens --
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.~+/=]{6,}"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"), "Basic " + REDACTED),
    # -- JWT (three base64url segments) --
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*"),
     REDACTED),
    # -- OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-... --
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), REDACTED),
    # -- other common vendor prefixes --
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), REDACTED),
    (re.compile(r"\b(?:gsk|xai|ghp|gho|ghu|ghs|ghr|glpat|hf|pplx|dop_v1)"
                r"[-_][A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
)

_WHITESPACE_RUN = re.compile(r"\s+")


def redact(text: str) -> str:
    """Strip credentials and credential-bearing headers out of ``text``.

    Pure string scrubbing -- no truncation, no whitespace normalisation, so it
    is safe to call on structured log values as well.  Never raises: a
    non-``str`` input is coerced with ``str()``.

    >>> redact("bad key sk-proj-AAAAAAAAAAAA")
    'bad key [REDACTED]'
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_message(text: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Redact, collapse whitespace and truncate ``text`` for downstream use.

    This is what :func:`to_error_response` applies; use :func:`redact` directly
    when you must preserve the original formatting.
    """
    cleaned = _WHITESPACE_RUN.sub(" ", redact(text)).strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "... [truncated]"
    return cleaned


#: Header names whose value is replaced wholesale by :func:`redact_headers`.
_SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "anthropic-api-key", "x-goog-api-key", "openai-api-key", "cookie",
    "set-cookie", "x-auth-token",
})


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Return a copy of ``headers`` with every credential header masked."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key)
        if name.lower() in _SENSITIVE_HEADERS:
            out[name] = REDACTED
        else:
            out[name] = redact(str(value))
    return out


# ---------------------------------------------------------------------------
# 2. ErrorClass -> HTTP status + official error object (§10.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """Static mapping row for one :class:`ErrorClass`."""

    error_class: ErrorClass
    http_status: int
    error_type: str            # official ``error.type``; "" when no body is sent
    code: str                  # official ``error.code``
    emits_body: bool = True    # False -> reported via SSE / no body at all
    retryable: bool = False    # upstream retry whitelist (T23 owns final policy)


#: HTTP 499 is the nginx "client closed request" convention.  There is no
#: client left to read the body, so ``client_disconnected`` emits none.
HTTP_CLIENT_CLOSED_REQUEST: int = 499

ERROR_SPECS: dict[ErrorClass, ErrorSpec] = {
    ErrorClass.INVALID_CLIENT_REQUEST: ErrorSpec(
        ErrorClass.INVALID_CLIENT_REQUEST, 400,
        "invalid_request_error", "invalid_request"),
    ErrorClass.UNSUPPORTED_INPUT_BLOCK: ErrorSpec(
        ErrorClass.UNSUPPORTED_INPUT_BLOCK, 400,
        "invalid_request_error", "unsupported_input_block"),
    ErrorClass.UNSUPPORTED_TOOL_CAPABILITY: ErrorSpec(
        ErrorClass.UNSUPPORTED_TOOL_CAPABILITY, 400,
        "invalid_request_error", "unsupported_tool"),
    ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE: ErrorSpec(
        ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE, 503,
        "server_error", "capability_route_unavailable"),
    ErrorClass.INVALID_TOOL_ARGUMENTS: ErrorSpec(
        ErrorClass.INVALID_TOOL_ARGUMENTS, 400,
        "invalid_request_error", "invalid_tool_arguments"),
    ErrorClass.UPSTREAM_CONNECT_ERROR: ErrorSpec(
        ErrorClass.UPSTREAM_CONNECT_ERROR, 502,
        "server_error", "upstream_connect_error", retryable=True),
    ErrorClass.UPSTREAM_RATE_LIMITED: ErrorSpec(
        ErrorClass.UPSTREAM_RATE_LIMITED, 429,
        "rate_limit_error", "rate_limit_exceeded", retryable=True),
    ErrorClass.UPSTREAM_SERVER_ERROR: ErrorSpec(
        ErrorClass.UPSTREAM_SERVER_ERROR, 502,
        "server_error", "upstream_error", retryable=True),
    ErrorClass.FIRST_TOKEN_TIMEOUT: ErrorSpec(
        ErrorClass.FIRST_TOKEN_TIMEOUT, 504,
        "server_error", "first_token_timeout", retryable=True),
    ErrorClass.READ_IDLE_TIMEOUT: ErrorSpec(
        # Not retryable: deltas were already written downstream (R-P0-34).
        ErrorClass.READ_IDLE_TIMEOUT, 504,
        "server_error", "read_idle_timeout"),
    ErrorClass.UPSTREAM_TRUNCATED: ErrorSpec(
        # 200 + SSE: surfaced through incomplete_details.reason, never a body.
        ErrorClass.UPSTREAM_TRUNCATED, 200,
        "", "upstream_truncated", emits_body=False),
    ErrorClass.INVALID_SSE_FRAME: ErrorSpec(
        ErrorClass.INVALID_SSE_FRAME, 502,
        "server_error", "invalid_sse_frame"),
    ErrorClass.CLIENT_DISCONNECTED: ErrorSpec(
        ErrorClass.CLIENT_DISCONNECTED, HTTP_CLIENT_CLOSED_REQUEST,
        "", "", emits_body=False),
    ErrorClass.INTERNAL_TRANSLATION_ERROR: ErrorSpec(
        ErrorClass.INTERNAL_TRANSLATION_ERROR, 500,
        "server_error", "internal_error"),
}

#: The four retryable upstream failures (T23 acceptance criterion 6).
RETRYABLE_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    spec.error_class for spec in ERROR_SPECS.values() if spec.retryable
)

#: Error classes that never produce an HTTP error body.
NO_BODY_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    spec.error_class for spec in ERROR_SPECS.values() if not spec.emits_body
)

#: ``TerminalReason`` -> ``ErrorClass``.  Reasons that are not failures (e.g.
#: ``NORMAL_FINISH``) are intentionally absent.
TERMINAL_REASON_TO_ERROR_CLASS: dict[TerminalReason, ErrorClass] = {
    TerminalReason.UPSTREAM_TRUNCATED: ErrorClass.UPSTREAM_TRUNCATED,
    TerminalReason.CAPABILITY_ROUTE_UNAVAILABLE:
        ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE,
    TerminalReason.CLIENT_DISCONNECTED: ErrorClass.CLIENT_DISCONNECTED,
    TerminalReason.CANCELLED_BY_CLIENT: ErrorClass.CLIENT_DISCONNECTED,
    TerminalReason.UPSTREAM_ERROR: ErrorClass.UPSTREAM_SERVER_ERROR,
    TerminalReason.UPSTREAM_CONNECT: ErrorClass.UPSTREAM_CONNECT_ERROR,
    TerminalReason.FIRST_TOKEN_TIMEOUT: ErrorClass.FIRST_TOKEN_TIMEOUT,
    TerminalReason.READ_IDLE_TIMEOUT: ErrorClass.READ_IDLE_TIMEOUT,
}


def get_spec(err_class: ErrorClass) -> ErrorSpec:
    """Return the :class:`ErrorSpec` for ``err_class``.

    Unknown values (only reachable if the enum grows without this table being
    updated) degrade to ``internal_translation_error`` rather than raising --
    an error path must never raise a second error.
    """
    spec = ERROR_SPECS.get(err_class)
    if spec is None:
        return ERROR_SPECS[ErrorClass.INTERNAL_TRANSLATION_ERROR]
    return spec


def http_status_for(err_class: ErrorClass) -> int:
    """HTTP status code mandated by §10.2 for ``err_class``."""
    return get_spec(err_class).http_status


def error_payload(
    err_class: ErrorClass,
    message: str,
    param: str | None = None,
) -> dict[str, Any]:
    """Build the inner official error object (no ``{"error": ...}`` wrapper).

    Used both by :func:`to_error_response` and by the SSE ``response.failed``
    event, which embeds the same object under its ``error`` key.
    """
    spec = get_spec(err_class)
    return {
        "type": spec.error_type or "server_error",
        "code": spec.code or err_class.value,
        "message": sanitize_message(message),
        "param": param if param else None,
    }


def to_error_response(
    err_class: ErrorClass,
    message: str,
    param: str | None = None,
    *,
    include_body: bool | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return ``(http_status, body)`` for ``err_class``.

    ``message`` is always redacted and truncated before it reaches ``body``
    (R-P1-52) -- callers may pass raw upstream text verbatim.

    ``body`` is ``{}`` for the two classes that carry no HTTP body:
    ``upstream_truncated`` (reported inside the SSE stream via
    :func:`to_incomplete_details`) and ``client_disconnected`` (nobody is
    listening).  Pass ``include_body=True`` to force an envelope anyway.
    """
    spec = get_spec(err_class)
    emit = spec.emits_body if include_body is None else bool(include_body)
    if not emit:
        return spec.http_status, {}
    return spec.http_status, {"error": error_payload(err_class, message, param)}


def to_incomplete_details(
    reason: TerminalReason | str,
    message: str = "",
) -> dict[str, Any]:
    """Build the ``incomplete_details`` object carried by a terminal event.

    Compatibility mode (Q2) sends ``response.completed`` even for truncated
    streams, so this object is the *only* diagnosable signal -- omitting it is
    treated as a P0 defect (R-P1-22).  ``message`` is optional and redacted;
    the official schema only guarantees ``reason``.
    """
    value = reason.value if isinstance(reason, TerminalReason) else str(reason)
    details: dict[str, Any] = {"reason": value}
    if message:
        details["message"] = sanitize_message(message)
    return details


def classify_http_status(status: int) -> ErrorClass:
    """Classify an upstream HTTP status into an :class:`ErrorClass`."""
    if status == 429:
        return ErrorClass.UPSTREAM_RATE_LIMITED
    if status >= 500:
        return ErrorClass.UPSTREAM_SERVER_ERROR
    if 400 <= status < 500:
        return ErrorClass.INVALID_CLIENT_REQUEST
    return ErrorClass.UPSTREAM_SERVER_ERROR


def is_retryable(err_class: ErrorClass) -> bool:
    """Whether ``err_class`` belongs to the upstream retry whitelist."""
    return get_spec(err_class).retryable


__all__ = [
    "REDACTED",
    "MAX_MESSAGE_CHARS",
    "HTTP_CLIENT_CLOSED_REQUEST",
    "ErrorSpec",
    "ERROR_SPECS",
    "RETRYABLE_ERROR_CLASSES",
    "NO_BODY_ERROR_CLASSES",
    "TERMINAL_REASON_TO_ERROR_CLASS",
    "redact",
    "sanitize_message",
    "redact_headers",
    "get_spec",
    "http_status_for",
    "error_payload",
    "to_error_response",
    "to_incomplete_details",
    "classify_http_status",
    "is_retryable",
]
