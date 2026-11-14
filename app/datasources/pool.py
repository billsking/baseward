"""Cache business-database pools per datasource."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from app.errors import DataSourceConnectionError
from app.i18n import t
from app.security.crypto import decrypt_secret

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
TEST_TIMEOUT_SECONDS = 10


def _connection_url(host: str, port: int, username: str, password: str, database: str) -> URL:
    return URL.create(
        "mysql+aiomysql",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
        query={"charset": "utf8mb4"},
    )


def _errno_of(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


async def test_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
) -> int:
    """Run SELECT 1 on a temporary connection and return elapsed milliseconds; exceptions carry no password."""
    engine = create_async_engine(
        _connection_url(host, port, username, password, database),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    started = time.perf_counter()
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - normalized into a business exception
        errno = _errno_of(exc)
        detail = f"（错误码 {errno}）" if errno is not None else ""
        logger.warning("数据源连接测试失败 %s:%s%s", host, port, detail)
        raise DataSourceConnectionError(
            t(
                "error.datasource.connection_failed",
                detail=t("error.datasource.error_code", errno=errno) if errno is not None else "",
            )
        ) from exc
    finally:
        await engine.dispose()
    return int((time.perf_counter() - started) * 1000)


class DataSourcePool:
    """One engine per datasource; rebuilt when the config signature changes."""

    def __init__(self, settings: Settings) -> None:
        self._master_key = settings.master_key_bytes
        self._engines: dict[int, tuple[str, AsyncEngine]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _signature(datasource) -> str:
        raw = (
            f"{datasource.host}|{datasource.port}|{datasource.username}|"
            f"{datasource.password_cipher}|{datasource.default_database}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def get(self, datasource) -> AsyncEngine:
        signature = self._signature(datasource)
        async with self._lock:
            cached = self._engines.get(datasource.id)
            if cached is not None and cached[0] == signature:
                return cached[1]
            if cached is not None:
                await cached[1].dispose()
            password = decrypt_secret(datasource.password_cipher, self._master_key)
            engine = create_async_engine(
                _connection_url(
                    datasource.host,
                    datasource.port,
                    datasource.username,
                    password,
                    datasource.default_database,
                ),
                pool_size=5,
                max_overflow=5,
                pool_pre_ping=True,
                pool_recycle=3600,
                # OceanBase reports autocommit off in the handshake while its session stays on, so
                # aiomysql skips SET AUTOCOMMIT and engine.begin() would not roll back.
                connect_args={
                    "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                    "init_command": "SET autocommit=0",
                },
            )
            self._engines[datasource.id] = (signature, engine)
            return engine

    async def invalidate(self, datasource_id: int) -> None:
        async with self._lock:
            cached = self._engines.pop(datasource_id, None)
            if cached is not None:
                await cached[1].dispose()

    async def close_all(self) -> None:
        async with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for _, engine in engines:
            await engine.dispose()
