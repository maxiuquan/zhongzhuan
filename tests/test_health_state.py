"""健康状态机测试：验证 mark_* 函数和 is_available() 的状态分流逻辑。"""
import time

from zhongzhuan.proxy.ratelimit import (
    KeyHealth, SlidingWindow,
    STATE_HEALTHY, STATE_RATE_LIMITED, STATE_INVALID, STATE_ERROR,
)
from zhongzhuan.proxy.retry import (
    mark_auth_failure, mark_rate_limited, mark_server_error,
    mark_network_failure, mark_success, classify_failure,
    reason_for_exhaustion,
)


def _kh(key_id: int = 1) -> KeyHealth:
    return KeyHealth(
        key_id=key_id, api_key=f"sk-{key_id}",
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
        assert not k.is_available()  # invalid 永不可用
        assert k.cooldown_until > time.time() + 3500  # ~1 小时冷却

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

    def test_403_returns_true_and_marks_invalid(self):
        k = _kh()
        should_retry = classify_failure(k, 403, {})
        assert should_retry is True
        assert k.status == STATE_INVALID

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
