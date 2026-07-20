"""Tests for src.rate_limit.SlidingWindowLimiter."""

from src.rate_limit import SlidingWindowLimiter


class _FakeClock:
    """Deterministic, manually-advanced clock for testing time windows."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_max_events_within_window():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)

    assert limiter.allow("key", max_events=3, window_seconds=60) is True
    assert limiter.allow("key", max_events=3, window_seconds=60) is True
    assert limiter.allow("key", max_events=3, window_seconds=60) is True
    assert limiter.allow("key", max_events=3, window_seconds=60) is False


def test_events_expire_after_window_elapses():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)

    assert limiter.allow("key", max_events=1, window_seconds=60) is True
    assert limiter.allow("key", max_events=1, window_seconds=60) is False

    clock.advance(61)
    assert limiter.allow("key", max_events=1, window_seconds=60) is True


def test_keys_are_independent():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)

    assert limiter.allow("a", max_events=1, window_seconds=60) is True
    assert limiter.allow("b", max_events=1, window_seconds=60) is True
    assert limiter.allow("a", max_events=1, window_seconds=60) is False
    assert limiter.allow("b", max_events=1, window_seconds=60) is False


def test_partial_window_expiry_allows_one_more_slot():
    """Only events older than the window are dropped; a single old event
    expiring frees exactly one slot, not the whole window."""
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)

    assert limiter.allow("key", max_events=2, window_seconds=10) is True  # t=0
    clock.advance(5)
    assert limiter.allow("key", max_events=2, window_seconds=10) is True  # t=5
    assert limiter.allow("key", max_events=2, window_seconds=10) is False  # t=5, still full

    clock.advance(6)  # t=11: the t=0 event has aged out, t=5 event has not
    assert limiter.allow("key", max_events=2, window_seconds=10) is True
    assert limiter.allow("key", max_events=2, window_seconds=10) is False
