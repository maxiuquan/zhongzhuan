"""SlidingWindow tests."""
import time

from zhongzhuan.proxy.ratelimit import SlidingWindow


def test_window_allows_below_limit():
    w = SlidingWindow(window_seconds=60, limit=3)
    assert w.allow(1)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)


def test_window_expires():
    w = SlidingWindow(window_seconds=1, limit=2)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)
    time.sleep(1.1)
    assert w.allow(1)


def test_window_unlimited():
    w = SlidingWindow(window_seconds=60, limit=0)
    for _ in range(1000):
        assert w.allow(1)


def test_window_current_usage():
    w = SlidingWindow(window_seconds=60, limit=10)
    w.allow(3)
    assert w.current_usage() == 3
    w.allow(2)
    assert w.current_usage() == 5