"""Integration tests for MCP large-result auto handoff (AC26-AC30).

Requires .env.test to point at a dedicated test MySQL; the export channel is started separately inside the test.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pymysql
import pytest
from sqlalchemy import text

from app.tokens.scope import TokenScope
from tests.integration.conftest import (
    business_engine,
    call_tool,
    connect_export,
    create_datasource,
    export_server,
    export_settings,
    extra_database,
    issue_credential,
    management_scalar,
    mcp_session,
    wait_deny_reason,
)

pytestmark = pytest.mark.integration

USERS_SQL = "SELECT id, phone FROM users ORDER BY id"
EXTRA_USERS = 297  # 300 rows in total with the 3 rows seeded by the fixture


async def seed_users(env, extra: int = EXTRA_USERS) -> int:
    engine = business_engine(env)
    try:
        async with engine.begin() as conn:
            for start in range(0, extra, 100):
                chunk = min(100, extra - start)
                values: list[str] = []
                params: list[object] = []
                for index in range(start, start + chunk):
                    params.extend([1000 + index, f"138{index:08d}", f"user{index}"])
                    values.append("(%s, %s, %s)")
                await conn.exec_driver_sql(
                    "INSERT INTO users (id, phone, real_name) VALUES " + ",".join(values),
                    tuple(params),
                )
            total = (await conn.exec_driver_sql("SELECT COUNT(*) FROM users")).scalar()
    finally:
        await engine.dispose()
    return int(total)


def handoff_settings(env, port: int, **overrides):
    return export_settings(
        env,
        mysql_export_allow_cidrs="127.0.0.1/32",
        mysql_export_advertise_host="127.0.0.1",
        mysql_export_advertise_port=port,
        **overrides,
    )


def install_handoff_settings(env, settings) -> None:
    env.services.settings = settings
    env.services.credentials._settings = settings


class RecordingConnection:
    def __init__(self, conn, sqls: list[str]) -> None:
        self._conn = conn
        self._sqls = sqls

    async def exec_driver_sql(self, sql, *args, **kwargs):
        self._sqls.append(sql)
        return await self._conn.exec_driver_sql(sql, *args, **kwargs)


class RecordingEngine:
    def __init__(self, engine, sqls: list[str]) -> None:
        self._engine = engine
        self._sqls = sqls

    @asynccontextmanager
    async def connect(self):
        async with self._engine.connect() as conn:
            yield RecordingConnection(conn, self._sqls)


def record_sql(monkeypatch, env) -> list[str]:
    """Record the SQL actually executed by the business database to prove the row cap is pushed upstream."""
    sqls: list[str] = []
    real_get = env.services.pool.get

    async def fake_get(datasource):
        return RecordingEngine(await real_get(datasource), sqls)

    monkeypatch.setattr(env.services.pool, "get", fake_get)
    return sqls


def fetch_all(connection, sql: str):
    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = [item[0] for item in cursor.description]
        return columns, cursor.fetchall()


async def credential_row(env, account_name: str):
    async with env.services.session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT issued_by, created_at, expires_at, revoked_at "
                    "FROM export_credential WHERE account_name = :name"
                ),
                {"name": account_name},
            )
        ).one()


async def count_audit(env, sql: str, params: dict) -> int:
    return int(await management_scalar(env, sql, params))


async def test_row_cap_hands_off_usable_credential(env, monkeypatch) -> None:
    total = await seed_users(env)
    sqls = record_sql(monkeypatch, env)
    async with export_server(env) as (port, _):
        install_handoff_settings(
            env,
            handoff_settings(
                env, port, mcp_result_max_rows=100, mcp_result_max_bytes=0
            ),
        )
        async with mcp_session(env) as session:
            is_error, payload = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )

        assert is_error is False
        assert payload["truncated"] is True
        assert payload["truncated_reason"] == "rows"
        assert payload["row_count"] == 100
        assert len(payload["rows"]) == 100
        assert payload["hint"]

        export = payload["export"]
        assert export is not None
        assert export["sql"] == USERS_SQL
        assert export["database"] == env.biz_db
        assert export["host"] == "127.0.0.1"
        assert export["port"] == port
        assert "GATEWAY_EXPORT_PASSWORD" in export["sample_code"]
        assert export["password"] not in export["sample_code"]

        # The row cap happens at the SQL layer: upstream fetches only cap+1 rows, never fetchall
        assert any("LIMIT 101" in sql for sql in sqls)

        # Fetch everything with the handoff credential and compare masked values cell by cell
        connection = await asyncio.to_thread(
            connect_export, port, export["user"], export["password"], pymysql.cursors.SSCursor
        )
        try:
            columns, rows = await asyncio.to_thread(fetch_all, connection, export["sql"])
        finally:
            await asyncio.to_thread(connection.close)

        assert columns == payload["columns"]
        assert len(rows) == total
        assert [list(row) for row in rows[:100]] == payload["rows"]

    request_id = payload["request_id"]
    assert (
        await count_audit(
            env,
            "SELECT COUNT(*) FROM audit_log WHERE event_type='credential_lifecycle' "
            "AND tool='issue_auto' AND deny_reason LIKE :pattern",
            {"pattern": f"%req={request_id}"},
        )
        == 1
    )
    assert (
        await count_audit(
            env,
            "SELECT COUNT(*) FROM audit_log WHERE event_type='mcp_tool' "
            "AND status='success' AND row_count=100",
            {},
        )
        >= 1
    )


async def test_export_disabled_only_truncates(env) -> None:
    await seed_users(env)
    install_handoff_settings(
        env, export_settings(env, mysql_export_enabled=False, mcp_result_max_rows=2)
    )
    async with mcp_session(env) as session:
        is_error, payload = await call_tool(
            session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
        )

    assert is_error is False
    assert payload["truncated"] is True
    assert payload["export"] is None
    assert payload["hint"] == "结果已截断。导出通道未启用，请缩小查询范围后重试，或联系管理员。"
    assert (
        await count_audit(
            env,
            "SELECT COUNT(*) FROM audit_log WHERE tool='issue_auto'",
            {},
        )
        == 0
    )


async def test_wildcard_listen_without_advertise_host_only_truncates(env) -> None:
    await seed_users(env)
    install_handoff_settings(
        env,
        export_settings(
            env,
            mcp_result_max_rows=2,
            mysql_export_host="0.0.0.0",
            mysql_export_advertise_host="",
        ),
    )
    async with mcp_session(env) as session:
        is_error, payload = await call_tool(
            session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
        )

    assert is_error is False
    assert payload["truncated"] is True
    assert payload["export"] is None
    assert payload["hint"] == "结果已截断。导出通道地址未配置，请缩小查询范围后重试，或联系管理员。"
    assert (
        await count_audit(
            env, "SELECT COUNT(*) FROM audit_log WHERE tool='issue_auto'", {}
        )
        == 0
    )


async def test_auto_credential_ttl_expiry_and_token_disable(env) -> None:
    await seed_users(env)
    async with export_server(env) as (port, _):
        settings = handoff_settings(
            env,
            port,
            mcp_result_max_rows=2,
            mcp_result_max_bytes=0,
            mcp_export_ttl_seconds=60,
        )
        install_handoff_settings(env, settings)
        async with mcp_session(env) as session:
            _is_error, payload = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )
        export = payload["export"]
        assert export is not None

        issued_by, created_at, expires_at, revoked_at = await credential_row(
            env, export["user"]
        )
        token_prefix = await management_scalar(env, "SELECT token_prefix FROM api_token LIMIT 1")
        assert issued_by == f"mcp:{token_prefix}"
        assert 59 <= (expires_at - created_at).total_seconds() <= 61
        assert revoked_at is None

        connection = await asyncio.to_thread(
            connect_export, port, export["user"], export["password"]
        )
        try:
            _columns, rows = await asyncio.to_thread(fetch_all, connection, "SELECT 1")
            assert rows == ((1,),)
        finally:
            await asyncio.to_thread(connection.close)

        # After expiry, reuse the v1.4 denial semantics: clients always get 1045
        async with env.services.session_factory() as session_db:
            await session_db.execute(
                text(
                    "UPDATE export_credential SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND "
                    "WHERE account_name = :name"
                ),
                {"name": export["user"]},
            )
            await session_db.commit()

        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(
                connect_export, port, export["user"], export["password"]
            )
        assert excinfo.value.args[0] == 1045
        await wait_deny_reason(env, "CREDENTIAL_EXPIRED")

        # Take another unexpired credential; disabling its token also yields 1045 (TOKEN_DISABLED)
        async with mcp_session(env) as session:
            _is_error, payload = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )
        second = payload["export"]
        assert second is not None
        token_id = await management_scalar(env, "SELECT id FROM api_token LIMIT 1")
        await env.services.tokens.disable(token_id)
        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(
                connect_export, port, second["user"], second["password"]
            )
        assert excinfo.value.args[0] == 1045
        await wait_deny_reason(env, "TOKEN_DISABLED")


async def test_active_cap_supersedes_only_auto_credentials(env) -> None:
    await seed_users(env)
    admin_account, admin_password = await issue_credential(env)
    async with export_server(env) as (port, _):
        install_handoff_settings(
            env,
            handoff_settings(
                env,
                port,
                mcp_result_max_rows=2,
                mcp_result_max_bytes=0,
                mcp_export_max_active=1,
            ),
        )
        async with mcp_session(env) as session:
            _is_error, first = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )
            _is_error, second = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )

        first_export = first["export"]
        second_export = second["export"]
        assert first_export is not None and second_export is not None
        assert first_export["user"] != second_export["user"]

        _issued_by, _created, _expires, first_revoked = await credential_row(
            env, first_export["user"]
        )
        _issued_by, _created, _expires, second_revoked = await credential_row(
            env, second_export["user"]
        )
        _issued_by, _created, _expires, admin_revoked = await credential_row(
            env, admin_account
        )
        assert first_revoked is not None
        assert second_revoked is None
        assert admin_revoked is None
        assert (
            await count_audit(
                env,
                "SELECT COUNT(*) FROM audit_log WHERE tool='revoke' "
                "AND deny_reason LIKE 'auto_superseded req=%'",
                {},
            )
            >= 1
        )

        with pytest.raises(pymysql.err.OperationalError) as excinfo:
            await asyncio.to_thread(
                connect_export, port, first_export["user"], first_export["password"]
            )
        assert excinfo.value.args[0] == 1045

        for account, password in (
            (second_export["user"], second_export["password"]),
            (admin_account, admin_password),
        ):
            connection = await asyncio.to_thread(connect_export, port, account, password)
            try:
                _columns, rows = await asyncio.to_thread(fetch_all, connection, "SELECT 1")
                assert rows == ((1,),)
            finally:
                await asyncio.to_thread(connection.close)


async def test_both_caps_zero_keeps_v14_behavior(env) -> None:
    total = await seed_users(env)
    install_handoff_settings(
        env,
        export_settings(
            env, mcp_result_max_rows=0, mcp_result_max_bytes=0, mysql_export_enabled=False
        ),
    )
    async with mcp_session(env) as session:
        is_error, payload = await call_tool(
            session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
        )

    assert is_error is False
    assert payload["truncated"] is False
    assert payload["truncated_reason"] is None
    assert payload["hint"] is None
    assert payload["export"] is None
    assert payload["row_count"] == total
    assert len(payload["rows"]) == total


async def test_truncated_query_pins_handoff_to_effective_database(env) -> None:
    """AC42: a query on database B is truncated; both the handoff credential and the export connection land in B."""
    async with extra_database(
        env,
        "handoff",
        [(201, "13900002001"), (202, "13900002002"), (203, "13900002003")],
    ) as view_db:
        datasource = await create_datasource(
            env, "handoff-multi", [env.biz_db, view_db]
        )
        token, _ = await env.services.tokens.create(
            "handoff-multi",
            TokenScope(
                datasources=frozenset({datasource.id}),
                unrestricted_databases=frozenset(
                    {(datasource.id, env.biz_db), (datasource.id, view_db)}
                ),
            ),
        )
        try:
            async with export_server(env) as (port, _):
                install_handoff_settings(
                    env,
                    handoff_settings(
                        env, port, mcp_result_max_rows=1, mcp_result_max_bytes=0
                    ),
                )
                async with mcp_session(env, token) as session:
                    is_error, payload = await call_tool(
                        session,
                        "execute_sql",
                        {
                            "datasource": datasource.name,
                            "sql": USERS_SQL,
                            "database": view_db,
                        },
                    )

                assert is_error is False
                export = payload["export"]
                assert export is not None
                assert export["database"] == view_db

                connection = await asyncio.to_thread(
                    connect_export, port, export["user"], export["password"]
                )
                try:
                    _columns, rows = await asyncio.to_thread(
                        fetch_all, connection, USERS_SQL
                    )
                finally:
                    await asyncio.to_thread(connection.close)
                assert [row[0] for row in rows] == [201, 202, 203]
        finally:
            await env.services.datasources.delete(datasource.id)


async def test_explicit_tool_issues_usable_credential(env) -> None:
    total = await seed_users(env)
    async with export_server(env) as (port, _):
        install_handoff_settings(env, handoff_settings(env, port))
        async with mcp_session(env) as session:
            is_error, payload = await call_tool(
                session,
                "issue_export_credential",
                {
                    "datasource": env.ds_name,
                    "sql": USERS_SQL,
                    "reason": "测试显式导出",
                },
            )

        assert is_error is False
        assert payload["sql"] == USERS_SQL
        assert "SSCursor" in payload["sample_code"]

        connection = await asyncio.to_thread(
            connect_export,
            port,
            payload["user"],
            payload["password"],
            pymysql.cursors.SSCursor,
        )
        try:
            _columns, rows = await asyncio.to_thread(fetch_all, connection, payload["sql"])
        finally:
            await asyncio.to_thread(connection.close)
        assert len(rows) == total

    request_id = payload["request_id"]
    assert (
        await count_audit(
            env,
            "SELECT COUNT(*) FROM audit_log WHERE event_type='mcp_tool' "
            "AND tool='issue_export_credential' AND status='success' "
            "AND request_id=:request_id",
            {"request_id": request_id},
        )
        == 1
    )
    assert (
        await count_audit(
            env,
            "SELECT COUNT(*) FROM audit_log WHERE event_type='credential_lifecycle' "
            "AND tool='issue_explicit' AND deny_reason LIKE :pattern",
            {"pattern": f"%req={request_id} reason=测试显式导出%"},
        )
        == 1
    )


async def test_require_token_explicit_issue_denied(env) -> None:
    async with export_server(env) as (port, _):
        install_handoff_settings(env, handoff_settings(env, port))
        token, _ = await env.services.tokens.create("agent", actor_policy="require")
        headers = {"X-BaseWard-Actor-Provider": "dingtalk", "X-BaseWard-Actor-Id": "lisi001"}
        async with mcp_session(env, token=token, extra_headers=headers) as session:
            is_error, payload = await call_tool(
                session,
                "issue_export_credential",
                {"datasource": env.ds_name, "sql": USERS_SQL, "reason": "agent export"},
            )
        assert is_error is True
        assert payload["error"]["code"] == "CREDENTIAL_ISSUE_DENIED"

    rows, total = await env.services.audit.query({"actor": "lisi001", "status": "denied"})
    assert total >= 1
    assert any(row.tool == "issue_export_credential" for row in rows)


async def test_require_token_auto_handoff_denied(env) -> None:
    await seed_users(env)
    async with export_server(env) as (port, _):
        install_handoff_settings(
            env,
            handoff_settings(env, port, mcp_result_max_rows=100, mcp_result_max_bytes=0),
        )
        token, _ = await env.services.tokens.create("agent", actor_policy="require")
        headers = {"X-BaseWard-Actor-Provider": "dingtalk", "X-BaseWard-Actor-Id": "lisi001"}
        async with mcp_session(env, token=token, extra_headers=headers) as session:
            is_error, payload = await call_tool(
                session, "execute_sql", {"datasource": env.ds_name, "sql": USERS_SQL}
            )
        assert is_error is False
        assert payload["truncated"] is True
        assert payload["export"] is None
        assert payload["hint"]
