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


class _FakeAudit:
    def __init__(self, *, due: bool = False, fail_first: bool = False) -> None:
        self.due = due
        self.fail_first = fail_first
        self.calls = 0
        self.marks = 0

    async def cleanup_due(self) -> bool:
        return self.due

    async def cleanup_once(self) -> int:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")
        return 0

    async def mark_cleanup_done(self) -> None:
        self.marks += 1


class _FakeExpiring:
    async def cleanup_expired(self) -> int:
        return 0


def _services(audit: _FakeAudit) -> SimpleNamespace:
    return SimpleNamespace(
        audit=audit,
        credentials=_FakeExpiring(),
        short_credentials=_FakeExpiring(),
    )


async def test_loop_waits_before_first_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(module, "seconds_until_next_run", lambda now=None: 30)

    audit = _FakeAudit(due=False)
    stop = asyncio.Event()

    task = asyncio.create_task(module.cleanup_loop(_services(audit), stop))
    await asyncio.sleep(0.05)
    assert audit.calls == 0

    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert audit.calls == 0


async def test_loop_runs_catchup_when_overdue(monkeypatch) -> None:
    monkeypatch.setattr(module, "seconds_until_next_run", lambda now=None: 3600)

    audit = _FakeAudit(due=True)
    stop = asyncio.Event()

    task = asyncio.create_task(module.cleanup_loop(_services(audit), stop))
    await asyncio.sleep(0.05)
    assert audit.calls == 1
    assert audit.marks == 1

    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_loop_keeps_running_after_failure(monkeypatch) -> None:
    monkeypatch.setattr(module, "seconds_until_next_run", lambda now=None: 0)
    monkeypatch.setattr(module.logger, "exception", lambda *args, **kwargs: None)
    ready = asyncio.Event()

    class FakeAudit(_FakeAudit):
        async def cleanup_once(self) -> int:
            self.calls += 1
            if self.calls >= 2:
                ready.set()
            raise RuntimeError("boom")

    audit = FakeAudit(due=False)
    stop = asyncio.Event()

    task = asyncio.create_task(module.cleanup_loop(_services(audit), stop))
    await asyncio.wait_for(ready.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert audit.calls >= 2
    assert audit.marks == 0
