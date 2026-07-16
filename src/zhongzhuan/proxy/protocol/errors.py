"""Error envelope translation between OpenAI and Anthropic formats."""
from __future__ import annotations

# HTTP status -> OpenAI error type
MAP_STATUS_A2O: dict[int, str] = {
    400: "invalid_request_error",
    401: "invalid_request_error",
    403: "invalid_request_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    503: "api_error",
    529: "api_error",
}

# HTTP status -> Anthropic error type
MAP_STATUS_O2A: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def _lookup_type(mapping: dict[int, str], status: int, default: str) -> str:
    return mapping.get(status, default)


def translate_error_a2o(
    status: int, message: str, inbound_protocol: str = "anthropic"
) -> tuple[int, dict]:
    """Translate an error to OpenAI error envelope format.

    Returns ``(status, error_body)`` where ``error_body`` is the dict to
    json-serialize as the response body.

    ``inbound_protocol`` is accepted for API symmetry but does not change the
    output format — callers use this when the *outbound* target is OpenAI,
    regardless of inbound protocol.
    """
    err_type = _lookup_type(MAP_STATUS_A2O, status, "api_error")
    body = {
        "error": {
            "message": message,
            "type": err_type,
            "param": None,
            "code": None,
        }
    }
    return status, body


def translate_error_o2a(
    status: int, message: str, inbound_protocol: str = "openai"
) -> tuple[int, dict]:
    """Translate an error to Anthropic error envelope format.

    Returns ``(status, error_body)`` where ``error_body`` is the dict to
    json-serialize as the response body.
    """
    err_type = _lookup_type(MAP_STATUS_O2A, status, "api_error")
    body = {
        "type": "error",
        "error": {
            "type": err_type,
            "message": message,
        },
    }
    return status, body
