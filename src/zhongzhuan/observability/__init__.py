"""Observability: structured logging + Prometheus metrics + debug capture
(T29 / R-P1-53..55, R-P2-12; T30 / R-P1-56).

``metrics`` -- the 13 §11.1 metrics rendered in Prometheus text format without
the ``prometheus-client`` dependency (:func:`~.metrics.render_metrics`).

``logfields`` -- the 14-field request-log schema (:func:`~.logfields.to_log_json`)
with redaction + truncation (:func:`~.logfields.sanitize_text`).

``capture`` -- optional anonymised debug capture + offline replay
(:class:`~.capture.DebugCapture`).  Disabled by default.
"""

from .capture import (
    CaptureConfig,
    CaptureEntry,
    CaptureStats,
    DebugCapture,
    capture,
    normalize_event,
)
from .log import setup_logging
from .logfields import (
    MAX_LOG_FIELD_CHARS,
    REDACTED,
    REQUEST_LOG_FIELDS,
    RequestLogRecord,
    emit_request_log,
    redact,
    redact_reasoning,
    sanitize_record,
    sanitize_text,
    sanitize_value,
    to_log_json,
    truncate,
)
from .metrics import (
    ERROR_CLASS_TO_TERMINAL_REASON,
    ALL_METRICS,
    Counter,
    Histogram,
    client_disconnect_total,
    duplicate_tool_chunks_total,
    first_token_seconds,
    heartbeat_total,
    late_chunks_total,
    reasoning_history_dropped_total,
    record_client_disconnect,
    record_duplicate_tool_chunk,
    record_error_class,
    record_heartbeat,
    record_late_chunk,
    record_reasoning_dropped,
    record_request,
    record_stream_completed,
    record_stream_truncated,
    record_tool_call,
    record_tool_call_invalid,
    record_unknown_param,
    render_metrics,
    requests_total,
    reset_metrics,
    streams_completed_total,
    streams_truncated_total,
    stream_duration_seconds,
    terminal_reason_for_error_class,
    tool_call_json_invalid_total,
    tool_calls_total,
    unknown_params_dropped_total,
)

__all__ = [
    "setup_logging",
    # capture
    "CaptureConfig",
    "CaptureEntry",
    "CaptureStats",
    "DebugCapture",
    "capture",
    "normalize_event",
    # logfields
    "REQUEST_LOG_FIELDS",
    "MAX_LOG_FIELD_CHARS",
    "REDACTED",
    "RequestLogRecord",
    "to_log_json",
    "emit_request_log",
    "sanitize_text",
    "sanitize_value",
    "sanitize_record",
    "redact",
    "redact_reasoning",
    "truncate",
    # metrics
    "Counter",
    "Histogram",
    "ALL_METRICS",
    "requests_total",
    "streams_completed_total",
    "streams_truncated_total",
    "unknown_params_dropped_total",
    "reasoning_history_dropped_total",
    "tool_calls_total",
    "tool_call_json_invalid_total",
    "duplicate_tool_chunks_total",
    "late_chunks_total",
    "first_token_seconds",
    "stream_duration_seconds",
    "heartbeat_total",
    "client_disconnect_total",
    "render_metrics",
    "reset_metrics",
    "record_request",
    "record_stream_completed",
    "record_stream_truncated",
    "record_unknown_param",
    "record_reasoning_dropped",
    "record_tool_call",
    "record_tool_call_invalid",
    "record_duplicate_tool_chunk",
    "record_late_chunk",
    "record_heartbeat",
    "record_client_disconnect",
    "ERROR_CLASS_TO_TERMINAL_REASON",
    "terminal_reason_for_error_class",
    "record_error_class",
]
