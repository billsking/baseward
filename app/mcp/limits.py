"""Admission control for MCP tool calls: global/per-token concurrency caps and per-token rate.

Every rejection is immediate; nothing is queued. A queued call would still consume an HTTP
connection and a request slot, so queueing under overload makes the gateway slower, not safer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

RATE_LIMITED = "RATE_LIMITED"
TOO_MANY_CONCURRENT = "TOO_MANY_CONCURRENT"
# Same client-facing code as the global cap, but a distinct message names the token's own cap.
TOO_MANY_CONCURRENT_TOKEN = "TOO_MANY_CONCURRENT"

RATE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class LimitRejection:
    """Why a call was rejected; ``message_key`` refers to an ``app.mcp.texts`` entry."""

    code: str
    message_key: str
    limit: int


class McpLimiter:
    """Counters are plain integers mutated without awaits, so the event loop needs no lock."""

    def __init__(
        self,
        max_concurrent: int,
        max_concurrent_per_token: int,
        rate_limit_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_concurrent = int(max_concurrent)
        # 0 means "no per-token cap": the global cap alone bounds the whole gateway.
        self._max_per_token = int(max_concurrent_per_token)
        # 0 disables rate limiting.
        self._rate_limit = int(rate_limit_per_minute)
        self._clock = clock
        self._active = 0
        self._active_by_token: dict[int, int] = {}
        # token_id -> (window index, requests counted in that window); fixed windows keep it cheap.
        self._windows: dict[int, tuple[int, int]] = {}

    def try_acquire(self, token_id: int) -> LimitRejection | None:
        """Reserve a slot for one tool call; callers must ``release`` in a finally block."""
        if self._rate_limit > 0:
            window = int(self._clock() // RATE_WINDOW_SECONDS)
            current_window, count = self._windows.get(token_id, (window, 0))
            if current_window != window:
                current_window, count = window, 0
            if count >= self._rate_limit:
                self._windows[token_id] = (current_window, count)
                return LimitRejection(
                    RATE_LIMITED, "error.rate_limited", self._rate_limit
                )
            self._windows[token_id] = (current_window, count + 1)

        if (
            self._max_per_token > 0
            and self._active_by_token.get(token_id, 0) >= self._max_per_token
        ):
            return LimitRejection(
                TOO_MANY_CONCURRENT_TOKEN,
                "error.too_many_concurrent_token",
                self._max_per_token,
            )
        if self._active >= self._max_concurrent:
            return LimitRejection(
                TOO_MANY_CONCURRENT,
                "error.too_many_concurrent",
                self._max_concurrent,
            )
        self._active += 1
        self._active_by_token[token_id] = self._active_by_token.get(token_id, 0) + 1
        return None

    def release(self, token_id: int) -> None:
        if self._active > 0:
            self._active -= 1
        current = self._active_by_token.get(token_id, 0)
        if current <= 1:
            self._active_by_token.pop(token_id, None)
        else:
            self._active_by_token[token_id] = current - 1

    @property
    def active(self) -> int:
        return self._active
