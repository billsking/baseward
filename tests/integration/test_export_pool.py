"""Export upstream pool: unbuffered cursor, concurrent connections, KILL QUERY when the pool is full, and pool invalidation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.errors import DataSourceConnectionError
from app.security.crypto import encrypt_secret

from tests.integration.conftest import business_engine, create_datasource, extra_database

pytestmark = pytest.mark.integration

SLEEP_SQL = "SELECT SLEEP(30)"


async def _ds(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    return ds


async def _processlist(engine) -> list[tuple]:
    async with engine.connect() as conn:
        return list((await conn.execute(text("SHOW PROCESSLIST"))).all())


async def _wait_until(factory, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await factory()
        if result:
            return result
        if loop.time() >= deadline:
            raise AssertionError("等待上游状态超时")
        await asyncio.sleep(0.05)


async def _sleeper_ids(engine, expected: int | None) -> list[int] | None:
    """Return the thread id of the running upstream SLEEP query; when expected is None, wait for it to clear."""
    rows = await _processlist(engine)
    ids = [row[0] for row in rows if row[7] and SLEEP_SQL in row[7]]
    if expected is None:
        return not ids
    return ids if len(ids) >= expected else None


async def test_stream_cursor_is_unbuffered(env):
    ds = await _ds(env)
    async with env.services.export_pool.stream_cursor(
        ds, "SELECT id FROM users ORDER BY id", env.biz_db
    ) as stream:
        assert stream.thread_id > 0
        assert stream.description is not None
        assert [row[0] for row in await stream.cursor.fetchmany(2)] == [1, 2]
        assert (await stream.cursor.fetchone())[0] == 3
        assert await stream.cursor.fetchone() is None


async def test_two_streams_hold_independent_connections(env):
    ds = await _ds(env)
    pool = env.services.export_pool
    first = pool.stream_cursor(ds, "SELECT id FROM orders ORDER BY id", env.biz_db)
    stream_one = await first.__aenter__()
    try:
        second = pool.stream_cursor(ds, "SELECT id FROM orders ORDER BY id", env.biz_db)
        stream_two = await second.__aenter__()
        try:
            assert stream_one.thread_id != stream_two.thread_id
            assert (await stream_one.cursor.fetchone())[0] == 1
            assert (await stream_two.cursor.fetchone())[0] == 1
        finally:
            await second.__aexit__(None, None, None)
    finally:
        await first.__aexit__(None, None, None)


async def test_kill_query_works_with_export_quota_saturated(env):
    ds = await _ds(env)
    pool = env.services.export_pool
    limit = env.settings.mysql_export_max_concurrent
    engine = business_engine(env)

    async def hold_sleep() -> None:
        async with pool.stream_cursor(ds, SLEEP_SQL, env.biz_db):
            pass

    tasks = [asyncio.create_task(hold_sleep()) for _ in range(limit)]
    try:
        thread_ids = await _wait_until(lambda: _sleeper_ids(engine, limit))
        assert len(thread_ids) >= limit
        loop = asyncio.get_running_loop()
        started = loop.time()
        for thread_id in thread_ids[:limit]:
            await pool.kill_query(ds, thread_id)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=10
        )
        assert loop.time() - started < 10, "KILL 未生效：游标关闭在等待上游查询自然结束"
        await _wait_until(lambda: _sleeper_ids(engine, None))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()

    async with pool.stream_cursor(ds, "SELECT 1", env.biz_db) as stream:
        assert (await stream.cursor.fetchone())[0] == 1


async def test_datasource_update_rebuilds_export_pool(env):
    ds = await _ds(env)
    old_pool = await env.services.export_pool.get(ds)
    updated = await env.services.datasources.update(
        ds.id,
        name=ds.name,
        host=ds.host,
        port=ds.port,
        username=ds.username,
        password="",
        default_database=ds.default_database,
        remark="导出池失效",
    )
    assert old_pool.closed is True
    new_pool = await env.services.export_pool.get(updated)
    assert new_pool is not old_pool


async def test_connection_failure_does_not_leak_password(env):
    fake = SimpleNamespace(
        id=999_999,
        host="127.0.0.1",
        port=1,
        username="nobody",
        password_cipher=encrypt_secret("Sup3rSecret!", env.settings.master_key_bytes),
        default_database="nobody",
    )
    with pytest.raises(DataSourceConnectionError) as excinfo:
        await env.services.export_pool.get(fake)
    assert "Sup3rSecret!" not in str(excinfo.value)


async def test_stream_cursor_switches_database_per_call(env):
    """AC43/E40: switching databases repeatedly on one pool, each stream must get rows from its own database."""
    pool = env.services.export_pool
    async with extra_database(
        env, "pool_a", [(1, "13000000001"), (2, "13000000002")]
    ) as db_a:
        async with extra_database(env, "pool_b", [(1, "13100000001")]) as db_b:
            ds = await create_datasource(env, "pool-multi", [env.biz_db, db_a, db_b])
            try:
                async with pool.stream_cursor(
                    ds, "SELECT COUNT(*) FROM users", db_a
                ) as stream:
                    assert (await stream.cursor.fetchone())[0] == 2
                async with pool.stream_cursor(
                    ds, "SELECT COUNT(*) FROM users", db_b
                ) as stream:
                    assert (await stream.cursor.fetchone())[0] == 1
                async with pool.stream_cursor(
                    ds, "SELECT COUNT(*) FROM users", env.biz_db
                ) as stream:
                    assert (await stream.cursor.fetchone())[0] == 3
                # The pool key excludes the database: switching databases on one datasource does not create a new pool (D69/R30).
                assert len(pool._pools) == 1
            finally:
                await env.services.datasources.delete(ds.id)


async def test_select_db_failure_is_connection_error(env):
    pool = env.services.export_pool
    async with extra_database(env, "pool_c", [(1, "13200000001")]) as db_c:
        ds = await create_datasource(env, "pool-select", [env.biz_db, db_c])
        try:
            with pytest.raises(DataSourceConnectionError) as excinfo:
                async with pool.stream_cursor(ds, "SELECT 1", "missing_database"):
                    pass
            assert env.db_password not in str(excinfo.value)
        finally:
            await env.services.datasources.delete(ds.id)
