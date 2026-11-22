"""In-memory brute-force guard for the admin sign-in form.

Failures are counted per (username, client IP) inside a fixed window; reaching the threshold
locks that pair for ``lock_seconds``. Keys are independent, so rotating usernames from one IP
or attacking one account from many IPs never locks every administrator out. State is
process-local: with multiple workers each worker throttles on its own, which still bounds the
guessing rate per worker without adding an external store.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from app.netip import normalize_ip

# Expired entries are pruned opportunistically once a dictionary grows past this size, so a
# spray of distinct usernames cannot grow memory without bound.
MAX_TRACKED_ENTRIES = 4096


class LoginGuard:
    """Counters are plain dicts mutated without awaits, so the event loop needs no lock."""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        lock_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._window = max(1, int(window_seconds))
        self._lock_seconds = max(1, int(lock_seconds))
        self._clock = clock
        # key -> (window index, failures counted in that window)
        self._failures: dict[tuple[str, str], tuple[int, int]] = {}
        # key -> monotonic timestamp when the lock expires
        self._locked: dict[tuple[str, str], float] = {}

    @staticmethod
    def _key(username: str, client_ip: str) -> tuple[str, str]:
        return (username or "").strip()[:64], normalize_ip(client_ip)

    def retry_after(self, username: str, client_ip: str) -> int:
        """Seconds until the pair may try again; 0 means it is not locked."""
        key = self._key(username, client_ip)
        until = self._locked.get(key)
        if until is None:
            return 0
        remaining = until - self._clock()
        if remaining <= 0:
            self._locked.pop(key, None)
            return 0
        return math.ceil(remaining)

    def record_failure(self, username: str, client_ip: str) -> int:
        """Count one failure; return the lock duration when it just triggered a lock, else 0."""
        now = self._clock()
        key = self._key(username, client_ip)
        window = int(now // self._window)
        current_window, count = self._failures.get(key, (window, 0))
        if current_window != window:
            count = 0
        count += 1
        if count >= self._max_attempts:
            self._failures.pop(key, None)
            self._locked[key] = now + self._lock_seconds
            self._prune(now, window)
            return self._lock_seconds
        self._failures[key] = (window, count)
        self._prune(now, window)
        return 0

    def reset(self, username: str, client_ip: str) -> None:
        """Clear both the failure count and any lock after a successful sign-in."""
        key = self._key(username, client_ip)
        self._failures.pop(key, None)
        self._locked.pop(key, None)

    def _prune(self, now: float, window: int) -> None:
        if len(self._failures) + len(self._locked) <= MAX_TRACKED_ENTRIES:
            return
        self._failures = {
            key: value for key, value in self._failures.items() if value[0] == window
        }
        self._locked = {
            key: until for key, until in self._locked.items() if until > now
        }
