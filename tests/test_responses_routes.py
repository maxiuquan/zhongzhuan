"""T15 tests: responses endpoint routing (§4.2.2).

Covers the six official Responses resource endpoints and the 405 behavior for
unmatched methods/paths:
1. POST /v1/responses -> CREATE
2. GET /v1/responses/{id} -> RETRIEVE
3. DELETE /v1/responses/{id} -> DELETE
4. POST /v1/responses/{id}/cancel -> CANCEL
5. POST /v1/responses/compact -> COMPACT
6. GET /v1/responses/{id}/input_items -> INPUT_ITEMS
7. Wrong methods / unknown sub-resources -> 405 (endpoint=None).
"""

from __future__ import annotations

import pytest

from zhongzhuan.proxy.protocol.responses_models import ResponsesEndpoint
from zhongzhuan.proxy.protocol.responses_routes import (
    is_responses_path,
    method_allows,
    resolve_responses_endpoint,
)


def test_create():
    r = resolve_responses_endpoint("POST", "/v1/responses")
    assert r.endpoint == ResponsesEndpoint.CREATE
    assert r.response_id == ""


def test_retrieve():
    r = resolve_responses_endpoint("GET", "/v1/responses/resp_123")
    assert r.endpoint == ResponsesEndpoint.RETRIEVE
    assert r.response_id == "resp_123"


def test_delete():
    r = resolve_responses_endpoint("DELETE", "/v1/responses/resp_123")
    assert r.endpoint == ResponsesEndpoint.DELETE
    assert r.response_id == "resp_123"


def test_cancel():
    r = resolve_responses_endpoint("POST", "/v1/responses/resp_123/cancel")
    assert r.endpoint == ResponsesEndpoint.CANCEL
    assert r.response_id == "resp_123"


def test_compact():
    r = resolve_responses_endpoint("POST", "/v1/responses/compact")
    assert r.endpoint == ResponsesEndpoint.COMPACT
    assert r.response_id == ""


def test_input_items():
    r = resolve_responses_endpoint("GET", "/v1/responses/resp_123/input_items")
    assert r.endpoint == ResponsesEndpoint.INPUT_ITEMS
    assert r.response_id == "resp_123"


# ---------------------------------------------------------------------------
# 405 behavior
# ---------------------------------------------------------------------------


def test_get_create_is_405():
    r = resolve_responses_endpoint("GET", "/v1/responses")
    assert r.endpoint is None
    assert r.reason == "method_not_allowed"


def test_post_retrieve_is_405():
    r = resolve_responses_endpoint("POST", "/v1/responses/resp_123")
    assert r.endpoint is None


def test_get_cancel_is_405():
    r = resolve_responses_endpoint("GET", "/v1/responses/resp_123/cancel")
    assert r.endpoint is None


def test_unknown_subresource_is_405():
    r = resolve_responses_endpoint("GET", "/v1/responses/resp_123/foo")
    assert r.endpoint is None
    assert r.reason == "unknown_responses_subresource"


def test_non_responses_path_is_not_matched():
    r = resolve_responses_endpoint("POST", "/v1/chat/completions")
    assert r.endpoint is None
    assert r.reason == "not_responses_path"


def test_unsupported_method_is_405():
    r = resolve_responses_endpoint("PATCH", "/v1/responses/resp_123")
    assert r.endpoint is None
    assert r.reason == "method_not_allowed"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_is_responses_path():
    assert is_responses_path("/v1/responses")
    assert is_responses_path("/v1/responses/resp_1")
    assert is_responses_path("/v1/responses/resp_1/cancel")
    assert not is_responses_path("/v1/chat/completions")
    assert not is_responses_path("/v1/models")


def test_method_allows_matrix():
    assert method_allows(ResponsesEndpoint.CREATE, "POST")
    assert method_allows(ResponsesEndpoint.RETRIEVE, "GET")
    assert method_allows(ResponsesEndpoint.DELETE, "DELETE")
    assert method_allows(ResponsesEndpoint.CANCEL, "POST")
    assert method_allows(ResponsesEndpoint.COMPACT, "POST")
    assert method_allows(ResponsesEndpoint.INPUT_ITEMS, "GET")
    assert not method_allows(ResponsesEndpoint.CREATE, "GET")
    assert not method_allows(ResponsesEndpoint.RETRIEVE, "POST")
