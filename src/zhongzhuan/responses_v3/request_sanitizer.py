"""Build the shared v3 request fact model used by routing and sticky binding.

This module intentionally performs only lossless, request-level fact extraction.
Provider-specific allowlisting and input-item normalization remain in their
existing translators; this layer must not silently drop fields before the
capability router has seen the original request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..proxy.protocol.responses_models import Capability, SanitizedRequest
from .hosted_tools import HostedToolRecognizer


class RequestSanitizer:
    """Construct one :class:`SanitizedRequest` from an inbound JSON object.

    The payload gets a shallow top-level copy so adding or replacing an
    upstream-only field cannot mutate ``RequestContext.body``. Nested values are
    preserved exactly; deeper normalization belongs to the protocol translators.
    """

    def __init__(self, recognizer: HostedToolRecognizer | None = None) -> None:
        self._recognizer = recognizer or HostedToolRecognizer()

    def sanitize(self, payload: Mapping[str, Any] | None) -> SanitizedRequest:
        source: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        sanitized_payload = dict(source)
        hosted_tools = self._recognizer.recognize(source)
        required = set(self._recognizer.required_capabilities(hosted_tools))

        if source.get("background") is True:
            required.add(Capability.BACKGROUND)

        metadata = source.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("stateful_responses") is True:
            required.add(Capability.STATEFUL_RESPONSES)

        previous_response_id = source.get("previous_response_id")
        if isinstance(previous_response_id, str) and previous_response_id.strip():
            required.add(Capability.STATEFUL_RESPONSES)

        return SanitizedRequest(
            payload=sanitized_payload,
            hosted_tools=hosted_tools,
            required_capabilities=frozenset(required),
        )


def capability_values(request: SanitizedRequest) -> frozenset[str]:
    """Return the legacy string snapshot used by T35 route bindings."""

    return frozenset(capability.value for capability in request.required_capabilities)


__all__ = ["RequestSanitizer", "capability_values"]
