"""Retry & cooldown utilities with health state machine.

Distinguishes failure types so the scheduler can skip permanently-bad keys:
  - 401/402/欠费/model_not_found → permanent (invalid until manual confirm)
  - 403 CF/WAF               → banned (long cooldown, auto-recover, manual early)
  - 429                      → rate_limited (cooldown by Retry-After or backoff)
  - 5xx / network / timeout  → transient (short exponential backoff 5s→10s→60s→600s)
  - 其他 4xx                  → no_retry (请求侧问题，换 key 无意义)
  - 规则盲区                  → unknown (异步交给 agnes 补判，不阻塞)
"""

from __future__ import annotations

import time

from .ratelimit import (
    KeyHealth,
    SlidingWindow,
    STATE_HEALTHY,
    STATE_RATE_LIMITED,
    STATE_INVALID,
    STATE_ERROR,
    CLASS_PERMANENT,
    CLASS_BANNED,
    CLASS_RATE_LIMIT,
    CLASS_TRANSIENT,
    CLASS_NO_RETRY,
    CLASS_UNKNOWN,
    backoff_seconds,
)


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


#: 永久失效的关键字（body 小写匹配，覆盖裸状态码无法区分的情形）。
#: 402 通常是欠费文案；400/404/422/500 里的 model_not_found 是配置缺失。
_PERMANENT_BODY_MARKERS = (
    "insufficient_balance",
    "insufficient balance",
    "topup",
    "model_not_found",
    "no available channel",
    "invalid token",
    "invalid_api_key",
    "unauthorized",
)


def classify_label(status_code: int, headers: dict, body: bytes = b"") -> str:
    """把一次上游失败分类成退避动作标签（纯函数，可单测）。

    判定优先级：Cloudflare/代理封禁检测 > body 永久失效关键字 > 裸状态码。
    返回 :data:`CLASS_*` 之一；规则盲区返回 ``CLASS_UNKNOWN``。
    """
    if status_code == 403 or (status_code == 503 and looks_like_cloudflare_block(status_code, headers, body)):
        # CF blockpage / 代理改写块页：封禁（长冷却，自动恢复）
        if looks_like_cloudflare_block(status_code, headers, body) or looks_like_proxy_block(status_code, headers, body):
            return CLASS_BANNED
    # body 关键字（欠费 / 配置缺失）优先于裸状态码
    raw = (body or b"")[:8192]
    if isinstance(raw, bytes):
        low = raw.decode("utf-8", "replace").lower()
    else:
        low = str(raw).lower()
    if any(m in low for m in _PERMANENT_BODY_MARKERS):
        return CLASS_PERMANENT
    # 裸状态码
    if status_code in (401, 402):
        return CLASS_PERMANENT
    if status_code == 403:
        # 无 CF 特征的非 401 403（如网关自定义拒绝）：按瞬时处理，避免误伤
        return CLASS_TRANSIENT
    if status_code == 429:
        return CLASS_RATE_LIMIT
    if status_code == 499:
        return CLASS_NO_RETRY
    if status_code in (0, 408, 504, 554) or 500 <= status_code <= 599 or status_code >= 600:
        return CLASS_TRANSIENT
    if 400 <= status_code < 500:
        return CLASS_NO_RETRY
    return CLASS_UNKNOWN


def mark_auth_failure(k: KeyHealth) -> None:
    """401/403 等凭据/配置类失败：标记 invalid，等待管理端确认恢复。

    与旧 1h 冷却的区别（2026-08-15 v1）：不再自动到期恢复，改为
    ``STATE_INVALID``（``is_available`` 永不返回 True），只有服务重启
    或管理端「确认恢复」才回到 healthy。
    """
    _bump_failure(k)
    k.status = STATE_INVALID
    k.cooldown_until = 0.0
    k.failure_class = CLASS_PERMANENT
    k.last_failure_at = time.time()


