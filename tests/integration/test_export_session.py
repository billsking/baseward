"""Export-channel session: multi-statement, USE, immediate credential invalidation, and read-only constraints (E18 / D21 / D25)."""

from __future__ import annotations

import asyncio

import pymysql
import pytest

from tests.integration.conftest import (
    connect_export,
    export_server,
    issue_credential,
    management_scalar,
    multi_db_export,
    wait_deny_reason,
    wait_success_database,
)

pytestmark = pytest.mark.integration


def _execute(connection, sql: str):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


async def test_multi_statement_is_1064_and_audited(env):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            with pytest.raises(pymysql.err.ProgrammingError) as excinfo:
                await asyncio.to_thread(_execute, connection, "SELECT 1; SELECT 2")
            assert excinfo.value.args[0] == 1064
            assert "MULTI_STATEMENT" in excinfo.value.args[1]
        finally:
            await asyncio.to_thread(connection.close)
    await wait_deny_reason(env, "MULTI_STATEMENT", event_type="mysql_export")


async def test_use_default_database_allowed(env):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            await asyncio.to_thread(_execute, connection, f"USE `{env.biz_db}`")
            rows = await asyncio.to_thread(_execute, connection, "SELECT DATABASE()")
            assert rows == ((env.biz_db,),)
        finally:
            await asyncio.to_thread(connection.close)


async def test_use_unexposed_database_is_rejected(env):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                await asyncio.to_thread(_execute, connection, "USE mysql")
            assert excinfo.value.args[0] == 1235
            assert "DATABASE_NOT_EXPOSED" in excinfo.value.args[1]
        finally:
            await asyncio.to_thread(connection.close)
    await wait_deny_reason(env, "DATABASE_NOT_EXPOSED", event_type="mysql_export")


async def test_revoked_credential_rejects_next_statement(env):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            rows = await asyncio.to_thread(_execute, connection, "SELECT id FROM users ORDER BY id")
            assert rows == ((1,), (2,), (3,))

            credential = (await env.services.credentials.list_all())[0]
            await env.services.credentials.revoke(credential.id, "admin", "127.0.0.1")

            with pytest.raises(pymysql.err.OperationalError) as excinfo:
                await asyncio.to_thread(_execute, connection, "SELECT id FROM users ORDER BY id")
            assert excinfo.value.args[0] == 1045
        finally:
            await asyncio.to_thread(connection.close)
    await wait_deny_reason(env, "CREDENTIAL_REVOKED", event_type="mysql_export")


async def test_export_audit_records_real_client_ip(env):
    """Fetch audits carry the real source IP (this field was always empty on the export channel before v1.6)."""
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            await asyncio.to_thread(_execute, connection, "SELECT id FROM users ORDER BY id")
        finally:
            await asyncio.to_thread(connection.close)
    deadline = asyncio.get_running_loop().time() + 5.0
    sql = (
        "SELECT COUNT(*) FROM audit_log "
        "WHERE event_type='mysql_export' AND status='success' AND client_ip=:ip"
    )
    while True:
        if await management_scalar(env, sql, {"ip": "127.0.0.1"}):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("导出审计未记录真实来源 IP")
        await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("INSERT INTO users (id, mobile) VALUES (99, 'x')", "WRITE_ON_READONLY"),
        ("UPDATE users SET mobile = 'x' WHERE id = 1", "WRITE_ON_READONLY"),
        ("DELETE FROM users WHERE id = 1", "WRITE_ON_READONLY"),
        ("DROP TABLE users", "DDL_BLOCKED"),
    ],
)
async def test_writes_are_rejected_via_force_read_only(env, sql, code):
    account, password = await issue_credential(env)
    async with export_server(env) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        try:
            with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                await asyncio.to_thread(_execute, connection, sql)
            assert excinfo.value.args[0] == 1235
            assert code in excinfo.value.args[1]
        finally:
            await asyncio.to_thread(connection.close)
    await wait_deny_reason(env, code, event_type="mysql_export")


async def test_use_switches_upstream_database_and_audit(env):
    """AC43/E40: after USE, both an unqualified SELECT and SHOW TABLES follow the database switch."""
    async with multi_db_export(
        env,
        view_rows=[(1, "13000000001"), (2, "13000000002")],
        denied_rows=[(1, "13100000001")],
    ) as fixture:
        async with export_server(env) as (port, _):
            connection = await asyncio.to_thread(
                connect_export, port, fixture.account, fixture.password
            )
            try:
                tables = await asyncio.to_thread(_execute, connection, "SHOW TABLES")
                assert {row[0] for row in tables} == {"users", "orders"}

                await asyncio.to_thread(
                    _execute, connection, f"USE `{fixture.view_db}`"
                )
                rows = await asyncio.to_thread(
                    _execute, connection, "SELECT COUNT(*) FROM users"
                )
                assert rows == ((2,),)
                rows = await asyncio.to_thread(_execute, connection, "SELECT DATABASE()")
                assert rows == ((fixture.view_db,),)
                # The metadata cache key includes the database: without switching it would return biz's users+orders.
                tables = await asyncio.to_thread(_execute, connection, "SHOW TABLES")
                assert {row[0] for row in tables} == {"users"}
            finally:
                await asyncio.to_thread(connection.close)
    await wait_success_database(env, fixture.view_db)


