"""Shared production request fact model for v3 capability routing."""

from __future__ import annotations

from zhongzhuan.proxy.protocol.responses_models import Capability
from zhongzhuan.responses_v3.request_sanitizer import (
    RequestSanitizer,
    capability_values,
)


def test_empty_or_non_mapping_payload_has_no_requirements():
    sanitizer = RequestSanitizer()

    assert sanitizer.sanitize(None).payload == {}
    assert sanitizer.sanitize(None).required_capabilities == frozenset()


def test_hosted_tools_keep_original_index_and_alias_capability():
    payload = {
        "tools": [
            {"type": "function", "name": "plain"},
            "ignored",
            {"type": "web_search_preview_2025_03_11"},
            {"type": "code_interpreter"},
        ]
    }

    request = RequestSanitizer().sanitize(payload)

    assert [spec.tool_type for spec in request.hosted_tools] == [
        "web_search_preview_2025_03_11",
        "code_interpreter",
    ]
    assert [spec.param_path for spec in request.hosted_tools] == [
        "tools[2].type",
        "tools[3].type",
    ]
    assert request.required_capabilities == frozenset(
        {Capability.WEB_SEARCH, Capability.CODE_INTERPRETER}
    )


def test_background_metadata_and_previous_response_are_capabilities():
    payload = {
        "background": True,
        "metadata": {"stateful_responses": True},
        "previous_response_id": " resp_parent ",
    }

    request = RequestSanitizer().sanitize(payload)

    assert request.required_capabilities == frozenset(
        {Capability.BACKGROUND, Capability.STATEFUL_RESPONSES}
    )
    assert capability_values(request) == frozenset(
        {"background", "stateful_responses"}
    )


def test_capability_booleans_are_strict_and_blank_previous_id_is_ignored():
    payload = {
        "background": 1,
        "metadata": {"stateful_responses": "true"},
        "previous_response_id": "   ",
    }

    assert RequestSanitizer().sanitize(payload).required_capabilities == frozenset()


def test_payload_is_top_level_copy_without_silent_field_drops():
    nested = {"tenant": "acme"}
    payload = {
        "model": "gpt-test",
        "metadata": nested,
        "unknown_future_field": {"kept": True},
    }

    request = RequestSanitizer().sanitize(payload)

    assert request.payload == payload
    assert request.payload is not payload
    assert request.payload["metadata"] is nested
    assert request.dropped_fields == []
    assert request.warnings == []
