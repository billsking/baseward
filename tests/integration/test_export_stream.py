"""Streaming replies (AC17/AC18/AC21): bounded memory, abort on disconnect, row/time limits, and all-NULL columns."""

from __future__ import annotations

import asyncio
import time

import pymysql
import pytest
from pymysql.cursors import SSCursor
from sqlalchemy import text

from tests.integration.conftest import (
    business_engine,
    connect_export,
    export_server,
    export_settings,
    issue_credential,
)
from tests.meminfo import rss_bytes

pytestmark = pytest.mark.integration

STREAM_TABLE = "stream_big"


async def _seed_stream(env, rows: int, table: str = STREAM_TABLE) -> None:
    # Engine-neutral row generator: MySQL-family engines differ in how a numeric series can be
    # produced (MariaDB exposes seq_1_to_N, MySQL 8 has no Sequence engine), so double a helper
    # table with plain INSERT ... SELECT, which every engine in the compatibility matrix supports.
    engine = business_engine(env)
    helper = f"{table}_seed"
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE TABLE {table} ("
                "id INT NOT NULL PRIMARY KEY, label VARCHAR(64) NULL, "
                "amount DECIMAL(12,2) NULL)"
            )
            await conn.exec_driver_sql(
                f"CREATE TABLE {helper} (seq INT NOT NULL PRIMARY KEY)"
            )
            await conn.exec_driver_sql(
                f"INSERT INTO {helper} (seq) VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10)"
            )
            count = 10
            while count < rows:
                step = min(count, rows - count)
                await conn.exec_driver_sql(
                    f"INSERT INTO {helper} (seq) "
                    f"SELECT seq + {count} FROM {helper} ORDER BY seq LIMIT {step}"
                )
                count += step
            await conn.exec_driver_sql(
                f"INSERT INTO {table} (id, label, amount) "
                f"SELECT seq, CONCAT('row-', seq), seq / 100 "
                f"FROM (SELECT seq FROM {helper} ORDER BY seq LIMIT {rows}) s"
            )
            await conn.exec_driver_sql(f"DROP TABLE {helper}")
    finally:
        await engine.dispose()


async def _latest_audit(env) -> dict | None:
    async with env.services.session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, row_count, deny_reason FROM audit_log "
                    "WHERE event_type='mysql_export' ORDER BY id DESC LIMIT 1"
                )
            )
        ).first()
    if row is None:
        return None
    return {"status": row[0], "row_count": row[1], "deny_reason": row[2]}


async def _wait_audit(env, statuses: set[str], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        entry = await _latest_audit(env)
        if entry is not None and entry["status"] in statuses:
            return entry
        if time.monotonic() >= deadline:
            raise AssertionError(f"未等到审计状态 {statuses}，最后一条：{entry}")
        await asyncio.sleep(0.1)


def _drain_stream(port: int, account: str, password: str, sql: str, limit: int | None = None) -> int:
    connection = connect_export(port, account, password, cursorclass=SSCursor)
    try:
        seen = 0
        cursor = connection.cursor()
        cursor.execute(sql)
        while True:
            row = cursor.fetchone()
            if row is None:
                cursor.close()
                return seen
            seen += 1
            if limit is not None and seen >= limit:
                # SSCursor.close() drains the remaining rows; here we simulate a hard client disconnect by closing the socket.
                result = getattr(connection, "_result", None)
                if result is not None:
                    result.unbuffered_active = False
                connection._force_close()
                return seen
    finally:
        connection.close()


def _run_buffered(port: int, account: str, password: str, sql: str) -> int:
    connection = connect_export(port, account, password)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return len(cursor.fetchall())
    finally:
        connection.close()


async def _upstream_queries(env, needle: str) -> int:
    # Exclude the diagnostic query itself; MariaDB keeps the previous statement's info on idle connections, so look only at non-Sleep ones.
    async with env.services.session_factory() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.processlist "
                "WHERE info LIKE :needle AND command <> 'Sleep' "
                "AND id <> CONNECTION_ID()"
            ),
            {"needle": f"%{needle}%"},
        )
        return int(result.scalar() or 0)


