"""SlidingWindow tests."""

import zhongzhuan.proxy.ratelimit as ratelimit_module

from zhongzhuan.proxy.ratelimit import SlidingWindow


class _FakeTime:
    """``ratelimit`` 模块内 ``time`` 的替身：手动推进，测试零真实等待。"""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_window_allows_below_limit():
    w = SlidingWindow(window_seconds=60, limit=3)
    assert w.allow(1)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)


def test_window_expires(monkeypatch):
    # ratelimit.py 以模块级 time.time() 为时间源；monkeypatch 替换整个
    # time 名字空间为假时钟，直接推进 61s 越过 60s 窗口。
    fake_time = _FakeTime()
    monkeypatch.setattr(ratelimit_module, "time", fake_time, raising=True)
    w = SlidingWindow(window_seconds=60, limit=2)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)
    fake_time.advance(61)
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
