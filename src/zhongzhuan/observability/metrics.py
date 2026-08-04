"""Prometheus-compatible metrics registry (T29 / R-P1-53).

Implements the 13 metrics of §11.1 / R-P1-53 **without** the
``prometheus-client`` dependency.  The registry renders the standard Prometheus
text exposition format (``# HELP`` / ``# TYPE`` / ``name{label="v"} value``),
so a future ``/metrics`` route (T33 / R-P2-09) can serve :func:`render_metrics`
verbatim and Prometheus will scrape it.

Why not ``prometheus-client``
-----------------------------
It is an optional extra in ``pyproject.toml`` and is **not** installed in the
current ``.venv3``.  Making the module import it unconditionally would break
every environment that did not install the extra.  The 13 metrics are simple
counters + two histograms; a self-contained implementation (~200 lines) with a
``threading.Lock`` gives byte-for-byte standard text output and keeps the whole
test suite green without the dependency (T27 handled ``mcp`` the same way).

Counters and histograms only
----------------------------
§11.1 lists exactly 11 counters and 2 histograms -- no gauges.  Every metric is
exposed as a module-level singleton; the trigger functions are the *recording
API* the pipeline / handler will call.  Criterion ⑤ ("10 类熔断原因 + 14 类错误
分类各触发一次，指标与 terminal_reason 可反查") is served by
:data:`ERROR_CLASS_TO_TERMINAL_REASON` + :func:`record_stream_truncated`.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from ..proxy.protocol.responses_errors import (
    TERMINAL_REASON_TO_ERROR_CLASS,
    http_status_for,
)
from ..proxy.protocol.responses_models import ErrorClass, TerminalReason

# ---------------------------------------------------------------------------
# 1. Primitive metric types
# ---------------------------------------------------------------------------


def _escape_label(value: Any) -> str:
    """Escape a label value for the Prometheus text format."""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _BaseMetric:
    """Shared rendering helpers for counters and histograms."""

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames: tuple[str, ...] = tuple(labelnames)
        self._lock = threading.Lock()

    # -- interface implemented by subclasses -----------------------------

    def render(self) -> list[str]:
        """Render this metric's lines in Prometheus text exposition format."""
        raise NotImplementedError

    def reset(self) -> None:
        """Zero this metric (test isolation / config reload)."""
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------

    def _label_key(self, labels: Mapping[str, Any]) -> tuple[str, ...]:
        unknown = [k for k in labels if k not in self.labelnames]
        if unknown:
            raise KeyError(
                "{0}: unknown label(s) {1}; expected {2}".format(
                    self.name,
                    sorted(unknown),
                    list(self.labelnames),
                )
            )
        missing = [k for k in self.labelnames if k not in labels]
        if missing:
            raise KeyError("{0}: missing label(s) {1}".format(self.name, sorted(missing)))
        # Prometheus output is sorted by label name for stable snapshots.
        return tuple(labels[k] for k in sorted(self.labelnames))

    @staticmethod
    def _render_labels(labelnames: Sequence[str], values: Sequence[Any]) -> str:
        if not labelnames:
            return ""
        pairs = ['{0}="{1}"'.format(name, _escape_label(value)) for name, value in zip(labelnames, values)]
        return "{" + ",".join(pairs) + "}"

    def _header(self, kind: str) -> list[str]:
        return [
            "# HELP {0} {1}".format(self.name, self.help_text),
            "# TYPE {0} {1}".format(self.name, kind),
        ]


class Counter(_BaseMetric):
    """A monotonic counter with optional labels."""

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, value: float = 1.0, **labels: Any) -> None:
        """Increment the counter by ``value`` for the given label set."""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def render(self) -> list[str]:
        lines = self._header("counter")
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            lines.append(
                "{0}{1} {2}".format(
                    self.name,
                    self._render_labels(sorted(self.labelnames), key),
                    _format_number(value),
                )
            )
        return lines

    def reset(self) -> None:
        with self._lock:
            self._values.clear()


