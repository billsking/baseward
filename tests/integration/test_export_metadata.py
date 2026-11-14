"""Authorization filtering on the metadata exit (AC23 / D22): all five entrypoints must share one allowlist."""

from __future__ import annotations

import asyncio

import pytest
from pymysql.constants import COMMAND

from app.tokens.scope import ColumnRef, TableRef, build_scope
from tests.integration.conftest import (
    connect_export,
    export_server,
    issue_credential,
    multi_db_export,
)

pytestmark = pytest.mark.integration

USERS_COLUMNS = {"id", "phone"}


async def _scoped_credential(env) -> tuple[str, str]:
    datasource = await env.services.datasources.get_by_name(env.ds_name)
    assert datasource is not None
    scope = build_scope(
        [datasource.id],
        [
            TableRef(datasource.id, env.biz_db, "users"),
            TableRef(datasource.id, env.biz_db, "orders"),
        ],
        [
            ColumnRef(datasource.id, env.biz_db, "users", "id"),
            ColumnRef(datasource.id, env.biz_db, "users", "phone"),
        ],
    )
    _, token_row = await env.services.tokens.create("scoped-export", scope)
    return await env.services.credentials.issue(
        token_id=token_row.id,
        datasource_id=datasource.id,
        database=datasource.default_database,
        ttl_hours=1,
        admin_username="admin",
        client_ip="127.0.0.1",
    )


def _query(connection, sql: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def _lenenc_string(buf: bytes, pos: int) -> tuple[str, int]:
    first = buf[pos]
    if first < 0xFB:
        length, pos = first, pos + 1
    elif first == 0xFC:
        length, pos = int.from_bytes(buf[pos + 1 : pos + 3], "little"), pos + 3
    else:  # pragma: no cover - a table name cannot exceed 16MB
        raise ValueError(f"不支持的 length-encoded 前缀：{first}")
    return buf[pos : pos + length].decode(), pos + length


def _com_field_list(connection, table: str) -> list[str]:
    """Column list for COM_FIELD_LIST; the library answers this command directly from schema()."""
    connection._execute_command(COMMAND.COM_FIELD_LIST, (table + "\x00").encode())
    names: list[str] = []
    while True:
        packet = connection._read_packet()
        if packet.is_eof_packet():
            break
        body = packet.read_all()
        pos = 0
        for _ in range(4):  # catalog / schema / table / org_table
            _, pos = _lenenc_string(body, pos)
        name, _ = _lenenc_string(body, pos)
        names.append(name)
    return names


async def test_show_tables_only_lists_authorized_tables(env):
    account, password = await _scoped_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            rows = await asyncio.to_thread(_query, connection, "SHOW TABLES")
            assert {row[0] for row in rows} == {"users", "orders"}
        finally:
            await asyncio.to_thread(connection.close)


async def test_describe_and_show_columns_filter_fields(env):
    account, password = await _scoped_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            described = await asyncio.to_thread(_query, connection, "DESCRIBE users")
            assert {row[0] for row in described} == USERS_COLUMNS

            shown = await asyncio.to_thread(_query, connection, "SHOW COLUMNS FROM users")
            assert {row[0] for row in shown} == USERS_COLUMNS

            orders = await asyncio.to_thread(_query, connection, "DESCRIBE orders")
            assert {"order_no", "amount"} <= {row[0] for row in orders}
        finally:
            await asyncio.to_thread(connection.close)


async def test_show_databases_hides_other_databases(env):
    account, password = await _scoped_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            rows = await asyncio.to_thread(_query, connection, "SHOW DATABASES")
            names = {row[0] for row in rows}
            assert env.biz_db in names
            assert env.mgmt_db not in names
        finally:
            await asyncio.to_thread(connection.close)


async def test_information_schema_is_filtered(env):
    account, password = await _scoped_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            rows = await asyncio.to_thread(
                _query,
                connection,
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_schema = '{env.biz_db}'",
            )
            users = {column for table, column in rows if table == "users"}
            assert users == USERS_COLUMNS
            assert any(table == "orders" and column == "order_no" for table, column in rows)
        finally:
            await asyncio.to_thread(connection.close)


async def test_com_field_list_is_filtered(env):
    account, password = await _scoped_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            names = await asyncio.to_thread(_com_field_list, connection, "users")
            assert set(names) == USERS_COLUMNS
        finally:
            await asyncio.to_thread(connection.close)


async def test_unrestricted_credential_sees_all_columns(env):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            described = await asyncio.to_thread(_query, connection, "DESCRIBE users")
            names = {row[0] for row in described}
            assert {"id", "phone", "mobile", "email", "password"} <= names

            field_list = await asyncio.to_thread(_com_field_list, connection, "users")
            assert set(field_list) == names
        finally:
            await asyncio.to_thread(connection.close)


async def test_metadata_follows_use_and_cache_key(env):
    """AC43: DESCRIBE / COM_FIELD_LIST follow USE; the cache key includes the database and does not mix them up."""
    async with multi_db_export(
        env,
        view_rows=[(1, "13000000001")],
        denied_rows=[(1, "13100000001")],
        view_extra_sql="ALTER TABLE users ADD COLUMN view_only VARCHAR(16) NULL",
    ) as fixture:
        async with export_server(env) as (port, _):
            connection = await asyncio.to_thread(
                connect_export, port, fixture.account, fixture.password
            )
            try:
                described = await asyncio.to_thread(
                    _query, connection, "DESCRIBE users"
                )
                names = {row[0] for row in described}
                assert {"id", "phone"} <= names
                assert "view_only" not in names

                await asyncio.to_thread(
                    _query, connection, f"USE `{fixture.view_db}`"
                )
                described = await asyncio.to_thread(
                    _query, connection, "DESCRIBE users"
                )
                assert "view_only" in {row[0] for row in described}

                field_list = await asyncio.to_thread(
                    _com_field_list, connection, "users"
                )
                assert "view_only" in field_list

                await asyncio.to_thread(_query, connection, f"USE `{env.biz_db}`")
                described = await asyncio.to_thread(
                    _query, connection, "DESCRIBE users"
                )
                assert "view_only" not in {row[0] for row in described}
            finally:
                await asyncio.to_thread(connection.close)
