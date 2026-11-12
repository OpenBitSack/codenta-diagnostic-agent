from __future__ import annotations

import threading
import time


class TokenBucket:
    """Thread-safe token bucket. Used for LLM call rate limiting (shared) and per-principal intake limits."""

    def __init__(self, rate_per_minute: float, burst: int | None = None) -> None:
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_minute)))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.try_acquire():
                return True
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return False

    def retry_after(self) -> float:
        with self._lock:
            self._refill()
            return 0.0 if self.tokens >= 1 else round((1 - self.tokens) / self.rate, 2)
