"""T29 metrics tests (R-P1-53 + criterion ⑤ of the task).

Acceptance mapping
------------------
① 13 metrics exist & triggerable + /metrics snapshot .. test_13_metrics_exist_and_triggerable
                                                       test_metrics_snapshot
⑤ 10 circuit-breaker reasons + 14 error classes each    test_ten_circuit_breaker_reasons_recorded
   trigger once, metric <-> terminal_reason reverse     test_fourteen_error_classes_cross_reference
   look-up
"""
from __future__ import annotations

from zhongzhuan.observability import metrics as m
from zhongzhuan.proxy.protocol.responses_errors import (
    TERMINAL_REASON_TO_ERROR_CLASS,
    http_status_for,
)
from zhongzhuan.proxy.protocol.responses_models import (
    CIRCUIT_BREAKER_REASONS,
    ErrorClass,
    TerminalReason,
)


def _reset() -> None:
    m.reset_metrics()


def _names() -> set[str]:
    """Metric names present in the rendered snapshot (with HELP/TYPE lines)."""
    text = m.render_metrics()
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# HELP "):
            names.add(line[len("# HELP "):].split(" ")[0])
    return names


# ---------------------------------------------------------------------------
# ① R-P1-53 -- the 13 metrics exist and each can be triggered
# ---------------------------------------------------------------------------


def test_13_metrics_exist_and_triggerable():
    _reset()
    # Trigger every metric through its recording API.
    m.record_request("create", 200)
    m.record_stream_completed(TerminalReason.NORMAL_FINISH)
    m.record_stream_truncated(TerminalReason.UPSTREAM_TRUNCATED)
    m.record_unknown_param("foo")
    m.record_reasoning_dropped()
    m.record_tool_call("web_search")
    m.record_tool_call_invalid()
    m.record_duplicate_tool_chunk()
    m.record_late_chunk()
    m.observe_first_token(0.25)
    m.observe_stream_duration(3.5)
    m.record_heartbeat()
    m.record_client_disconnect()

    names = _names()
    expected = {
        "responses_requests_total",
        "responses_streams_completed_total",
        "responses_streams_truncated_total",
        "responses_unknown_params_dropped_total",
        "responses_reasoning_history_dropped_total",
        "responses_tool_calls_total",
        "responses_tool_call_json_invalid_total",
        "responses_duplicate_tool_chunks_total",
        "responses_late_chunks_total",
        "responses_first_token_seconds",
        "responses_stream_duration_seconds",
        "responses_heartbeat_total",
        "responses_client_disconnect_total",
    }
    assert expected <= names
    assert len(expected) == 13

    # Each triggered metric has a value line in the snapshot.
    text = m.render_metrics()
    assert "responses_requests_total{endpoint=\"create\",status=\"200\"} 1" in text
    assert "responses_streams_completed_total{terminal_reason=\"normal_finish\"} 1" in text
    assert "responses_streams_truncated_total{terminal_reason=\"upstream_truncated\"} 1" in text
    assert "responses_unknown_params_dropped_total{field=\"foo\"} 1" in text
    assert "responses_reasoning_history_dropped_total 1" in text
    assert "responses_tool_calls_total{tool_type=\"web_search\"} 1" in text
    assert "responses_tool_call_json_invalid_total 1" in text
    assert "responses_duplicate_tool_chunks_total 1" in text
    assert "responses_late_chunks_total 1" in text
    assert "responses_heartbeat_total 1" in text
    assert "responses_client_disconnect_total 1" in text
    # Histograms render _sum/_count.
    assert "responses_first_token_seconds_sum" in text
    assert "responses_first_token_seconds_count" in text
    assert "responses_stream_duration_seconds_sum" in text
    assert "responses_stream_duration_seconds_count" in text


def test_metrics_snapshot():
    """`/metrics` snapshot: deterministic text with HELP/TYPE for all 13."""
    _reset()
    m.record_request("create", 200)
    m.record_heartbeat()
    m.observe_first_token(0.5)
    snapshot = m.render_metrics()

    # Standard Prometheus text format markers.
    assert snapshot.startswith("# HELP ")
    assert snapshot.endswith("\n")
    for name in _names():
        assert "# HELP {0} ".format(name) in snapshot
        assert "# TYPE {0} ".format(name) in snapshot

    # Determinism: rendering twice gives identical bytes.
    assert snapshot == m.render_metrics()

    # Every line is either a comment (#) or a sample with a value.
    for line in snapshot.splitlines():
        if not line.startswith("#"):
            assert " " in line, line


