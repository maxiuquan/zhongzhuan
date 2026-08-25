"""T29 logfields tests (R-P1-54, R-P1-55, R-P2-12).

Acceptance mapping
------------------
② 14 keys in one request-log JSON ............... test_14_fields_present_in_log_json
③ zero sensitive patterns in captured logs ..... test_logs_never_contain_sensitive_patterns
④ overlong + sensitive content truncated/redacted test_long_content_truncated_prefix_preserved
                                               test_short_sensitive_content_redacted
                                               test_redact_before_truncate_order
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest
from loguru import logger

from zhongzhuan.observability.logfields import (
    MAX_LOG_FIELD_CHARS,
    REDACTED,
    REQUEST_LOG_FIELDS,
    RequestLogRecord,
    emit_request_log,
    sanitize_text,
    sanitize_value,
    to_log_json,
    truncate,
)

# Hard-coded sensitive sample values (criterion ③: regex must never hit these).
API_KEY_SAMPLE = "sk-test-1234"
AUTH_HEADER_SAMPLE = "Authorization: Bearer t0k3n"
REASONING_SAMPLE = "The model reasoned at length about the internal design of the tokenizer, then concluded nothing."
JWT_SAMPLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzZWNyZXQifQ.abc"


@contextmanager
def _capture_loguru():
    """Capture loguru INFO output into a list of raw messages."""
    records: list[str] = []

    def _sink(message) -> None:
        records.append(str(message))

    sink_id = logger.add(_sink, format="{message}", level="INFO")
    try:
        yield records
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# ② R-P1-54 -- one request-log JSON carries exactly the 14 keys
# ---------------------------------------------------------------------------


def test_14_fields_present_in_log_json():
    assert len(REQUEST_LOG_FIELDS) == 14
    record = RequestLogRecord(
        request_id="req_1",
        session_id_hash="abc123",
        model="gpt-4o",
        upstream_protocol="openai",
        upstream_key_id="7",
        stream=True,
        attempt=2,
        first_token_ms=125,
        duration_ms=4100,
        dropped_fields=["instructions"],
        reasoning_history_items_dropped=3,
        tool_call_count=1,
        terminal_reason="normal_finish",
        client_disconnected=False,
    )
    line = to_log_json(record)
    parsed = json.loads(line)
    assert set(parsed.keys()) == set(REQUEST_LOG_FIELDS)
    assert sorted(parsed.keys()) == sorted(REQUEST_LOG_FIELDS)
    # Values survive round-trip.
    assert parsed["request_id"] == "req_1"
    assert parsed["model"] == "gpt-4o"
    assert parsed["stream"] is True
    assert parsed["attempt"] == 2
    assert parsed["dropped_fields"] == ["instructions"]


def test_to_log_json_always_has_14_keys_even_empty():
    """Even a bare record serialises with all 14 keys (missing -> defaults)."""
    line = to_log_json({})
    parsed = json.loads(line)
    assert set(parsed.keys()) == set(REQUEST_LOG_FIELDS)
    assert parsed["request_id"] == ""
    assert parsed["stream"] is False
    assert parsed["attempt"] == 1  # dataclass default


def test_request_log_record_to_dict_is_14_keys():
    record = RequestLogRecord(request_id="req_x")
    assert set(record.to_dict().keys()) == set(REQUEST_LOG_FIELDS)


# ---------------------------------------------------------------------------
# ③ R-P1-55 -- captured logs contain zero sensitive patterns
# ---------------------------------------------------------------------------


def test_logs_never_contain_sensitive_patterns():
    """A request-log stuffed with sensitive content yields zero hits of the
    hard-coded samples (API key / Authorization / reasoning full text).

    ``model`` 是标识类键：第二层推理折叠对其豁免（模型名如
    ``gpt-5.2-reasoning`` 天然带 reasoning 词根），所以这里放的是真实
    形态的模型名而非推理正文；推理正文样本走自由文本键 terminal_reason。
    """
    with _capture_loguru() as records:
        emit_request_log(
            {
                "request_id": API_KEY_SAMPLE,
                "session_id_hash": AUTH_HEADER_SAMPLE,
                "model": "gpt-5.2-reasoning",
                "upstream_protocol": "openai",
                "upstream_key_id": JWT_SAMPLE,
                "stream": True,
                "attempt": 1,
                "first_token_ms": 1,
                "duration_ms": 1,
                "dropped_fields": [API_KEY_SAMPLE, AUTH_HEADER_SAMPLE],
                "reasoning_history_items_dropped": 1,
                "tool_call_count": 1,
                "terminal_reason": REASONING_SAMPLE,
                "client_disconnected": False,
            }
        )

    assert records, "expected at least one captured log line"
    joined = "\n".join(records)

    # The three mandated sensitive families must be absent.
    assert API_KEY_SAMPLE not in joined
    assert AUTH_HEADER_SAMPLE not in joined
    assert REASONING_SAMPLE not in joined
    # A JWT (long base64url token) must be absent too.
    assert JWT_SAMPLE not in joined

    # Identifier keys survive verbatim: the model name is NOT collapsed.
    parsed = json.loads(records[-1])
    assert parsed["model"] == "gpt-5.2-reasoning"
    # Free-text keys carrying reasoning discourse collapse to [REDACTED].
    assert parsed["terminal_reason"] == REDACTED

    # Strict regex form: the sample substrings literally never appear.
    for pattern in (
        re.escape(API_KEY_SAMPLE),
        re.escape(AUTH_HEADER_SAMPLE),
        re.escape(REASONING_SAMPLE),
        re.escape(JWT_SAMPLE),
    ):
        assert re.search(pattern, joined) is None, "sensitive pattern leaked: {0}".format(pattern)


def test_emit_logs_are_valid_14_key_json():
    with _capture_loguru() as records:
        emit_request_log(RequestLogRecord(request_id="req_json"))
    assert records
    parsed = json.loads(records[-1])
    assert set(parsed.keys()) == set(REQUEST_LOG_FIELDS)


# ---------------------------------------------------------------------------
# ④ R-P2-12 -- overlong content truncated; sensitive content redacted;
#    redaction runs before truncation
# ---------------------------------------------------------------------------


def test_long_content_truncated_prefix_preserved():
    long_text = "x" * (MAX_LOG_FIELD_CHARS + 500)
    out = sanitize_text(long_text)
    assert len(out) <= MAX_LOG_FIELD_CHARS + len("... [truncated]")
    # Readable prefix preserved.
    assert out.startswith("x" * 100)
    assert out.endswith("... [truncated]")
    # Standalone truncate behaves the same.
    assert truncate(long_text).endswith("... [truncated]")
    assert truncate("short") == "short"


def test_short_sensitive_content_redacted():
    out = sanitize_text("my key is " + API_KEY_SAMPLE)
    assert API_KEY_SAMPLE not in out
    assert REDACTED in out

    out2 = sanitize_text(AUTH_HEADER_SAMPLE)
    assert "t0k3n" not in out2
    assert REDACTED in out2


def test_reasoning_text_is_redacted_not_truncated():
    """A reasoning-bearing field never logs its content, even when short."""
    out = sanitize_text(REASONING_SAMPLE, field_name="reasoning_summary_text")
    assert out == REDACTED


# ---------------------------------------------------------------------------
# 第二层推理启发式的键白名单（model/name/id 豁免，自由文本键生效）
# ---------------------------------------------------------------------------


def test_model_name_with_reasoning_marker_is_exempt():
    """模型名携带 reasoning 词根 → 原样保留，不被整段抹掉。"""
    for value in ("gpt-5.2-reasoning", "deepseek-reasoner", "o3-reasoned-mini"):
        assert sanitize_text(value, field_name="model") == value
    # 标识类键同理。
    assert sanitize_text("req-reasonable-001", field_name="request_id") == "req-reasonable-001"


@pytest.mark.parametrize(
    "field_name",
    ["terminal_reason", "cancel_reason", "message", "error"],
)
def test_free_text_fields_carrying_reasoning_collapse(field_name):
    """自由文本键的值含推理语篇标记 → 整段折叠为 [REDACTED]。"""
    out = sanitize_text(REASONING_SAMPLE, field_name=field_name)
    assert out == REDACTED


def test_free_text_field_without_reasoning_marker_survives():
    """自由文本键的正常内容（无标记）不受第二层影响。"""
    assert sanitize_text("normal_finish", field_name="terminal_reason") == "normal_finish"


def test_nested_error_message_key_is_whitelisted_via_mapping():
    """Mapping 值以子键名作 field_name：嵌套 message/error 键同样生效。"""
    out = sanitize_value({"message": REASONING_SAMPLE})
    assert out["message"] == REDACTED
    out2 = sanitize_value({"model": "gpt-5.2-reasoning"})
    assert out2["model"] == "gpt-5.2-reasoning"


# ---------------------------------------------------------------------------
# 数值直通的有限性检查（NaN/Inf -> None）
# ---------------------------------------------------------------------------


def test_non_finite_floats_become_none():
    """NaN / Inf 不是合法 JSON 数值，直通前替换为 None。"""
    assert sanitize_value(float("nan")) is None
    assert sanitize_value(float("inf")) is None
    assert sanitize_value(float("-inf")) is None
    assert sanitize_value(3.14) == 3.14
    assert sanitize_value(7) == 7  # int 不受影响
    line = to_log_json({"first_token_ms": float("nan"), "duration_ms": float("inf")})
    assert "NaN" not in line and "Infinity" not in line
    parsed = json.loads(line)
    assert parsed["first_token_ms"] is None
    assert parsed["duration_ms"] is None


def test_redact_before_truncate_order():
    """A secret sitting *inside* a long value is gone even if it would be cut."""
    # The 12-char secret starts at index 506 (after a separator) so a 512-char
    # cut would keep "sk-tes" (6 chars) -- a recognisable fragment that the
    # credential regex alone cannot fully re-redact afterwards (``{6,}`` needs
    # 6+ chars after ``sk-`` but the fragment only carries 3).  The space keeps
    # the ``\\b`` boundary intact so redact-first removes the whole secret
    # before any truncation can split it.
    value = ("a" * 505) + " " + API_KEY_SAMPLE + ("b" * 100)
    assert len(value) > MAX_LOG_FIELD_CHARS  # genuinely long
    out = sanitize_text(value)
    assert API_KEY_SAMPLE not in out
    assert "sk-tes" not in out  # the exact fragment a 512-char cut would leave
    # The readable prefix survives regardless of order.
    assert out.startswith("a" * 505)


def test_to_log_json_redacts_every_field():
    line = to_log_json(
        {
            "request_id": "id-" + API_KEY_SAMPLE,
            "model": "model " + AUTH_HEADER_SAMPLE,
            "terminal_reason": "err " + API_KEY_SAMPLE,
        }
    )
    assert API_KEY_SAMPLE not in line
    assert AUTH_HEADER_SAMPLE not in line
    assert "t0k3n" not in line
    # The output is still valid JSON with the 14 keys.
    parsed = json.loads(line)
    assert set(parsed.keys()) == set(REQUEST_LOG_FIELDS)