async def _wait_upstream_idle(env, needle: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if await _upstream_queries(env, needle) == 0:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"上游仍残留查询：{needle}")
        await asyncio.sleep(0.1)


async def test_ac17_memory_bounded_and_audit_row_count(env):
    rows = 200_000
    await _seed_stream(env, rows)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_fetch_batch=1000, mysql_export_max_rows=rows)
    async with export_server(env, settings) as (port, limiter):
        baseline = rss_bytes()
        seen = await asyncio.to_thread(
            _drain_stream, port, account, password, f"SELECT id, label, amount FROM {STREAM_TABLE}"
        )
        peak = rss_bytes()
        assert seen == rows
        # The full result is about 200k rows x about 40B; a bounded stream should stay far below that.
        assert peak - baseline < 64 * 1024 * 1024, f"RSS 增量 {(peak - baseline) / 1e6:.1f}MB"
        assert limiter.active == 0
    entry = await _wait_audit(env, {"success"})
    assert entry["row_count"] == rows
    assert entry["deny_reason"] is None


async def test_ac18_disconnect_interrupts_and_kills_upstream(env):
    rows = 200_000
    await _seed_stream(env, rows)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_fetch_batch=1000, mysql_export_max_rows=rows)
    async with export_server(env, settings) as (port, limiter):
        seen = await asyncio.to_thread(
            _drain_stream, port, account, password,
            f"SELECT id, label FROM {STREAM_TABLE}", 100,
        )
        assert seen == 100
        entry = await _wait_audit(env, {"interrupted"})
        assert entry["deny_reason"] == "CLIENT_DISCONNECTED"
        assert 0 < entry["row_count"] < rows
        await _wait_upstream_idle(env, STREAM_TABLE)
        assert limiter.active == 0


async def test_ac21_row_limit_stops_at_configured_count(env):
    total = 20_000
    max_rows = 10_000
    await _seed_stream(env, total)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_rows=max_rows)
    async with export_server(env, settings) as (port, limiter):
        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(
                _run_buffered, port, account, password, f"SELECT id FROM {STREAM_TABLE}"
            )
        assert excinfo.value.args[0] == 1105
        message = str(excinfo.value.args[1])
        assert "GATEWAY:EXPORT_ROW_LIMIT" in message
        assert "request_id=" in message
        entry = await _wait_audit(env, {"interrupted"})
        assert entry["deny_reason"] == "ROW_LIMIT_EXCEEDED"
        assert entry["row_count"] == max_rows
        await _wait_upstream_idle(env, STREAM_TABLE)
        assert limiter.active == 0


async def test_ac21_time_limit_stops_stream(env):
    total = 20_000
    await _seed_stream(env, total)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_seconds=1, mysql_export_fetch_batch=500)
    async with export_server(env, settings) as (port, limiter):
        started = time.monotonic()
        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(
                _run_buffered, port, account, password,
                f"SELECT id, SLEEP(0.001) FROM {STREAM_TABLE}",
            )
        assert excinfo.value.args[0] == 1105
        assert "GATEWAY:EXPORT_TIME_LIMIT" in str(excinfo.value.args[1])
        assert time.monotonic() - started < 30
        entry = await _wait_audit(env, {"interrupted"})
        assert entry["deny_reason"] == "TIME_LIMIT_EXCEEDED"
        assert 0 < entry["row_count"] < total
        await _wait_upstream_idle(env, STREAM_TABLE)
        assert limiter.active == 0


async def test_all_null_column_does_not_swallow_rows(env):
    rows = 3_000
    await _seed_stream(env, rows, table="stream_nulls")
    engine = business_engine(env)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("UPDATE stream_nulls SET label = NULL, amount = NULL")
    finally:
        await engine.dispose()
    account, password = await issue_credential(env)
    async with export_server(env, export_settings(env)) as (port, _):
        rows_read = await asyncio.to_thread(
            _drain_stream, port, account, password, "SELECT label, amount FROM stream_nulls"
        )
        assert rows_read == rows
