"""Purge expired audit records daily at 03:00 (Asia/Shanghai)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.clock import SHANGHAI

if TYPE_CHECKING:
    from app.services import Services

logger = logging.getLogger(__name__)

CLEANUP_HOUR = 3


def seconds_until_next_run(now: datetime | None = None) -> float:
    current = now or datetime.now(SHANGHAI)
    target = current.replace(hour=CLEANUP_HOUR, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


async def cleanup_loop(services: "Services", stop_event: asyncio.Event) -> None:
    """Do not run at startup; wait for the next 03:00."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds_until_next_run())
            return
        except asyncio.TimeoutError:
            pass
        try:
            deleted = await services.audit.cleanup_once()
            logger.info("审计清理完成，删除 %s 条过期记录", deleted)
        except Exception:  # noqa: BLE001 - a failed cleanup must not exit the loop
            logger.exception("审计清理失败，将在下一个周期重试")
        try:
            removed = await services.credentials.cleanup_expired()
            logger.info("凭证清理完成，删除 %s 个过期凭证", removed)
        except Exception:  # noqa: BLE001 - independent from audit cleanup
            logger.exception("凭证清理失败，将在下一个周期重试")
        try:
            removed = await services.short_credentials.cleanup_expired()
            logger.info("短期凭证清理完成，删除 %s 个过期凭证", removed)
        except Exception:  # noqa: BLE001 - independent from the other cleanups
            logger.exception("短期凭证清理失败，将在下一个周期重试")
