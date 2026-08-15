"""健康状态机测试：验证 mark_* 函数和 is_available() 的状态分流逻辑。"""

import time

from zhongzhuan.proxy.ratelimit import (
    KeyHealth,
    SlidingWindow,
    STATE_HEALTHY,
    STATE_RATE_LIMITED,
    STATE_INVALID,
    STATE_ERROR,
)
from zhongzhuan.proxy.retry import (
    mark_auth_failure,
    mark_rate_limited,
    mark_server_error,
    mark_network_failure,
    mark_success,
    classify_failure,
    reason_for_exhaustion,
)


def _kh(key_id: int = 1) -> KeyHealth:
    return KeyHealth(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 100),
        rpm_limit=100,
    )


class TestHealthStateMachine:
    def test_fresh_key_is_healthy_and_available(self):
        k = _kh()
        assert k.status == STATE_HEALTHY
        assert k.is_available()

    def test_mark_auth_failure_sets_invalid(self):
        k = _kh()
        mark_auth_failure(k)
        assert k.status == STATE_INVALID
        assert not k.is_available()  # invalid 永不可用（等管理端确认恢复）
        # v1（2026-08-15）：不再自动到期——cooldown=0，invalid 由
        # is_available() 直接拒绝，直到「确认恢复」或服务重启。
        assert k.cooldown_until == 0.0

    def test_mark_rate_limited_sets_state_and_cooldown(self):
        k = _kh()
        mark_rate_limited(k, retry_after=30.0)
        assert k.status == STATE_RATE_LIMITED
        assert not k.is_available()  # 冷却期内不可用
        assert k.cooldown_until > time.time() + 25
        assert k.recent_429_count == 1

    def test_mark_rate_limited_without_retry_after_uses_backoff(self):
        k = _kh()
        mark_rate_limited(k)  # 无 retry_after
        assert k.status == STATE_RATE_LIMITED
        assert k.cooldown_until > time.time()  # 有冷却
        assert k.recent_429_count == 1

    def test_mark_server_error_sets_error_state(self):
        k = _kh()
        mark_server_error(k)
        assert k.status == STATE_ERROR
        assert not k.is_available()  # 冷却期内不可用
        assert k.cooldown_until > time.time()

    def test_mark_network_failure_sets_error_state(self):
        k = _kh()
        mark_network_failure(k)
        assert k.status == STATE_ERROR
        assert not k.is_available()

    def test_mark_success_resets_to_healthy(self):
        k = _kh()
        mark_server_error(k)
        assert k.status == STATE_ERROR
        mark_success(k)
        assert k.status == STATE_HEALTHY
        assert k.cooldown_until == 0.0
        assert k.recent_429_count == 0
        assert k.is_available()

    def test_invalid_never_available_even_after_cooldown(self):
        """invalid 状态即使冷却到期也不可用（需要手动 reload 或重启）。"""
        k = _kh()
        mark_auth_failure(k)
        # 模拟冷却到期
        k.cooldown_until = time.time() - 1
        assert k.status == STATE_INVALID
        assert not k.is_available()  # 仍然不可用


class TestClassifyFailure:
    def test_401_returns_true_and_marks_invalid(self):
        k = _kh()
        should_retry = classify_failure(k, 401, {})
        assert should_retry is True
        assert k.status == STATE_INVALID

    def test_403_returns_true_and_marks_transient_without_cf_markers(self):
        k = _kh()
        should_retry = classify_failure(k, 403, {})
        assert should_retry is True
        # v1：无 CF 特征的裸 403 按瞬时处理（避免误伤）；带 CF/WAF 特征才是 banned。
        assert k.status == STATE_ERROR

    def test_403_cf_block_marks_banned(self):
        k = _kh()
        should_retry = classify_failure(k, 403, {"server": "cloudflare"})
        assert should_retry is True
        assert k.status == STATE_ERROR  # banned = 长冷却的 error 态
        assert k.cooldown_until > time.time() + 500  # 600s 档

    def test_429_returns_true_and_marks_rate_limited(self):
        k = _kh()
        should_retry = classify_failure(k, 429, {"retry-after": "10"})
        assert should_retry is True
        assert k.status == STATE_RATE_LIMITED

    def test_500_returns_true_and_marks_error(self):
        k = _kh()
        should_retry = classify_failure(k, 500, {})
        assert should_retry is True
        assert k.status == STATE_ERROR

    def test_503_returns_true_and_marks_error(self):
        k = _kh()
        should_retry = classify_failure(k, 503, {})
        assert should_retry is True
        assert k.status == STATE_ERROR

    def test_400_returns_false_and_does_not_mark(self):
        """400 是请求侧错误，不可重试，且不标记 key 健康度（T07 修正）。"""
        k = _kh()
        should_retry = classify_failure(k, 400, {})
        assert should_retry is False
        assert k.status == STATE_HEALTHY  # 4xx 不再误伤上游 key
        assert k.total_failures == 0

    def test_404_returns_false(self):
        k = _kh()
        should_retry = classify_failure(k, 404, {})
        assert should_retry is False