class Histogram(_BaseMetric):
    """A Prometheus histogram (cumulative buckets + sum + count)."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: Sequence[float] = (),
        labelnames: Sequence[str] = (),
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self._buckets: tuple[float, ...] = (
            tuple(float(b) for b in buckets)
            if buckets
            else (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
        )
        # key -> [bucket counts (len buckets+1, +Inf last), sum, count]
        self._data: dict[tuple[str, ...], list[float]] = {}

    def observe(self, value: float, **labels: Any) -> None:
        """Record one observation."""
        key = self._label_key(labels)
        v = float(value)
        with self._lock:
            entry = self._data.setdefault(key, [0.0] * (len(self._buckets) + 1) + [0.0, 0.0])
            for i, upper in enumerate(self._buckets):
                if v <= upper:
                    entry[i] += 1.0
            entry[len(self._buckets)] += 1.0  # +Inf bucket
            entry[len(self._buckets) + 1] += v  # sum
            entry[len(self._buckets) + 2] += 1.0  # count

    def render(self) -> list[str]:
        lines = self._header("histogram")
        names = sorted(self.labelnames)
        with self._lock:
            items = sorted(self._data.items())
        for key, entry in items:
            base = self.name + self._render_labels(names, key)
            for i, upper in enumerate(self._buckets):
                lines.append(
                    '{0}_bucket{{le="{1}"}} {2}'.format(
                        base,
                        _escape_label(upper),
                        _format_number(entry[i]),
                    )
                )
            lines.append(
                '{0}_bucket{{le="+Inf"}} {1}'.format(
                    base,
                    _format_number(entry[len(self._buckets)]),
                )
            )
            lines.append("{0}_sum {1}".format(base, _format_number(entry[len(self._buckets) + 1])))
            lines.append("{0}_count {1}".format(base, _format_number(entry[len(self._buckets) + 2])))
        return lines

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


def _format_number(value: float) -> str:
    """Render a number Prometheus-style (int when integral, else repr)."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


# ---------------------------------------------------------------------------
# 2. The 13 metrics (§11.1 / R-P1-53)
# ---------------------------------------------------------------------------


#: Counter: every Responses request, by endpoint + final HTTP status.
requests_total = Counter(
    "responses_requests_total",
    "Responses API requests by endpoint and HTTP status.",
    labelnames=("endpoint", "status"),
)

#: Counter: streams that ended gracefully, keyed by terminal_reason.
streams_completed_total = Counter(
    "responses_streams_completed_total",
    "Responses streams that completed, by terminal_reason.",
    labelnames=("terminal_reason",),
)

#: Counter: streams truncated mid-flight, keyed by terminal_reason.
streams_truncated_total = Counter(
    "responses_streams_truncated_total",
    "Responses streams truncated before completion, by terminal_reason.",
    labelnames=("terminal_reason",),
)

#: Counter: unknown request params dropped by the sanitizer, by field name.
unknown_params_dropped_total = Counter(
    "responses_unknown_params_dropped_total",
    "Unknown request parameters dropped by the sanitizer, by field.",
    labelnames=("field",),
)

#: Counter: reasoning items dropped from the upstream payload (铁律 1).
reasoning_history_dropped_total = Counter(
    "responses_reasoning_history_dropped_total",
    "Reasoning history items dropped before reaching the upstream.",
)

#: Counter: tool calls observed, by tool type.
tool_calls_total = Counter(
    "responses_tool_calls_total",
    "Tool calls observed during streaming, by tool type.",
    labelnames=("tool_type",),
)

#: Counter: tool call JSON fragments that failed to parse.
tool_call_json_invalid_total = Counter(
    "responses_tool_call_json_invalid_total",
    "Tool call argument fragments that were not valid JSON.",
)

#: Counter: duplicate tool chunks dropped by the accumulator (§9.3).
duplicate_tool_chunks_total = Counter(
    "responses_duplicate_tool_chunks_total",
    "Duplicate tool call chunks dropped by the accumulator.",
)

#: Counter: deltas arriving after a terminal event.
late_chunks_total = Counter(
    "responses_late_chunks_total",
    "Upstream chunks arriving after the stream already ended.",
)

#: Histogram: seconds between request start and the first output token.
first_token_seconds = Histogram(
    "responses_first_token_seconds",
    "Seconds from request start to the first output token.",
)

#: Histogram: total wall-clock duration of a stream.
stream_duration_seconds = Histogram(
    "responses_stream_duration_seconds",
    "Total wall-clock seconds of a response stream.",
)

#: Counter: SSE comment heartbeats emitted (R-P0-21).
heartbeat_total = Counter(
    "responses_heartbeat_total",
    "SSE comment heartbeats emitted to keep the connection alive.",
)

#: Counter: client disconnects (never penalises the upstream key, R-P1-25).
client_disconnect_total = Counter(
    "responses_client_disconnect_total",
    "Client disconnects observed (no key-health penalty).",
)

#: Counter: v3 requests that fell back to the legacy path, by reason
#: (R-P0-25: ``all_keys_excluded`` when every candidate key is excluded by the
#: key rollout).  Emitted at the single v2/v3 fork point (T22 / R-P0-22).
v3_fallback_total = Counter(
    "responses_v3_fallback_total",
    "Responses requests that fell back to the legacy v2 path, by reason.",
    labelnames=("reason",),
)


#: Every §11.1 metric in a stable order (used by :func:`render_metrics`).
ALL_METRICS: tuple[_BaseMetric, ...] = (
    requests_total,
    streams_completed_total,
    streams_truncated_total,
    unknown_params_dropped_total,
    reasoning_history_dropped_total,
    tool_calls_total,
    tool_call_json_invalid_total,
    duplicate_tool_chunks_total,
    late_chunks_total,
    first_token_seconds,
    stream_duration_seconds,
    heartbeat_total,
    client_disconnect_total,
    v3_fallback_total,
)


