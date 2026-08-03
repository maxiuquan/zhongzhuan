"""Inbound protocol detection by path + headers."""
from __future__ import annotations


def detect_inbound_protocol(path: str, headers: dict) -> str:
    """Return ``"anthropic"``, ``"responses"`` or ``"openai"`` based on path + headers.

    Priority: path > headers.

    Responses API (Codex) indicators:
        * path is ``/v1/responses`` or starts with ``/v1/responses/``

    Anthropic indicators:
        * path starts with ``/v1/messages``
        * header ``x-api-key`` present
        * header ``anthropic-version`` present

    Default: ``"openai"``.
    """
    if path and (path == "/v1/responses" or path.startswith("/v1/responses/")):
        return "responses"

    if path and path.startswith("/v1/messages"):
        return "anthropic"

    if not headers:
        return "openai"

    # Normalize header keys to lowercase for case-insensitive lookup.
    lower_headers = {str(k).lower(): v for k, v in headers.items()}
    if lower_headers.get("x-api-key"):
        return "anthropic"
    if lower_headers.get("anthropic-version"):
        return "anthropic"

    return "openai"
