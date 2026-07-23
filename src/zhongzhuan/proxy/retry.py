"""Retry & cooldown utilities with health state machine.

Distinguishes failure types so the scheduler can skip permanently-bad keys:
  - 401/403  → invalid (long cooldown, stops wasting requests)
  - 429      → rate_limited (cooldown by Retry-After header or backoff)
  - 5xx      → error (short exponential backoff)
  - network  → error (short backoff, may be transient)
"""
from __future__ import annotations

import time

from .ratelimit import KeyHealth, SlidingWindow, STATE_HEALTHY, STATE_RATE_LIMITED, STATE_INVALID, STATE_ERROR


def cooldown_for(failures: int) -> float:
    """Return cooldown seconds based on consecutive failures (for 5xx / network)."""
    if failures <= 1:
        return 5.0
    if failures == 2:
        return 10.0
    if failures == 3:
        return 30.0
    return 60.0


def mark_auth_failure(k: KeyHealth) -> None:
    """401/403: credentials are invalid. Long cooldown to avoid wasted requests."""
    k.failure_count += 1
    k.status = STATE_INVALID
    # 1 hour: invalid keys rarely recover without manual rotation
    k.cooldown_until = time.time() + 3600.0


def mark_rate_limited(k: KeyHealth, retry_after: float = 0.0) -> None:
    """429: rate limited. Honor Retry-After header when available."""
    k.recent_429_count += 1
    k.failure_count += 1
    k.status = STATE_RATE_LIMITED
    if retry_after > 0:
        k.cooldown_until = time.time() + min(retry_after, 600.0)
    else:
        backoff = min(5.0 * (2 ** min(k.recent_429_count - 1, 5)), 300.0)
        k.cooldown_until = time.time() + backoff


def mark_server_error(k: KeyHealth) -> None:
    """5xx: upstream server error. Short exponential backoff."""
    k.failure_count += 1
    k.status = STATE_ERROR
    k.cooldown_until = time.time() + cooldown_for(k.failure_count)


def mark_network_failure(k: KeyHealth) -> None:
    """Connection error / timeout. Short backoff (often transient)."""
    k.failure_count += 1
    k.status = STATE_ERROR
    k.cooldown_until = time.time() + cooldown_for(k.failure_count)


def mark_failure(k: KeyHealth) -> None:
    """Generic failure marker (backwards-compatible → server-error treatment)."""
    mark_server_error(k)


def mark_success(k: KeyHealth) -> None:
    k.success_count += 1
    k.recent_429_count = 0
    k.status = STATE_HEALTHY
    k.cooldown_until = 0.0


def _header_get(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup."""
    target = name.lower()
    for hk, hv in headers.items():
        if hk.lower() == target:
            return hv
    return None


def learn_rate_limits(k: KeyHealth, headers: dict, status: int) -> None:
    """Tighten local rate limits based on upstream response headers.

    Many providers (OpenAI, Groq, Anthropic, …) return x-ratelimit-* headers
    on 200/429 responses. We tighten our local caps so the scheduler stops
    picking keys that are about to be rate-limited, and honor Retry-After.
    """
    # Retry-After (seconds or HTTP-date; we only handle the seconds form)
    retry_after = _header_get(headers, "retry-after")
    if retry_after:
        try:
            k.cooldown_until = time.time() + min(float(retry_after), 600.0)
        except (ValueError, TypeError):
            pass

    # OpenAI / generic style: x-ratelimit-limit-requests
    limit_req = _header_get(headers, "x-ratelimit-limit-requests")
    if limit_req:
        try:
            val = int(limit_req)
            if val > 0 and (k.rpm_limit == 0 or val < k.rpm_limit):
                k.rpm_limit = val
        except (ValueError, TypeError):
            pass

    # x-ratelimit-limit-tokens (per-minute token cap)
    limit_tokens = _header_get(headers, "x-ratelimit-limit-tokens")
    if limit_tokens:
        try:
            val = int(limit_tokens)
            if val > 0:
                if k.tpm_limit == 0 or val < k.tpm_limit:
                    k.tpm_limit = val
                if k.tpm_window is None:
                    k.tpm_window = SlidingWindow(60, val)
                else:
                    k.tpm_window.limit = val
        except (ValueError, TypeError):
            pass

    # Sync real usage from remaining counters (more accurate than our own count)
    remaining_tokens = _header_get(headers, "x-ratelimit-remaining-tokens")
    if remaining_tokens and k.tpm_window is not None and k.tpm_limit > 0:
        try:
            rem = int(remaining_tokens)
            used = k.tpm_limit - rem
            if used > 0:
                current = k.tpm_window.current_usage()
                if used > current:
                    k.tpm_window.add(used - current)
        except (ValueError, TypeError):
            pass

    remaining_req = _header_get(headers, "x-ratelimit-remaining-requests")
    if remaining_req and k.rpm_limit > 0:
        try:
            rem = int(remaining_req)
            used = k.rpm_limit - rem
            if used > 0:
                current = k.window.current_usage()
                if used > current:
                    k.window.add(used - current)
        except (ValueError, TypeError):
            pass