class TestReasonForExhaustion:
    def test_no_keys(self):
        assert reason_for_exhaustion([]) == "no_keys"

    def test_all_invalid(self):
        keys = [_kh(1), _kh(2)]
        for k in keys:
            mark_auth_failure(k)
        assert reason_for_exhaustion(keys) == "all_invalid"

    def test_all_rate_limited(self):
        keys = [_kh(1), _kh(2)]
        for k in keys:
            mark_rate_limited(k)
        assert reason_for_exhaustion(keys) == "all_rate_limited"

    def test_all_error(self):
        keys = [_kh(1), _kh(2)]
        for k in keys:
            mark_server_error(k)
        assert reason_for_exhaustion(keys) == "all_error"

    def test_mixed_returns_all_exhausted(self):
        keys = [_kh(1), _kh(2)]
        mark_auth_failure(keys[0])
        mark_rate_limited(keys[1])
        assert reason_for_exhaustion(keys) == "all_exhausted"


# ---------------------------------------------------------------------------
# 2026-08-15 v1：classify_label 纯函数 + 逐级退避 + 确认恢复
# ---------------------------------------------------------------------------


class TestClassifyLabel:
    def test_permanent_body_keywords(self):
        from zhongzhuan.proxy.retry import classify_label
        from zhongzhuan.proxy.ratelimit import CLASS_PERMANENT

        cases = [
            (400, b'{"error":{"code":"model_not_found"}}'),
            (500, b'{"error":{"message":"No available channel for model x"}}'),
            (402, '{"error":"INSUFFICIENT_BALANCE","message":"请充值 topup"}'.encode("utf-8")),
            (401, b'{"error":{"message":"Invalid token"}}'),
        ]
        for status, body in cases:
            assert classify_label(status, {}, body) == CLASS_PERMANENT, (status, body)

    def test_banned_cloudflare(self):
        from zhongzhuan.proxy.retry import classify_label
        from zhongzhuan.proxy.ratelimit import CLASS_BANNED

        assert classify_label(403, {"server": "cloudflare"}, b"") == CLASS_BANNED
        assert classify_label(403, {}, b"<!doctype html>Just a moment...") == CLASS_BANNED
        assert classify_label(503, {"server": "cloudflare"}, b"") == CLASS_BANNED

    def test_bare_403_transient(self):
        from zhongzhuan.proxy.retry import classify_label
        from zhongzhuan.proxy.ratelimit import CLASS_TRANSIENT

        assert classify_label(403, {}, b"{}") == CLASS_TRANSIENT

    def test_status_code_rules(self):
        from zhongzhuan.proxy.retry import classify_label
        from zhongzhuan.proxy.ratelimit import (
            CLASS_RATE_LIMIT, CLASS_TRANSIENT, CLASS_NO_RETRY, CLASS_UNKNOWN,
        )

        assert classify_label(429, {}, b"") == CLASS_RATE_LIMIT
        assert classify_label(503, {}, b"") == CLASS_TRANSIENT
        assert classify_label(554, {}, b"") == CLASS_TRANSIENT
        assert classify_label(499, {}, b"") == CLASS_NO_RETRY
        assert classify_label(422, {}, b"{}") == CLASS_NO_RETRY
        assert classify_label(200, {}, b"") == CLASS_UNKNOWN  # 规则盲区


class TestBackoffLevels:
    def test_transient_escalates_and_success_degrades(self):
        k = _kh()
        k.record_failure("transient")
        assert k.backoff_level == 1
        assert k.cooldown_until > time.time()
        k.record_failure("transient")
        assert k.backoff_level == 2
        k.record_success()
        assert k.backoff_level == 1  # 成功降一级
        assert k.status == STATE_HEALTHY

    def test_permanent_invalid_requires_reactivate(self):
        k = _kh()
        k.record_failure("permanent")
        assert k.status == STATE_INVALID
        assert not k.is_available()
        assert k.cooldown_until == 0.0  # 不自动到期
        k.reactivate()
        assert k.status == STATE_HEALTHY
        assert k.is_available()
        assert k.backoff_level == 0

    def test_banned_uses_max_backoff_and_reactivate_clears(self):
        k = _kh()
        k.record_failure("banned")
        assert k.status == STATE_ERROR
        assert k.backoff_level == 3  # 600s 档
        assert k.cooldown_until > time.time() + 500
        k.reactivate()
        assert k.status == STATE_HEALTHY
        assert k.cooldown_until == 0.0

    def test_rate_limit_respects_retry_after(self):
        k = _kh()
        k.record_failure("rate_limit", retry_after="3")
        assert k.status == STATE_RATE_LIMITED
        assert k.cooldown_until > time.time() + 1
        assert k.cooldown_until <= time.time() + 10  # 尊重 Retry-After，不超 600s
