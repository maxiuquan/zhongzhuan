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

    # OpenAI 专属路径：即使请求带了 x-api-key / anthropic-version 头
    # （很多 OpenAI 兼容客户端会同时携带），也必须按路径判定为 openai，
    # 否则 OpenAI 请求体会被误当成 Anthropic 格式转换，导致 tool 字段损坏。
    # Path 优先级必须高于 headers（见函数 docstring "Priority: path > headers"）。
    if path and (
        path == "/v1/chat/completions"
        or path.startswith("/v1/chat/completions")
        or path == "/v1/completions"
        or path.startswith("/v1/completions")
        or path == "/v1/embeddings"
        or path.startswith("/v1/embeddings")
    ):
        return "openai"

    if not headers:
        return "openai"

    # Normalize header keys to lowercase for case-insensitive lookup.
    lower_headers = {str(k).lower(): v for k, v in headers.items()}
    if lower_headers.get("x-api-key"):
        return "anthropic"
    if lower_headers.get("anthropic-version"):
        return "anthropic"

    return "openai"
