"""Cache for stable, deterministic intermediate results (replays, regression evaluations).

Keys include every input and every version that can change the result, so a hit is always safe.
"""
from __future__ import annotations

import threading
from typing import Any, Protocol


class ResultCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, kind: str, value: dict[str, Any]) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
            return value

    def put(self, key: str, kind: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = value
