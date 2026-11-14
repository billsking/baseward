"""Upstream MySQL pool for the export channel: dedicated pool, unbuffered cursor, KILL QUERY.

Fully independent from `datasources/pool.py` (A16): in-flight exports cannot crowd out MCP or admin traffic.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiomysql

from app.config import Settings
from app.errors import DataSourceConnectionError
from app.i18n import t
from app.security.crypto import decrypt_secret

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
KILL_TIMEOUT_SECONDS = 5
POOL_RECYCLE_SECONDS = 3600
# Reserve two overflow connections for kill_query and metadata queries: queries can still be aborted when the pool is full of in-flight exports.
OVERFLOW_CONNECTIONS = 2


def _signature(datasource) -> str:
    raw = (
        f"{datasource.host}|{datasource.port}|{datasource.username}|"
        f"{datasource.password_cipher}|{datasource.default_database}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _errno_of(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _connection_error(exc: BaseException) -> DataSourceConnectionError:
    errno = _errno_of(exc)
    detail = t("error.datasource.error_code", errno=errno) if errno is not None else ""
    return DataSourceConnectionError(
        t("error.datasource.connection_failed", detail=detail)
    )


@dataclass
class ExportCursor:
    cursor: Any
    thread_id: int
    description: tuple | None


class _ScopeRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)


class _ScopeConnection:
    """Wrap an aiomysql connection in the synchronous row-fetch interface expected by `scope.load_columns`."""

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool
        self._conn: Any = None
        self._cursor: Any = None

    async def __aenter__(self) -> "_ScopeConnection":
        self._conn = await self._pool.acquire()
        self._cursor = await self._conn.cursor()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._cursor.close()
        finally:
            self._pool.release(self._conn)

    async def exec_driver_sql(self, sql: str, params: tuple = ()) -> _ScopeRows:
        await self._cursor.execute(sql, params)
        return _ScopeRows(await self._cursor.fetchall())


class ScopeEngine:
    """Pseudo-engine for read-only metadata queries; the interface matches how `scope.enforce` is used."""

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool

    def connect(self) -> _ScopeConnection:
        return _ScopeConnection(self._pool)


class ExportPool:
    """One aiomysql pool per datasource; rebuilt when the config signature changes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._master_key = settings.master_key_bytes
        self._pools: dict[int, tuple[str, aiomysql.Pool]] = {}
        self._lock = asyncio.Lock()

    async def get(self, datasource) -> aiomysql.Pool:
        signature = _signature(datasource)
        async with self._lock:
            cached = self._pools.get(datasource.id)
            if cached is not None and cached[0] == signature:
                return cached[1]
            if cached is not None:
                await self._close_pool(cached[1])
                self._pools.pop(datasource.id, None)
            password = decrypt_secret(datasource.password_cipher, self._master_key)
            try:
                pool = await aiomysql.create_pool(
                    host=datasource.host,
                    port=datasource.port,
                    user=datasource.username,
                    password=password,
                    db=datasource.default_database,
                    charset="utf8mb4",
                    minsize=1,
                    maxsize=self._settings.mysql_export_max_concurrent + OVERFLOW_CONNECTIONS,
                    autocommit=True,
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    pool_recycle=POOL_RECYCLE_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - normalized into a business exception
                logger.warning(
                    "导出连接池创建失败 datasource=%s%s",
                    datasource.id,
                    f"（错误码 {_errno_of(exc)}）" if _errno_of(exc) is not None else "",
                )
                raise _connection_error(exc) from exc
            self._pools[datasource.id] = (signature, pool)
            return pool

    async def scope_engine(self, datasource) -> ScopeEngine:
        return ScopeEngine(await self.get(datasource))

    @asynccontextmanager
    async def stream_cursor(
        self, datasource, sql: str, database: str
    ) -> AsyncIterator[ExportCursor]:
        """Unbuffered cursor; closes the cursor on exit (closing SSCursor drains remaining rows, so abort must kill first).

        Pooled connections are reused and ``db=default_database`` only applies at pool creation;
        every statement must switch to the target database explicitly, otherwise the protocol layer
        switched but upstream still queried the default database (E40/AC43).
        """
        pool = await self.get(datasource)
        try:
            conn = await asyncio.wait_for(
                pool.acquire(), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            raise _connection_error(exc) from exc
        try:
            try:
                await conn.select_db(database)
            except Exception as exc:  # noqa: BLE001 - missing database / no privilege is a connection error
                raise _connection_error(exc) from exc
            cursor = await conn.cursor(aiomysql.SSCursor)
            try:
                await cursor.execute(sql)
            except Exception:
                await self._close_cursor(cursor)
                raise
            try:
                yield ExportCursor(
                    cursor=cursor,
                    thread_id=int(conn.thread_id()),
                    description=cursor.description,
                )
            finally:
                await self._close_cursor(cursor)
        finally:
            pool.release(conn)

    async def kill_query(self, datasource, thread_id: int) -> None:
        """Abort the upstream query from another connection; best-effort recovery that only logs failures."""
        try:
            pool = await self.get(datasource)

            async def _execute_kill() -> None:
                conn = await pool.acquire()
                try:
                    cursor = await conn.cursor()
                    try:
                        await cursor.execute(f"KILL QUERY {int(thread_id)}")
                    finally:
                        await cursor.close()
                finally:
                    pool.release(conn)

            await asyncio.wait_for(_execute_kill(), timeout=KILL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - a failed abort must not raise a new error
            logger.warning(
                "KILL QUERY 失败 datasource=%s thread_id=%s",
                getattr(datasource, "id", None),
                thread_id,
                exc_info=True,
            )

    async def invalidate(self, datasource_id: int) -> None:
        async with self._lock:
            cached = self._pools.pop(datasource_id, None)
            if cached is not None:
                await self._close_pool(cached[1])

    async def close_all(self) -> None:
        async with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for _, pool in pools:
            await self._close_pool(pool)

    @staticmethod
    async def _close_cursor(cursor) -> None:
        try:
            await cursor.close()
        except Exception:  # noqa: BLE001 - close failures must not mask the original exception
            logger.warning("关闭导出游标失败", exc_info=True)

    @staticmethod
    async def _close_pool(pool: aiomysql.Pool) -> None:
        try:
            pool.close()
            await pool.wait_closed()
        except Exception:  # noqa: BLE001
            logger.warning("关闭导出连接池失败", exc_info=True)