# ---------------------------------------------------------------------------
# 3. Rendering
# ---------------------------------------------------------------------------


def render_metrics() -> str:
    """Render every metric in Prometheus text exposition format.

    Output is deterministic (sorted by metric name, then label set), so a
    ``/metrics`` snapshot test can compare byte-for-byte.
    """
    lines: list[str] = []
    for metric in sorted(ALL_METRICS, key=lambda m: m.name):
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """Zero every metric (test isolation / config reload)."""
    for metric in ALL_METRICS:
        metric.reset()


# ---------------------------------------------------------------------------
# 4. Recording API (the trigger path for the pipeline / handler)
# ---------------------------------------------------------------------------


def record_request(endpoint: str, status: int) -> None:
    """Record one ``responses_requests_total`` event."""
    requests_total.inc(endpoint=endpoint, status=str(status))


def record_stream_completed(reason: TerminalReason | str) -> None:
    """Record a graceful stream end, keyed by ``terminal_reason``."""
    streams_completed_total.inc(terminal_reason=_reason_value(reason))


def record_stream_truncated(reason: TerminalReason | str) -> None:
    """Record a truncated stream, keyed by ``terminal_reason`` (criterion ⑤)."""
    streams_truncated_total.inc(terminal_reason=_reason_value(reason))


def record_unknown_param(field: str) -> None:
    unknown_params_dropped_total.inc(field=field)


def record_reasoning_dropped() -> None:
    reasoning_history_dropped_total.inc()


def record_tool_call(tool_type: str) -> None:
    tool_calls_total.inc(tool_type=tool_type)


def record_tool_call_invalid() -> None:
    tool_call_json_invalid_total.inc()


def record_duplicate_tool_chunk() -> None:
    duplicate_tool_chunks_total.inc()


def record_late_chunk() -> None:
    late_chunks_total.inc()


def observe_first_token(seconds: float) -> None:
    first_token_seconds.observe(seconds)


def observe_stream_duration(seconds: float) -> None:
    stream_duration_seconds.observe(seconds)


def record_heartbeat() -> None:
    heartbeat_total.inc()


def record_client_disconnect() -> None:
    client_disconnect_total.inc()


def record_v3_fallback(reason: str) -> None:
    """Record one ``responses_v3_fallback_total`` event (T22 fork point)."""
    v3_fallback_total.inc(reason=reason)


def _reason_value(reason: TerminalReason | str) -> str:
    return reason.value if isinstance(reason, TerminalReason) else str(reason)


# ---------------------------------------------------------------------------
# 5. Error class -> terminal_reason cross-reference (criterion ⑤)
# ---------------------------------------------------------------------------

#: Reverse of :data:`TERMINAL_REASON_TO_ERROR_CLASS`: every ErrorClass that has
#: a canonical terminal outcome maps back to its ``TerminalReason``.  Request-side
#: classes (invalid_client_request / unsupported_input_block / invalid_sse_frame /
#: invalid_tool_arguments / internal_translation_error / unsupported_tool_capability)
#: have **no** terminal event and therefore no entry here -- they surface as an
#: HTTP status via :func:`http_status_for` instead.
ERROR_CLASS_TO_TERMINAL_REASON: dict[ErrorClass, TerminalReason] = {}
for _reason, _err_class in TERMINAL_REASON_TO_ERROR_CLASS.items():
    ERROR_CLASS_TO_TERMINAL_REASON.setdefault(_err_class, _reason)


def terminal_reason_for_error_class(err_class: ErrorClass) -> TerminalReason | None:
    """The canonical ``TerminalReason`` for ``err_class``, or ``None`` when the
    class is request-side (no terminal event)."""
    return ERROR_CLASS_TO_TERMINAL_REASON.get(err_class)


def record_error_class(err_class: ErrorClass) -> None:
    """Record one occurrence of ``err_class``.

    Stream-ending classes record into ``streams_truncated_total{terminal_reason}``
    so criterion ⑤ can reverse-look-up the reason; request-side classes record
    into ``requests_total`` with their HTTP status.
    """
    reason = terminal_reason_for_error_class(err_class)
    if reason is not None:
        record_stream_truncated(reason)
    else:
        record_request(endpoint="responses", status=http_status_for(err_class))


__all__ = [
    "Counter",
    "Histogram",
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
    "v3_fallback_total",
    "ALL_METRICS",
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
    "observe_first_token",
    "observe_stream_duration",
    "record_heartbeat",
    "record_client_disconnect",
    "ERROR_CLASS_TO_TERMINAL_REASON",
    "terminal_reason_for_error_class",
    "record_error_class",
]
