"""Cleanup scheduling math and loop behavior (no MySQL)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.audit import cleanup as module
from app.clock import SHANGHAI


def test_seconds_until_next_run() -> None:
    before = datetime(2026, 1, 2, 2, 59, 30, tzinfo=SHANGHAI)
    assert module.seconds_until_next_run(before) == 30

    exactly = datetime(2026, 1, 2, 3, 0, 0, tzinfo=SHANGHAI)
    assert module.seconds_until_next_run(exactly) == 24 * 3600

    noon = datetime(2026, 1, 2, 12, 0, 0, tzinfo=SHANGHAI)
    assert module.seconds_until_next_run(noon) == 15 * 3600

    late = datetime(2026, 1, 2, 3, 0, 1, tzinfo=SHANGHAI)
    assert module.seconds_until_next_run(late) == 24 * 3600 - 1


async def test_loop_waits_before_first_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(module, "seconds_until_next_run", lambda now=None: 30)

    class FakeAudit:
        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_once(self) -> int:
            self.calls += 1
            return 0

    audit = FakeAudit()
    services = SimpleNamespace(audit=audit)
    stop = asyncio.Event()

    task = asyncio.create_task(module.cleanup_loop(services, stop))
    await asyncio.sleep(0.05)
    assert audit.calls == 0

    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert audit.calls == 0


async def test_loop_keeps_running_after_failure(monkeypatch) -> None:
    monkeypatch.setattr(module, "seconds_until_next_run", lambda now=None: 0)
    monkeypatch.setattr(module.logger, "exception", lambda *args, **kwargs: None)
    ready = asyncio.Event()

    class FakeAudit:
        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_once(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            ready.set()
            return 0

    audit = FakeAudit()
    services = SimpleNamespace(audit=audit)
    stop = asyncio.Event()

    task = asyncio.create_task(module.cleanup_loop(services, stop))
    await asyncio.wait_for(ready.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert audit.calls >= 2