async def test_masking_follows_current_database(env):
    """The masking plan must be built for the current database (the export-side counterpart of D72)."""
    async with multi_db_export(
        env,
        view_rows=[(1, "13000000001"), (2, "13000000002")],
        denied_rows=[(1, "13100000001")],
    ) as fixture:
        await env.services.masking.add_rule(
            fixture.datasource.id, fixture.view_db, "users", "phone", "null", None
        )
        async with export_server(env) as (port, _):
            connection = await asyncio.to_thread(
                connect_export, port, fixture.account, fixture.password
            )
            try:
                rows = await asyncio.to_thread(
                    _execute, connection, "SELECT phone FROM users ORDER BY id"
                )
                assert rows[0][0] not in (None, "13800138000")

                await asyncio.to_thread(
                    _execute, connection, f"USE `{fixture.view_db}`"
                )
                rows = await asyncio.to_thread(
                    _execute, connection, "SELECT phone FROM users ORDER BY id"
                )
                assert rows == ((None,), (None,))
            finally:
                await asyncio.to_thread(connection.close)


async def test_use_denials_are_1235_with_database_audit(env):
    """E35: unexposed and exposed-but-unauthorized are separate codes, both 1235, and the audit records the rejected database."""
    async with multi_db_export(
        env,
        view_rows=[(1, "13000000001")],
        denied_rows=[(1, "13100000001")],
    ) as fixture:
        async with export_server(env) as (port, _):
            connection = await asyncio.to_thread(
                connect_export, port, fixture.account, fixture.password
            )
            try:
                with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                    await asyncio.to_thread(
                        _execute, connection, f"USE `{fixture.denied_db}`"
                    )
                assert excinfo.value.args[0] == 1235
                assert "DATABASE_DENIED" in excinfo.value.args[1]
                assert "1046" not in excinfo.value.args[1]

                with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                    await asyncio.to_thread(
                        _execute, connection, "USE `not_exposed_db`"
                    )
                assert excinfo.value.args[0] == 1235
                assert "DATABASE_NOT_EXPOSED" in excinfo.value.args[1]
                assert "1046" not in excinfo.value.args[1]
            finally:
                await asyncio.to_thread(connection.close)
    await wait_deny_reason(
        env,
        "DATABASE_DENIED",
        event_type="mysql_export",
        database_name=fixture.denied_db,
    )
    await wait_deny_reason(
        env,
        "DATABASE_NOT_EXPOSED",
        event_type="mysql_export",
        database_name="not_exposed_db",
    )


async def test_target_database_removed_blocks_next_statement(env):
    """AC44: after the credential target database is removed from the exposed list, the next statement on the same connection is rejected."""
    async with multi_db_export(
        env,
        view_rows=[(1, "13000000001")],
        denied_rows=[(1, "13100000001")],
    ) as fixture:
        async with export_server(env) as (port, _):
            connection = await asyncio.to_thread(
                connect_export, port, fixture.account, fixture.password
            )
            try:
                rows = await asyncio.to_thread(
                    _execute, connection, "SELECT COUNT(*) FROM users"
                )
                assert rows == ((3,),)

                await env.services.datasources.update(
                    fixture.datasource.id,
                    name=fixture.datasource.name,
                    host=fixture.datasource.host,
                    port=fixture.datasource.port,
                    username=fixture.datasource.username,
                    password="",
                    default_database=fixture.view_db,
                    databases=[fixture.view_db, fixture.denied_db],
                )

                with pytest.raises(pymysql.err.NotSupportedError) as excinfo:
                    await asyncio.to_thread(
                        _execute, connection, "SELECT COUNT(*) FROM users"
                    )
                assert excinfo.value.args[0] == 1235
                assert "DATABASE_NOT_EXPOSED" in excinfo.value.args[1]
            finally:
                await asyncio.to_thread(connection.close)
    await wait_deny_reason(
        env,
        "DATABASE_NOT_EXPOSED",
        event_type="mysql_export",
        database_name=env.biz_db,
    )
