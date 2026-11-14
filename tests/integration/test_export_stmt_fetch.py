"""AC25: server-side cursor batching drops no rows (D29 patch); text and binary protocols agree row by row."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import (
    business_engine,
    export_server,
    export_settings,
    issue_credential,
)
from tests.integration.mysql_wire import SERVER_STATUS_LAST_ROW_SENT, WireConnection

pytestmark = pytest.mark.integration

STMT_TABLE = "stmt_rows"


async def _seed(env, rows: int) -> None:
    engine = business_engine(env)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE TABLE {STMT_TABLE} (id INT NOT NULL PRIMARY KEY)"
            )
            for start in range(1, rows + 1, 500):
                stop = min(start + 500, rows + 1)
                values = ",".join(f"({value})" for value in range(start, stop))
                await conn.exec_driver_sql(
                    f"INSERT INTO {STMT_TABLE} (id) VALUES {values}"
                )
    finally:
        await engine.dispose()


def _collect(port: int, account: str, password: str, batch: int) -> tuple[list, list, list]:
    connection = WireConnection("127.0.0.1", port, account, password)
    try:
        sql = f"SELECT id FROM {STMT_TABLE} ORDER BY id"
        text_rows = [int(row[0]) for row in connection.query(sql)]

        stmt_id = connection.prepare(sql)
        buffered = [row[0] for row in connection.execute_buffered(stmt_id)]

        connection.execute_cursor(stmt_id)
        fetched: list[int] = []
        while True:
            rows, status = connection.fetch(stmt_id, batch)
            fetched.extend(row[0] for row in rows)
            if status & SERVER_STATUS_LAST_ROW_SENT:
                break
        return text_rows, buffered, fetched
    finally:
        connection.close()


@pytest.mark.parametrize(("rows", "batch"), [(1000, 100), (1005, 100), (99, 100)])
async def test_cursor_fetch_loses_no_rows(env, rows, batch):
    await _seed(env, rows)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_rows=rows + 10)
    async with export_server(env, settings) as (port, limiter):
        text_rows, buffered, fetched = await asyncio.to_thread(
            _collect, port, account, password, batch
        )
        assert text_rows == list(range(1, rows + 1))
        assert buffered == text_rows
        assert fetched == text_rows
        assert limiter.active == 0