def mark_banned(k: KeyHealth) -> None:
    """403 CF/WAF 封禁：长冷却（600s 档），到期自动恢复；也可手动提前恢复。"""
    _bump_failure(k)
    k.status = STATE_ERROR
    # 封禁直接用最长沙冷却档，不再逐级
    k.cooldown_until = time.time() + backoff_seconds(3)
    k.failure_class = CLASS_BANNED
    k.last_failure_at = time.time()


def mark_rate_limited(k: KeyHealth, retry_after: float = 0.0) -> None:
    """429: rate limited. Honor Retry-After header when available."""
    k.recent_429_count += 1
    _bump_failure(k)
    k.status = STATE_RATE_LIMITED
    k.failure_class = CLASS_RATE_LIMIT
    k.last_failure_at = time.time()
    if retry_after > 0:
        k.cooldown_until = time.time() + min(retry_after, backoff_seconds(3))
    else:
        # 逐级退避：5s → 10s → 60s → 600s（复用 backoff_level）
        k.backoff_level = min(k.backoff_level + 1, 3)
        k.cooldown_until = time.time() + backoff_seconds(k.backoff_level)


def mark_server_error(k: KeyHealth) -> None:
    """5xx: upstream server error. Short exponential backoff."""
    _bump_failure(k)
    k.status = STATE_ERROR
    k.failure_class = CLASS_TRANSIENT
    k.last_failure_at = time.time()
    k.backoff_level = min(k.backoff_level + 1, 3)
    k.cooldown_until = time.time() + backoff_seconds(k.backoff_level)


def mark_network_failure(k: KeyHealth) -> None:
    """Connection error / timeout. Short backoff (often transient)."""
    _bump_failure(k)
    k.status = STATE_ERROR
    k.failure_class = CLASS_TRANSIENT
    k.last_failure_at = time.time()
    k.backoff_level = min(k.backoff_level + 1, 3)
    k.cooldown_until = time.time() + backoff_seconds(k.backoff_level)


def mark_failure(k: KeyHealth) -> None:
    """Generic failure marker (backwards-compatible → server-error treatment)."""
    mark_server_error(k)


def mark_empty_response(k: KeyHealth) -> None:
    """Upstream returned HTTP 200 but with *no content* (empty completion).

    Treated as a soft failure (short exponential backoff, like a 5xx) so the
    scheduler deprioritises the key and routes the *next* request elsewhere.
    Crucially, because the in-flight request has not committed a byte to the
    client yet (pre-first-byte), it can transparently retry with another
    candidate key — this is the "auto switch-key on empty" behaviour.
    """
    _bump_failure(k)
    k.status = STATE_ERROR
    k.failure_class = CLASS_TRANSIENT
    k.last_failure_at = time.time()
    k.backoff_level = min(k.backoff_level + 1, 3)
    k.cooldown_until = time.time() + backoff_seconds(k.backoff_level)


def mark_success(k: KeyHealth) -> None:
    k.success_count += 1
    k.recent_429_count = 0
    # T07: success resets the consecutive counter but keeps the lifetime total.
    k.consecutive_failures = 0
    k.status = STATE_HEALTHY
    k.cooldown_until = 0.0
    # 2026-08-15 v1: 成功降一级退避（半开探测），回到 healthy 后 failure_class 保留
    # 供管理端展示「上次失败原因」，但不影响可用性。
    if k.backoff_level > 0:
        k.backoff_level -= 1
    if k.failure_class and k.status == STATE_HEALTHY and k.backoff_level == 0:
        # 完全恢复后清掉失败标记
        k.failure_class = ""


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


_CLOUDFLARE_BODY_MARKERS = (
    b"cloudflare",
    b"cf-chl",
    b"just a moment",
    b"checking your browser",
    b"_cf_chl",
    b"cf-ray",
    b"attack-scanner",
)


