"""Stream upstream rows through masking and hand them to the protocol layer in batches: backpressure,
disconnect awareness, row/time limits, and abort.

The library's `write_text_resultset` async branch drains only once after all rows are written and
buffers synchronously in between (D18). Without draining here there is no backpressure: after the
client disconnects the upstream is still drained and memory grows with the result set. Row/time
limits do not depend on disconnect awareness (D26); both must be checked inside the generator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Sequence

from mysql_mimic import ColumnType, ResultColumn
from mysql_mimic.connection import Connection
from mysql_mimic.errors import ErrorCode, MysqlError

from app.datasources.export_pool import ExportCursor
from app.masking import engine as masking
from app.masking.rules import STRATEGY_HASH, STRATEGY_MASK
from app.mysql.errors import format_message
from app.query.pipeline import (
    PreparedRead,
    conclude_interrupted,
    conclude_success,
)

logger = logging.getLogger(__name__)

# Align with the library's write_many batch size so stream._buffer stays at about 1,000 rows.
DRAIN_EVERY = 1000
# `drain()` returns immediately below the high-water mark and does not yield to the event loop. Exports
# share that loop with MCP, so not yielding pins MCP requests behind a whole batch encoding; yield every 50 rows (~1 ms slices).
YIELD_EVERY = 50

ROW_LIMIT = "ROW_LIMIT_EXCEEDED"
TIME_LIMIT = "TIME_LIMIT_EXCEEDED"
CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"

# Audit reasons (§6.3) and client error codes are different sets: limits return EXPORT_*_LIMIT.
_LIMIT_CODE = {ROW_LIMIT: "EXPORT_ROW_LIMIT", TIME_LIMIT: "EXPORT_TIME_LIMIT"}
_LIMIT_MESSAGE = {
    "EXPORT_ROW_LIMIT": "导出结果行数达到上限",
    "EXPORT_TIME_LIMIT": "导出耗时达到上限",
}


class ExportAborted(MysqlError):
    """Carry abort information: the generator is consumed by the library and can only become an error packet via the protocol layer's `MysqlError`."""

    def __init__(self, reason: str, sent: int, request_id: str) -> None:
        code = _LIMIT_CODE.get(reason, CLIENT_DISCONNECTED)
        message = f"{_LIMIT_MESSAGE.get(code, '客户端已断开')}（已发送 {sent} 行）"
        super().__init__(format_message(code, message, request_id), ErrorCode.UNKNOWN_ERROR)
        self.reason = reason
        self.sent = sent


def build_result_columns(
    description: Sequence[Sequence[Any]] | None, string_masked: set[str]
) -> list[ResultColumn]:
    """Emit explicit types for every column.

    Without column types the library infers them by pulling rows from the async iterator
    (results.py:108-168); an all-NULL column then swallows the whole result set into memory and
    breaks AC17. Both NULL and unknown types use VAR_STRING.
    """
    columns: list[ResultColumn] = []
    for item in description or ():
        name = str(item[0])
        try:
            column_type = ColumnType(int(item[1]))
        except (TypeError, ValueError):
            column_type = ColumnType.VAR_STRING
        if column_type is ColumnType.NULL or name in string_masked:
            column_type = ColumnType.VAR_STRING
        columns.append(ResultColumn(name=name, type=column_type))
    return columns


def string_masked_columns(plan, columns: list[str]) -> set[str]:
    """Columns whose masked value becomes a string (mask/hash); the null policy keeps the original type."""
    rules = masking.column_rules(plan, columns)
    return {
        columns[index]
        for index, rule in enumerate(rules)
        if rule is not None and rule.strategy in (STRATEGY_MASK, STRATEGY_HASH)
    }


async def stream_rows(
    *,
    services: Any,
    settings: Any,
    prepared: PreparedRead,
    cursor: ExportCursor,
    cursor_cm: Any,
    connection: Connection,
    plan: Any,
    session: Any,
) -> AsyncIterator[Sequence]:
    """Read in batches -> mask -> yield; on exit abort upstream, write the audit, release quota, and close the cursor."""
    columns = [str(item[0]) for item in cursor.description or ()]
    sent = 0
    reason: str | None = None
    exhausted = False
    deadline = time.monotonic() + settings.mysql_export_max_seconds
    # Must share the source with the MCP channel (AC16): Services.mask_key is a derived subkey, not the master key.
    mask_key = services.mask_key
    try:
        while reason is None:
            batch = await cursor.cursor.fetchmany(settings.mysql_export_fetch_batch)
            if not batch:
                exhausted = True
                break
            masked, _ = masking.apply(plan, columns, batch, mask_key)
            for row in masked:
                if sent >= settings.mysql_export_max_rows:
                    reason = ROW_LIMIT
                    break
                if time.monotonic() > deadline:
                    reason = TIME_LIMIT
                    break
                yield tuple(row)
                sent += 1
                if sent % YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                    if sent % DRAIN_EVERY == 0:
                        if connection.stream.writer.is_closing():
                            reason = CLIENT_DISCONNECTED
                            break
                        await connection.stream.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        reason = CLIENT_DISCONNECTED
    except asyncio.CancelledError:
        reason = reason or CLIENT_DISCONNECTED
        raise
    except GeneratorExit:
        reason = reason or CLIENT_DISCONNECTED
        raise
    finally:
        if reason is not None and not exhausted:
            # Order matters: SSCursor.close() drains remaining rows, so closing first makes the kill impossible.
            await services.export_pool.kill_query(prepared.datasource, cursor.thread_id)
        try:
            if reason is None:
                await conclude_success(services, prepared, sent)
            else:
                await conclude_interrupted(services, prepared, sent, reason)
        finally:
            session.release_quota()
            await close_cursor(cursor_cm)
    if reason is not None:
        raise ExportAborted(reason, sent, prepared.request_id)


async def close_cursor(cursor_cm: Any) -> None:
    try:
        await cursor_cm.__aexit__(None, None, None)
    except Exception:  # noqa: BLE001 - close failures must not mask the abort reason
        logger.warning("channel=export 关闭导出游标失败", exc_info=True)
