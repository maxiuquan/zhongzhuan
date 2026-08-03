"""14-field structured request logging + redaction (T29 / R-P1-54, R-P1-55, R-P2-12).

Two responsibilities, both mandated by §11.2 of the architecture document:

1. **One canonical request log record** with exactly the 14 keys of R-P1-54::

       request_id · session_id_hash · model · upstream_protocol · upstream_key_id
       stream · attempt · first_token_ms · duration_ms · dropped_fields
       reasoning_history_items_dropped · tool_call_count · terminal_reason
       client_disconnected

   :func:`to_log_json` serialises a record to a single-line JSON object with
   **exactly** these keys (no more, no fewer), so a log consumer can parse the
   line and rely on the schema.

2. **Redaction + truncation before anything reaches the wire** (R-P1-55 /
   R-P2-12): no API key, no ``Authorization`` header, no full reasoning text,
   and (by default) no full tool arguments / output is ever written.  Every
   string value is pushed through :func:`sanitize_text` which *redacts first,
   then truncates* -- the order is load-bearing: a sensitive pattern that sits
   near the truncation boundary must already be ``[REDACTED]`` before any
   characters are cut, otherwise cutting could leave a recognisable fragment
   of the secret in the log.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from loguru import logger as _default_logger

from ..proxy.protocol.responses_errors import redact as _redact_sensitive

# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

#: The 14 mandatory request-log keys, in canonical order (R-P1-54).
REQUEST_LOG_FIELDS: tuple[str, ...] = (
    "request_id",
    "session_id_hash",
    "model",
    "upstream_protocol",
    "upstream_key_id",
    "stream",
    "attempt",
    "first_token_ms",
    "duration_ms",
    "dropped_fields",
    "reasoning_history_items_dropped",
    "tool_call_count",
    "terminal_reason",
    "client_disconnected",
)

#: Hard cap for any single string value written to the log (R-P2-12).
MAX_LOG_FIELD_CHARS: int = 512

#: Sentinel used for every redacted / reasoning-bearing value.
REDACTED: str = "[REDACTED]"

_TRUNCATED_SUFFIX: str = "... [truncated]"


@dataclass
class RequestLogRecord:
    """One structured request-log record (the 14-field schema).

    ``dropped_fields`` is a list of **field names** dropped by the sanitizer
    (never their values), so it is safe to log as-is after per-element
    sanitisation.
    """

    request_id: str = ""
    session_id_hash: str = ""
    model: str = ""
    upstream_protocol: str = ""
    upstream_key_id: str = ""
    stream: bool = False
    attempt: int = 1
    first_token_ms: float | None = None
    duration_ms: float | None = None
    dropped_fields: list[str] = field(default_factory=list)
    reasoning_history_items_dropped: int = 0
    tool_call_count: int = 0
    terminal_reason: str = ""
    client_disconnected: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Plain dict with exactly the 14 canonical keys."""
        return {name: getattr(self, name) for name in REQUEST_LOG_FIELDS}


# ---------------------------------------------------------------------------
# 2. Redaction
# ---------------------------------------------------------------------------

#: Reasoning-ish JSON keys whose **string values** are replaced wholesale.
#: The keys themselves are kept (they are metadata) but the content is not.
_REASONING_KEY_PATTERN = re.compile(
    r'(?i)("(?:reasoning[a-z_]*|summary_text)"\s*:\s*)(\s*")((?:\\.|[^"\\])*)(")',
)

#: Reasoning-discourse markers.  The log schema has **no** field whose string
#: value legitimately contains these words (the only reasoning field is the
#: integer ``reasoning_history_items_dropped``), so a string value that carries
#: one is by definition reasoning content and is collapsed wholesale (R-P1-55:
#: the complete reasoning text must never be written).
_REASONING_DISCOURSE = re.compile(r"(?i)\breason(?:ing|ed|s|es|able)?\b")


def redact(value: str) -> str:
    """Redact credentials / Authorization / JWT from ``value``.

    Delegates to :func:`zhongzhuan.proxy.protocol.responses_errors.redact` so
    there is exactly one source of truth for secret patterns across the proxy
    (the same patterns R-P1-52 already tests).
    """
    return _redact_sensitive(value)


def redact_reasoning(value: str) -> str:
    """Redact reasoning content so the full reasoning text is never written.

    Two layers:

    1. JSON reasoning-keyed string values (``"reasoning_summary_text": "..."``)
       keep their key but lose their content;
    2. any value that *is* reasoning discourse (contains a reasoning marker)
       collapses to :data:`REDACTED` wholesale -- this is what catches a
       reasoning passage stuffed into an ordinary field like ``model`` or
       ``terminal_reason``.
    """
    value = _REASONING_KEY_PATTERN.sub(
        lambda m: m.group(1) + m.group(2) + REDACTED + m.group(4),
        value,
    )
    if _REASONING_DISCOURSE.search(value):
        return REDACTED
    return value