def looks_like_cloudflare_block(status_code: int, headers: dict, body: bytes = b"") -> bool:
    """Detect a Cloudflare challenge / interstitial page.

    Cloudflare often answers API calls with HTTP 403 (or 503) and an HTML
    "Just a moment" / "Checking your browser" page instead of JSON.  Treating
    that as a normal 403-auth failure wrongly cools the key for an hour, and
    forwarding the HTML to downstream clients yields unparseable garbage.
    """
    h = _lower_headers(headers or {})
    if "cloudflare" in (h.get("server") or ""):
        return True
    if "cf-ray" in h or "cf-mitigated" in h:
        return True
    ctype = h.get("content-type") or ""
    if "text/html" in ctype and status_code in (403, 503, 429):
        return True
    low = (body or b"")[:4096].lower()
    if any(m in low for m in _CLOUDFLARE_BODY_MARKERS):
        return True
    return False


_PROXY_BLOCK_BODY_MARKERS = (
    b"please check your network settings",
    b"error code: 1010",
    b"error code: 1011",
    b"error code: 1015",
    b"error code: 1020",
    b"cf-error-code",
)


def looks_like_proxy_block(status_code: int, headers: dict, body: bytes = b"") -> bool:
    """Detect an upstream block page wrapped/rewritten by a reverse proxy.

    Some relay layers (e.g. the user's `.macc.eu.cc` proxy) sit in front of
    Cloudflare-protected upstreams and rewrite Cloudflare's 1010/1020 challenge
    pages into a clean JSON envelope such as::

        {"error": {"message": "Access denied. Please check your network settings."}}

    They strip the `server: cloudflare` / `cf-ray` / HTML markers that
    `looks_like_cloudflare_block` relies on, so the 403 would otherwise be
    misclassified as an `auth_failure` (1h key cooldown).  That is wrong: the
    key is fine, only the *egress IP* is banned and will recover in minutes.
    Detect the proxy-block signature by body content and treat it as a
    transient `server_error` (short backoff + failover to the next group
    member), not a permanent auth failure.
    """
    if status_code not in (403, 503):
        return False
    low = (body or b"")[:8192].lower()
    if any(m in low for m in _PROXY_BLOCK_BODY_MARKERS):
        return True
    return False


def classify_failure(k: KeyHealth, status_code: int, headers: dict, body: bytes = b"") -> bool:
    """根据上游状态码/响应体分类失败并更新 key 健康状态（2026-08-15 v1）。

    返回 True 表示可重试（应换下一个 key），False 表示请求侧错误（应直接返回客户端）。
    分类委托 :func:`classify_label`，状态迁移交给对应的 mark_* 函数：
      permanent → invalid（等确认恢复）；banned → 长冷却；rate_limit/transient → 逐级退避；
      no_retry → 不标记；unknown → 按 transient 降级处理（不阻塞，可交给 agnes 异步补判）。
    """
    retryable, _label = classify_failure_labelled(k, status_code, headers, body)
    return retryable


def classify_failure_labelled(
    k: KeyHealth, status_code: int, headers: dict, body: bytes = b""
) -> tuple[bool, str]:
    """同 :func:`classify_failure`，但额外返回分类标签（供调用方决定 agnes 补判）。"""
    label = classify_label(status_code, headers, body)
    if label == CLASS_PERMANENT:
        mark_auth_failure(k)
        return True, label
    if label == CLASS_BANNED:
        mark_banned(k)
        return True, label
    if label == CLASS_RATE_LIMIT:
        learn_rate_limits(k, headers, status_code)
        h = _lower_headers(headers)
        retry_after = h.get("retry-after")
        try:
            ra_sec = float(retry_after) if retry_after else 0.0
        except (TypeError, ValueError):
            ra_sec = 0.0
        mark_rate_limited(k, ra_sec)
        return True, label
    if label == CLASS_TRANSIENT:
        mark_server_error(k)
        return True, label
    if label == CLASS_UNKNOWN:
        # 规则盲区：按瞬时降级（当前请求继续 failover），分类结论交给
        # agnes 异步补判（config 开关控制，见 handler._maybe_agnes_classify）。
        mark_server_error(k)
        return True, label
    # no_retry：请求侧问题，不重试，也不记健康度（T07 修正历史误判）。
    return False, label


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