def test_counter_increments_are_cumulative():
    _reset()
    m.record_request("create", 200)
    m.record_request("create", 200)
    text = m.render_metrics()
    assert "responses_requests_total{endpoint=\"create\",status=\"200\"} 2" in text


def test_histogram_buckets_are_cumulative():
    _reset()
    m.observe_first_token(0.1)
    m.observe_first_token(5.0)
    text = m.render_metrics()
    # Both observations land in the +Inf bucket.
    assert "responses_first_token_seconds_bucket{le=\"+Inf\"} 2" in text
    assert "responses_first_token_seconds_count 2" in text


# ---------------------------------------------------------------------------
# ⑤ criterion ⑤ -- 10 circuit-breaker reasons + 14 error classes each
#    trigger once, and metric <-> terminal_reason are reverse-lookable
# ---------------------------------------------------------------------------


def test_ten_circuit_breaker_reasons_recorded():
    """Each of the 10 §9.4 circuit-breaker reasons lands in
    ``streams_truncated_total`` keyed by its own terminal_reason."""
    _reset()
    assert len(CIRCUIT_BREAKER_REASONS) == 10
    for reason in CIRCUIT_BREAKER_REASONS:
        m.record_stream_truncated(reason)

    text = m.render_metrics()
    for reason in CIRCUIT_BREAKER_REASONS:
        label = "responses_streams_truncated_total{{terminal_reason=\"{0}\"}} 1".format(reason.value)
        assert label in text, "missing metric for reason {0}".format(reason.value)

    # Reverse look-up: every recorded reason is a real TerminalReason member.
    for reason in CIRCUIT_BREAKER_REASONS:
        assert isinstance(reason, TerminalReason)


def test_fourteen_error_classes_cross_reference():
    """Each of the 14 ErrorClass members can be recorded once, and the
    resulting metric is reverse-lookable by terminal_reason.

    Stream-ending classes map to ``streams_truncated_total{terminal_reason}``
    via :data:`ERROR_CLASS_TO_TERMINAL_REASON`; request-side classes have no
    terminal event and record into ``requests_total`` by HTTP status.
    """
    _reset()
    error_classes = list(ErrorClass)
    assert len(error_classes) == 14

    stream_ending: list[ErrorClass] = []
    request_side: list[ErrorClass] = []
    for err in error_classes:
        m.record_error_class(err)
        if m.terminal_reason_for_error_class(err) is not None:
            stream_ending.append(err)
        else:
            request_side.append(err)

    text = m.render_metrics()

    # Stream-ending classes: reverse-lookable by terminal_reason.
    for err in stream_ending:
        reason = m.terminal_reason_for_error_class(err)
        assert reason is not None
        assert reason in TERMINAL_REASON_TO_ERROR_CLASS
        assert "responses_streams_truncated_total{{terminal_reason=\"{0}\"}} 1".format(
            reason.value
        ) in text, "error {0} -> reason {1} missing".format(err.value, reason.value)

    # Request-side classes: recorded by HTTP status (no fabricated terminal reason).
    for err in request_side:
        assert m.terminal_reason_for_error_class(err) is None
        status = http_status_for(err)
        # Several classes share a status (e.g. four request-side classes map to
        # 400), so the counter accumulates -- assert the label line exists and
        # carries at least one occurrence.
        label = "responses_requests_total{{endpoint=\"responses\",status=\"{0}\"}}".format(status)
        line = next((ln for ln in text.splitlines() if ln.startswith(label)), None)
        assert line is not None, "request-side error {0} (status {1}) missing".format(err.value, status)
        assert int(line.split()[-1]) >= 1

    # The cross-reference is complete: every ErrorClass is accounted for.
    assert len(stream_ending) + len(request_side) == 14


def test_terminal_reason_reverse_lookup_matches_table():
    """:data:`ERROR_CLASS_TO_TERMINAL_REASON` agrees with the canonical table."""
    for err_class, reason in m.ERROR_CLASS_TO_TERMINAL_REASON.items():
        assert reason in TERMINAL_REASON_TO_ERROR_CLASS
        assert TERMINAL_REASON_TO_ERROR_CLASS[reason] is err_class
