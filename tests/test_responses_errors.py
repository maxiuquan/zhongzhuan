"""T09 unit tests: 14 error classes -> HTTP mapping, and secret redaction.

Covers acceptance criteria (1) and (3) of T09:
  (1) every one of the 14 ``ErrorClass`` members maps to the documented HTTP
      status / ``error.type`` / ``error.code`` triple (§10.2);
  (3) an upstream error message carrying an API key never reaches the
      downstream response body (R-P1-52).
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.proxy.protocol.responses_errors import (
    ERROR_SPECS,
    HTTP_CLIENT_CLOSED_REQUEST,
    MAX_MESSAGE_CHARS,
    NO_BODY_ERROR_CLASSES,
    REDACTED,
    RETRYABLE_ERROR_CLASSES,
    TERMINAL_REASON_TO_ERROR_CLASS,
    ErrorSpec,
    classify_http_status,
    error_payload,
    get_spec,
    http_status_for,
    is_retryable,
    redact,
    redact_headers,
    sanitize_message,
    to_error_response,
    to_incomplete_details,
)
from zhongzhuan.proxy.protocol.responses_models import ErrorClass, TerminalReason

# ---------------------------------------------------------------------------
# T09 criterion (1): 14 classes x (HTTP, type, code)  -- §10.2 verbatim
# ---------------------------------------------------------------------------

#: (ErrorClass, http_status, error_type, code) exactly as tabulated in §10.2.
SPEC_TABLE = [
    (ErrorClass.INVALID_CLIENT_REQUEST, 400, "invalid_request_error", "invalid_request"),
    (ErrorClass.UNSUPPORTED_INPUT_BLOCK, 400, "invalid_request_error", "unsupported_input_block"),
    (ErrorClass.UNSUPPORTED_TOOL_CAPABILITY, 400, "invalid_request_error", "unsupported_tool"),
    (ErrorClass.CAPABILITY_ROUTE_UNAVAILABLE, 503, "server_error", "capability_route_unavailable"),
    (ErrorClass.INVALID_TOOL_ARGUMENTS, 400, "invalid_request_error", "invalid_tool_arguments"),
    (ErrorClass.UPSTREAM_CONNECT_ERROR, 502, "server_error", "upstream_connect_error"),
    (ErrorClass.UPSTREAM_RATE_LIMITED, 429, "rate_limit_error", "rate_limit_exceeded"),
    (ErrorClass.UPSTREAM_SERVER_ERROR, 502, "server_error", "upstream_error"),
    (ErrorClass.FIRST_TOKEN_TIMEOUT, 504, "server_error", "first_token_timeout"),
    (ErrorClass.READ_IDLE_TIMEOUT, 504, "server_error", "read_idle_timeout"),
    (ErrorClass.UPSTREAM_TRUNCATED, 200, "", "upstream_truncated"),
    (ErrorClass.INVALID_SSE_FRAME, 502, "server_error", "invalid_sse_frame"),
    (ErrorClass.CLIENT_DISCONNECTED, 499, "", ""),
    (ErrorClass.INTERNAL_TRANSLATION_ERROR, 500, "server_error", "internal_error"),
]


def test_exactly_fourteen_error_classes():
    assert len(list(ErrorClass)) == 14
    assert set(ERROR_SPECS) == set(ErrorClass)
    assert len(SPEC_TABLE) == 14


@pytest.mark.parametrize("err_class,status,err_type,code", SPEC_TABLE)
def test_error_class_maps_to_documented_status_type_and_code(err_class, status, err_type, code):
    """T09 criterion (1): one example per class, mapping asserted."""
    spec = get_spec(err_class)
    assert isinstance(spec, ErrorSpec)
    assert spec.error_class is err_class
    assert spec.http_status == status
    assert spec.error_type == err_type
    assert spec.code == code
    assert http_status_for(err_class) == status


@pytest.mark.parametrize("err_class,status,err_type,code", SPEC_TABLE)
def test_to_error_response_shape_per_class(err_class, status, err_type, code):
    got_status, body = to_error_response(err_class, "boom while calling upstream", param="tools[2].type")
    assert got_status == status
    if err_class in NO_BODY_ERROR_CLASSES:
        assert body == {}
        return
    assert set(body) == {"error"}
    err = body["error"]
    assert set(err) == {"type", "code", "message", "param"}
    assert err["type"] == err_type
    assert err["code"] == code
    assert err["param"] == "tools[2].type"
    assert err["message"] == "boom while calling upstream"
    # must be JSON serialisable as-is
    json.dumps(body)


def test_no_body_classes_are_exactly_the_two_documented_ones():
    assert NO_BODY_ERROR_CLASSES == frozenset(
        {
            ErrorClass.UPSTREAM_TRUNCATED,
            ErrorClass.CLIENT_DISCONNECTED,
        }
    )


def test_client_disconnected_uses_499():
    assert HTTP_CLIENT_CLOSED_REQUEST == 499
    assert http_status_for(ErrorClass.CLIENT_DISCONNECTED) == 499


def test_include_body_override_forces_a_valid_envelope():
    status, body = to_error_response(ErrorClass.UPSTREAM_TRUNCATED, "stream cut", include_body=True)
    assert status == 200
    assert body["error"]["type"] == "server_error"  # non-empty fallback
    assert body["error"]["code"] == "upstream_truncated"


def test_param_omitted_becomes_null():
    _, body = to_error_response(ErrorClass.INVALID_CLIENT_REQUEST, "bad")
    assert body["error"]["param"] is None
    _, body2 = to_error_response(ErrorClass.INVALID_CLIENT_REQUEST, "bad", "")
    assert body2["error"]["param"] is None


def test_error_payload_matches_the_documented_example():
    payload = error_payload(
        ErrorClass.UNSUPPORTED_TOOL_CAPABILITY,
        "hosted tool 'web_search' is not supported by any route",
        "tools[2].type",
    )
    assert payload["type"] == "invalid_request_error"
    assert payload["code"] == "unsupported_tool"
    assert payload["param"] == "tools[2].type"


def test_retryable_whitelist_has_four_members():
    assert RETRYABLE_ERROR_CLASSES == frozenset(
        {
            ErrorClass.UPSTREAM_CONNECT_ERROR,
            ErrorClass.UPSTREAM_RATE_LIMITED,
            ErrorClass.UPSTREAM_SERVER_ERROR,
            ErrorClass.FIRST_TOKEN_TIMEOUT,
        }
    )
    assert is_retryable(ErrorClass.UPSTREAM_RATE_LIMITED) is True
    # deltas already flushed downstream -> replay forbidden (R-P0-34)
    assert is_retryable(ErrorClass.READ_IDLE_TIMEOUT) is False
    assert is_retryable(ErrorClass.INVALID_CLIENT_REQUEST) is False


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, ErrorClass.UPSTREAM_RATE_LIMITED),
        (500, ErrorClass.UPSTREAM_SERVER_ERROR),
        (502, ErrorClass.UPSTREAM_SERVER_ERROR),
        (529, ErrorClass.UPSTREAM_SERVER_ERROR),
        (400, ErrorClass.INVALID_CLIENT_REQUEST),
        (401, ErrorClass.INVALID_CLIENT_REQUEST),
        (422, ErrorClass.INVALID_CLIENT_REQUEST),
    ],
)
def test_classify_http_status(status, expected):
    assert classify_http_status(status) is expected


def test_terminal_reason_to_error_class_targets_are_valid():
    for reason, err_class in TERMINAL_REASON_TO_ERROR_CLASS.items():
        assert isinstance(reason, TerminalReason)
        assert err_class in ERROR_SPECS
    assert TERMINAL_REASON_TO_ERROR_CLASS[TerminalReason.UPSTREAM_TRUNCATED] is ErrorClass.UPSTREAM_TRUNCATED
    assert TerminalReason.NORMAL_FINISH not in TERMINAL_REASON_TO_ERROR_CLASS


def test_to_incomplete_details():
    assert to_incomplete_details(TerminalReason.UPSTREAM_TRUNCATED) == {"reason": "upstream_truncated"}
    assert to_incomplete_details("max_tool_rounds") == {"reason": "max_tool_rounds"}
    details = to_incomplete_details(TerminalReason.MAX_OUTPUT_BUDGET, "budget hit at 200000 tokens")
    assert details["reason"] == "max_output_budget"
    assert details["message"] == "budget hit at 200000 tokens"


def test_get_spec_degrades_instead_of_raising():
    spec = get_spec("not-an-error-class")  # type: ignore[arg-type]
    assert spec.error_class is ErrorClass.INTERNAL_TRANSLATION_ERROR


# ---------------------------------------------------------------------------
# T09 criterion (3): redaction  (R-P1-52)
# ---------------------------------------------------------------------------

LEAKY_SECRETS = [
    "sk-proj-abc123DEF456ghi789jkl012MNO",
    "sk-ant-api03-Zx9yWv8uTs7rQp6oNm5lKj4i",
    "sk-abcdefghijklmnop",
    "AIzaSyD-1234567890abcdefghijklmnopqrst",
    "ghp_16CharsAndThenSomeMoreChars0000",
    "gsk_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    "xai-9876543210abcdefghijklmnop",
    "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
]


@pytest.mark.parametrize("secret", LEAKY_SECRETS)
def test_redact_removes_known_key_shapes(secret):
    text = "upstream rejected the request using key {0} at 12:00".format(secret)
    out = redact(text)
    assert secret not in out
    assert REDACTED in out


def test_redact_removes_authorization_header():
    text = 'Upstream 401: {"headers": {"Authorization": "Bearer sk-proj-LEAK1234"}}'
    out = redact(text)
    assert "sk-proj-LEAK1234" not in out
    assert "Bearer sk-" not in out


def test_redact_removes_x_api_key_header():
    text = "x-api-key: sk-ant-api03-SUPERSECRETVALUE99"
    out = redact(text)
    assert "SUPERSECRETVALUE99" not in out
    assert REDACTED in out


def test_redact_removes_json_api_key_field():
    text = '{"api_key": "MY-PLAINTEXT-KEY-000111", "model": "gpt-4o"}'
    out = redact(text)
    assert "MY-PLAINTEXT-KEY-000111" not in out
    assert "gpt-4o" in out  # non-secret content survives


def test_redact_is_idempotent_and_total():
    text = "Bearer sk-proj-abc123DEF456ghi"
    once = redact(text)
    assert redact(once) == once


def test_redact_handles_empty_and_non_string():
    assert redact("") == ""
    assert redact(None) == ""  # type: ignore[arg-type]
    assert redact(12345) == "12345"  # type: ignore[arg-type]


def test_redact_preserves_ordinary_text():
    text = "model 'gpt-4o-mini' is not available for this workspace"
    assert redact(text) == text


def test_sanitize_message_truncates_long_upstream_echoes():
    """Bounds leakage of reasoning / tool output echoed back by the upstream."""
    long_text = "R" * (MAX_MESSAGE_CHARS * 3)
    out = sanitize_message(long_text)
    assert len(out) <= MAX_MESSAGE_CHARS + len("... [truncated]")
    assert out.endswith("... [truncated]")


def test_sanitize_message_collapses_whitespace():
    assert sanitize_message("  a\n\n b\t c  ") == "a b c"


def test_redact_headers_masks_credentials():
    out = redact_headers(
        {
            "Authorization": "Bearer sk-proj-abc123DEF456",
            "X-Api-Key": "sk-ant-api03-XYZ987654321",
            "Content-Type": "application/json",
        }
    )
    assert out["Authorization"] == REDACTED
    assert out["X-Api-Key"] == REDACTED
    assert out["Content-Type"] == "application/json"
    assert redact_headers(None) == {}


def test_upstream_error_carrying_a_key_never_reaches_the_client():
    """T09 criterion (3), end to end through the public constructor."""
    api_key = "sk-proj-abc123DEF456ghi789jkl012MNO"
    upstream_message = (
        "Incorrect API key provided: {0}. "
        "You can find your API key at https://platform.openai.com/account/api-keys. "
        "(request headers: Authorization: Bearer {0})"
    ).format(api_key)

    status, body = to_error_response(ErrorClass.UPSTREAM_SERVER_ERROR, upstream_message)
    serialized = json.dumps(body, ensure_ascii=False)

    assert status == 502
    assert api_key not in serialized
    assert "abc123DEF456ghi789jkl012MNO" not in serialized
    assert "Bearer sk-" not in serialized
    assert REDACTED in body["error"]["message"]
    # the diagnosable, non-secret part survives
    assert "Incorrect API key provided" in body["error"]["message"]


def test_reasoning_text_is_not_echoed_verbatim_in_error_bodies():
    reasoning = "Let me think step by step. " * 60  # > MAX_MESSAGE_CHARS
    _, body = to_error_response(
        ErrorClass.INTERNAL_TRANSLATION_ERROR,
        "translation failed on reasoning item: " + reasoning,
    )
    message = body["error"]["message"]
    assert len(message) <= MAX_MESSAGE_CHARS + len("... [truncated]")
    assert message.endswith("... [truncated]")
    assert reasoning.strip() not in message


@pytest.mark.parametrize("err_class,_status,_type,_code", SPEC_TABLE)
def test_every_class_redacts_its_message(err_class, _status, _type, _code):
    secret = "sk-proj-LEAKLEAKLEAK0001"
    _, body = to_error_response(
        err_class,
        "upstream said: bad key {0}".format(secret),
        include_body=True,
    )
    assert secret not in json.dumps(body)
