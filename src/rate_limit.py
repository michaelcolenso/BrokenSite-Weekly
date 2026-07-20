"""
Lightweight in-memory rate limiting for BrokenSite-Weekly's tracking server.

This is single-process, best-effort abuse mitigation, not an
adversarial-proof control (an attacker rotating source IPs or waiting out
the window is not stopped, and limits reset on restart). It exists to blunt
naive abuse against the unauthenticated tracking endpoints: refresh-spam
scripts inflating engagement scores toward "warm lead" status, and repeated
CTA form submissions flooding pro subscribers with notification emails.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict


class SlidingWindowLimiter:
    """Thread-safe sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str, max_events: int, window_seconds: float) -> bool:
        """Return True and record this call if under the limit for `key`.

        Returns False (without recording) once `max_events` calls for this
        key have landed within the trailing `window_seconds`.
        """
        now = self._clock()
        with self._lock:
            window = self._events[key]
            cutoff = now - window_seconds
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= max_events:
                return False
            window.append(now)
            return True