def truncate(value: str, *, max_chars: int = MAX_LOG_FIELD_CHARS) -> str:
    """Cut ``value`` to ``max_chars`` keeping a readable prefix + a marker.

    Truncation never runs before redaction in the real path
    (:func:`sanitize_text`); the standalone function is public so tests can
    verify the two steps independently (R-P2-12).
    """
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + _TRUNCATED_SUFFIX


def sanitize_text(
    value: str,
    *,
    field_name: str = "",
    max_chars: int = MAX_LOG_FIELD_CHARS,
) -> str:
    """Redact then truncate one string value (order is load-bearing).

    * reasoning-bearing fields never log their content at all;
    * every credential pattern is replaced with ``[REDACTED]`` **before**
      truncation, so a secret near the boundary cannot be cut into a
      recognisable fragment;
    * the remaining (already-clean) text is capped at ``max_chars``.
    """
    text = str(value)
    if "reasoning" in (field_name or "").lower():
        return REDACTED
    cleaned = redact(text)
    cleaned = redact_reasoning(cleaned)
    return truncate(cleaned, max_chars=max_chars)


def sanitize_value(value: Any, *, field_name: str = "", max_chars: int = MAX_LOG_FIELD_CHARS) -> Any:
    """Sanitise one record value (str / list[str] / scalar passthrough)."""
    if isinstance(value, str):
        return sanitize_text(value, field_name=field_name, max_chars=max_chars)
    if isinstance(value, list):
        return [
            sanitize_text(str(item), field_name=field_name, max_chars=max_chars)
            for item in value
        ]
    if isinstance(value, Mapping):
        return {
            str(k): sanitize_value(v, field_name=str(k), max_chars=max_chars)
            for k, v in value.items()
        }
    return value  # bool / int / float / None -- no secret material


# ---------------------------------------------------------------------------
# 3. Serialisation + emission
# ---------------------------------------------------------------------------


#: A bare record gives the canonical typed defaults for missing keys.
_DEFAULT_RECORD: RequestLogRecord = RequestLogRecord()


def _get_field(record: Any, name: str) -> Any:
    """Read ``name`` from a Mapping or a dataclass instance."""
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def sanitize_record(record: Any, *, max_chars: int = MAX_LOG_FIELD_CHARS) -> dict[str, Any]:
    """Build a sanitised dict with **exactly** the 14 canonical keys.

    ``record`` may be a :class:`RequestLogRecord` or a plain mapping.  Unknown
    keys are dropped (the schema is fixed), and every value is pushed through
    :func:`sanitize_value`.  The returned dict is what :func:`to_log_json`
    serialises -- nothing else reaches the log line.
    """
    out: dict[str, Any] = {}
    for name in REQUEST_LOG_FIELDS:
        value = _get_field(record, name)
        if value is None:
            value = _get_field(_DEFAULT_RECORD, name)
        out[name] = sanitize_value(value, field_name=name, max_chars=max_chars)
    return out


def to_log_json(record: Mapping[str, Any], *, max_chars: int = MAX_LOG_FIELD_CHARS) -> str:
    """Serialize ``record`` to a single-line JSON object with 14 keys.

    The line is already fully sanitised -- this is the only function that
    renders a request log line, so every log path goes through the redaction
    guarantee.
    """
    safe = sanitize_record(record, max_chars=max_chars)
    return json.dumps(safe, ensure_ascii=False, sort_keys=False)


def emit_request_log(
    record: Mapping[str, Any],
    *,
    logger: Callable[..., Any] | None = None,
    max_chars: int = MAX_LOG_FIELD_CHARS,
) -> str:
    """Write one structured request-log line and return it.

    ``logger`` defaults to loguru's global logger; pass a test sink to capture
    the line.  Returns the exact line written so tests can assert on it.
    """
    line = to_log_json(record, max_chars=max_chars)
    target = logger if logger is not None else _default_logger.info
    target(line)
    return line


__all__ = [
    "REQUEST_LOG_FIELDS",
    "MAX_LOG_FIELD_CHARS",
    "REDACTED",
    "RequestLogRecord",
    "redact",
    "redact_reasoning",
    "truncate",
    "sanitize_text",
    "sanitize_value",
    "sanitize_record",
    "to_log_json",
    "emit_request_log",
]
