"""Time helpers: store UTC, display Asia/Shanghai."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    """Return a naive UTC datetime to match the DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_shanghai(value: datetime | None) -> datetime | None:
    """Convert a naive UTC value from the database to Asia/Shanghai."""
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc)
    return aware.astimezone(SHANGHAI)


def format_shanghai(value: datetime | None) -> str:
    """Format for admin display."""
    local = to_shanghai(value)
    return local.strftime("%Y-%m-%d %H:%M:%S") if local else ""
