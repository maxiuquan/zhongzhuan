"""Scheduler tests."""
import time

from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.proxy.scheduler import pick_key, score


def _kh(key_id: int, success: int = 0, failure: int = 0, cooldown: float = 0) -> KeyHealth:
    return KeyHealth(
        key_id=key_id, api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 1000),
        success_count=success, failure_count=failure,
        cooldown_until=cooldown,
    )


def test_pick_key_prefers_higher_success_rate():
    keys = [_kh(1, success=10, failure=0), _kh(2, success=5, failure=5)]
    picked = pick_key(keys)
    assert picked is not None
    assert picked.key_id == 1


def test_pick_key_skip_cooldown():
    keys = [_kh(1, cooldown=time.time() + 60), _kh(2)]
    picked = pick_key(keys)
    assert picked is not None
    assert picked.key_id == 2


def test_pick_key_returns_none_when_all_unavailable():
    keys = [_kh(1, cooldown=time.time() + 60), _kh(2, cooldown=time.time() + 60)]
    assert pick_key(keys) is None


def test_score_clamps():
    k = _kh(1, success=0, failure=0)
    s = score(k)
    assert 0.0 <= s <= 1.0