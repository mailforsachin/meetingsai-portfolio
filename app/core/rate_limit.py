from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 5


class LoginRateLimiter:
    def __init__(self, window: int = WINDOW_SECONDS, max_attempts: int = MAX_ATTEMPTS):
        self._window = window
        self._max = max_attempts
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> None:
        q = self._hits[key]
        while q and now - q[0] > self._window:
            q.popleft()

    def is_blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._trim(key, now)
            return len(self._hits[key]) >= self._max

    def register_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._trim(key, now)
            self._hits[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_limiter = LoginRateLimiter()
