"""AC20 / E15: once the quota is full, new export requests get 1040 while the connection itself is not rejected (D16)."""

from __future__ import annotations

import asyncio
import time

import pymysql
import pytest

from tests.integration.conftest import (
    business_engine,
    connect_export,
    export_server,
    export_settings,
    issue_credential,
)

pytestmark = pytest.mark.integration

SLOW_TABLE = "conc_slow"
ROWS = 10


async def _seed(env) -> None:
    engine = business_engine(env)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE TABLE {SLOW_TABLE} (id INT NOT NULL PRIMARY KEY)"
            )
            values = ",".join(f"({value})" for value in range(1, ROWS + 1))
            await conn.exec_driver_sql(f"INSERT INTO {SLOW_TABLE} (id) VALUES {values}")
    finally:
        await engine.dispose()


def _slow_select(port: int, account: str, password: str) -> list[int]:
    connection = connect_export(port, account, password)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id, SLEEP(0.3) FROM {SLOW_TABLE} ORDER BY id")
            return [int(row[0]) for row in cursor.fetchall()]
    finally:
        connection.close()


def _select_ids(port: int, account: str, password: str) -> list[int]:
    connection = connect_export(port, account, password)
    try:
        return _execute_ids(connection)
    finally:
        connection.close()


def _execute_ids(connection) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {SLOW_TABLE} ORDER BY id")
        return [int(row[0]) for row in cursor.fetchall()]


async def _wait_active(limiter, expected: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while limiter.active != expected:
        if time.monotonic() >= deadline:
            raise AssertionError(f"配额未达到 {expected}，当前 {limiter.active}")
        await asyncio.sleep(0.02)


async def test_ac20_third_export_gets_1040_and_connection_between_requests_is_fine(env):
    await _seed(env)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_concurrent=2)

    async with export_server(env, settings) as (port, limiter):
        # Idle connections (no query yet) do not consume quota: open one first, then use it for the third query after two hold the quota.
        third = await asyncio.to_thread(connect_export, port, account, password)
        try:
            first = asyncio.create_task(
                asyncio.to_thread(_slow_select, port, account, password)
            )
            second = asyncio.create_task(
                asyncio.to_thread(_slow_select, port, account, password)
            )
            try:
                await _wait_active(limiter, 2)
                with pytest.raises(pymysql.err.OperationalError) as excinfo:
                    await asyncio.to_thread(_execute_ids, third)
                error = excinfo.value
                assert error.args[0] == 1040
                assert "GATEWAY:EXPORT_CONCURRENCY_LIMIT" in str(error.args[1])
                assert "request_id=" in str(error.args[1])
                assert limiter.active == 2

                expected = list(range(1, ROWS + 1))
                assert await first == expected
                assert await second == expected
                assert limiter.active == 0

                # The TCP connection was not rejected: after quota is released the same connection can still query.
                assert await asyncio.to_thread(_execute_ids, third) == expected
            finally:
                for task in (first, second):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
        finally:
            await asyncio.to_thread(third.close)


async def test_idle_pool_connections_do_not_consume_quota(env):
    await _seed(env)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_concurrent=1)

    async with export_server(env, settings) as (port, limiter):
        idle = [
            await asyncio.to_thread(connect_export, port, account, password)
            for _ in range(3)
        ]
        try:
            assert limiter.active == 0
            assert await asyncio.to_thread(_select_ids, port, account, password) == list(
                range(1, ROWS + 1)
            )
            assert limiter.active == 0
        finally:
            for connection in idle:
                await asyncio.to_thread(connection.close)
