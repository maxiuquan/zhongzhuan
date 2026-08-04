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


def _bump_failure(k: KeyHealth) -> None:
    """Record one failure on both counters (T07: total vs consecutive)."""
    k.total_failures += 1
    k.consecutive_failures += 1


def mark_auth_failure(k: KeyHealth) -> None:
    """401/403: credentials are invalid. Long cooldown to avoid wasted requests."""
    _bump_failure(k)
    k.status = STATE_INVALID
    # 1 hour: invalid keys rarely recover without manual rotation
    k.cooldown_until = time.time() + 3600.0


def mark_rate_limited(k: KeyHealth, retry_after: float = 0.0) -> None:
    """429: rate limited. Honor Retry-After header when available."""
    k.recent_429_count += 1
    _bump_failure(k)
    k.status = STATE_RATE_LIMITED
    if retry_after > 0:
        k.cooldown_until = time.time() + min(retry_after, 600.0)
    else:
        backoff = min(5.0 * (2 ** min(k.recent_429_count - 1, 5)), 300.0)
        k.cooldown_until = time.time() + backoff


def mark_server_error(k: KeyHealth) -> None:
    """5xx: upstream server error. Short exponential backoff."""
    _bump_failure(k)
    k.status = STATE_ERROR
    k.cooldown_until = time.time() + cooldown_for(k.consecutive_failures)


def mark_network_failure(k: KeyHealth) -> None:
    """Connection error / timeout. Short backoff (often transient)."""
    _bump_failure(k)
    k.status = STATE_ERROR
    k.cooldown_until = time.time() + cooldown_for(k.consecutive_failures)


def mark_failure(k: KeyHealth) -> None:
    """Generic failure marker (backwards-compatible → server-error treatment)."""
    mark_server_error(k)


def mark_success(k: KeyHealth) -> None:
    k.success_count += 1
    k.recent_429_count = 0
    # T07: success resets the consecutive counter but keeps the lifetime total.
    k.consecutive_failures = 0
    k.status = STATE_HEALTHY
    k.cooldown_until = 0.0


def _lower_headers(headers: dict) -> dict[str, str]:
    """Build a lowercase-keyed dict once for O(1) case-insensitive lookup."""
    return {k.lower(): v for k, v in headers.items()}


def learn_rate_limits(k: KeyHealth, headers: dict, status: int) -> None:
    """Tighten local rate limits based on upstream response headers.

    Many providers (OpenAI, Groq, Anthropic, …) return x-ratelimit-* headers
    on 200/429 responses. We tighten our local caps so the scheduler stops
    picking keys that are about to be rate-limited, and honor Retry-After.
    """
    # 一次构建 lowercase dict，避免多次 O(n) 遍历（优化点6）
    h = _lower_headers(headers)

    # Retry-After (seconds or HTTP-date; we only handle the seconds form)
    retry_after = h.get("retry-after")
    if retry_after:
        try:
            k.cooldown_until = time.time() + min(float(retry_after), 600.0)
        except (ValueError, TypeError):
            pass

    # OpenAI / generic style: x-ratelimit-limit-requests
    limit_req = h.get("x-ratelimit-limit-requests")
    if limit_req:
        try:
            val = int(limit_req)
            if val > 0 and (k.rpm_limit == 0 or val < k.rpm_limit):
                k.rpm_limit = val
        except (ValueError, TypeError):
            pass

    # x-ratelimit-limit-tokens (per-minute token cap)
    limit_tokens = h.get("x-ratelimit-limit-tokens")
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
    remaining_tokens = h.get("x-ratelimit-remaining-tokens")
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

    remaining_req = h.get("x-ratelimit-remaining-requests")
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


def classify_failure(k: KeyHealth, status_code: int, headers: dict) -> bool:
    """根据上游状态码分类失败并更新 key 健康状态（优化点2：消除 handler 重复逻辑）。

    返回 True 表示可重试（应换下一个 key），False 表示请求侧错误（应直接返回客户端）。
    """
    if status_code in (401, 403):
        mark_auth_failure(k)
        return True
    if status_code == 429:
        learn_rate_limits(k, headers, status_code)
        h = _lower_headers(headers)
        retry_after = h.get("retry-after")
        try:
            ra_sec = float(retry_after) if retry_after else 0.0
        except (ValueError, TypeError):
            ra_sec = 0.0
        mark_rate_limited(k, ra_sec)
        return True
    if status_code >= 500:
        mark_server_error(k)
        return True
    # 其他 4xx（400/404/409/413/422…）：请求侧错误，不重试，也不记健康度。
    # 用户自己发了个坏请求，不该把上游 key 标记成不健康（T07 修正历史误判）。
    return False


def reason_for_exhaustion(keys: list[KeyHealth]) -> str:
    """当所有 key 耗尽时，返回原因标签（优化点8：429 响应带 X-Zhongzhuan-Reason 头）。"""
    if not keys:
        return "no_keys"
    from .ratelimit import STATE_INVALID, STATE_RATE_LIMITED, STATE_ERROR

    has_invalid = any(k.status == STATE_INVALID for k in keys)
    has_rate_limited = any(k.status == STATE_RATE_LIMITED for k in keys)
    has_error = any(k.status == STATE_ERROR for k in keys)
    if has_invalid and not has_rate_limited and not has_error:
        return "all_invalid"
    if has_rate_limited and not has_invalid and not has_error:
        return "all_rate_limited"
    if has_error and not has_invalid and not has_rate_limited:
        return "all_error"
    return "all_exhausted"
