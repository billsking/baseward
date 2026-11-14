"""Export quota counter: cap, no queueing, release, and floor protection."""

from __future__ import annotations

from app.mysql.control import ExportLimiter


def test_limiter_counts_up_to_cap():
    limiter = ExportLimiter(2)
    assert limiter.active == 0
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.active == 2


def test_limiter_rejects_instead_of_queueing():
    limiter = ExportLimiter(1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    assert limiter.active == 1


def test_limiter_release_allows_reacquire():
    limiter = ExportLimiter(1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    limiter.release()
    assert limiter.active == 0
    assert limiter.try_acquire() is True


def test_limiter_release_has_floor():
    limiter = ExportLimiter(2)
    limiter.release()
    limiter.release()
    assert limiter.active == 0
    assert limiter.try_acquire() is True
