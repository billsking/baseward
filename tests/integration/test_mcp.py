"""MCP end to end: tool calls, masking consistency, policy denials, auditing, and token lifecycle."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import DataSource
from app.tokens.scope import TokenScope
from tests.integration.conftest import (
    admin_login,
    business_engine,
    call_tool,
    mcp_session,
)

pytestmark = pytest.mark.integration


async def _exec(session, env, sql: str):
    return await call_tool(
        session, "execute_sql", {"datasource": env.ds_name, "sql": sql}
    )


def _error(payload: dict) -> dict:
    assert "error" in payload, payload
    return payload["error"]


async def test_tools_and_search_objects(env):
    async with mcp_session(env) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {
            "list_datasources",
            "describe_datasource",
            "search_objects",
            "execute_sql",
            "issue_export_credential",
        }
        descriptions = {tool.name: tool.description or "" for tool in tools.tools}
        assert "元数据" in descriptions["search_objects"]
        assert "表" in descriptions["describe_datasource"]
        assert "数据源" in descriptions["list_datasources"]
        assert "脱敏" in descriptions["execute_sql"]

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "user", "object_type": "table"},
        )
        assert failed is False, payload
        assert payload["datasource"] == env.ds_name
        assert payload["truncated"] is False
        assert any(
            item["database"] == env.biz_db
            and item["table"] == "users"
            and item["type"] == "table"
            for item in payload["objects"]
        )

        failed, payload = await call_tool(
            session,
            "search_objects",
            {
                "datasource": env.ds_name,
                "pattern": "phone",
                "object_type": "column",
                "database": env.biz_db,
            },
        )
        assert failed is False
        assert any(
            item["type"] == "column"
            and item["column"] == "phone"
            and item["table"] == "users"
            for item in payload["objects"]
        )

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "%", "object_type": "all"},
        )
        assert failed is False
        assert payload["objects"] == []

        for arguments in (
            {"datasource": env.ds_name, "pattern": ""},
            {"datasource": env.ds_name, "pattern": "x", "object_type": "view"},
            {"datasource": env.ds_name, "pattern": "x", "database": "d" * 129},
            {"datasource": env.ds_name, "pattern": "x" * 129},
            {"datasource": "  ", "pattern": "x"},
        ):
            failed, payload = await call_tool(session, "search_objects", arguments)
            assert failed is True
            assert _error(payload)["code"] == "INVALID_ARGUMENT"

        failed, payload = await call_tool(
            session, "search_objects", {"datasource": "nope", "pattern": "x"}
        )
        assert failed is True
        assert _error(payload)["code"] == "DATASOURCE_NOT_FOUND"

        audit_rows, total = await env.services.audit.query(
            {"datasource_name": env.ds_name, "status": "success"}, page=1
        )
        assert total >= 3
        assert all(row.tool == "search_objects" for row in audit_rows)


async def test_search_objects_matches_comments_and_multi_terms(env):
    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "手机", "database": env.biz_db},
        )
        assert failed is False
        assert all("comment" in item for item in payload["objects"])
        assert any(
            item["type"] == "column"
            and item["table"] == "users"
            and item["column"] == "phone"
            and item["comment"] == "手机号"
            for item in payload["objects"]
        )

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "用户 手机", "database": env.biz_db},
        )
        assert failed is False
        assert {item["table"] for item in payload["objects"]} == {"users"}

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "订单 订单号", "database": env.biz_db},
        )
        assert failed is False
        assert any(
            item["table"] == "orders" and item.get("column") == "order_no"
            for item in payload["objects"]
        )


async def test_list_describe_and_alias(env):
    async with mcp_session(env) as session:
        failed, payload = await call_tool(session, "list_datasources", {})
        assert failed is False
        assert payload["count"] == 1
        assert payload["datasources"] == [
            {
                "name": env.ds_name,
                "description": "集成测试数据源",
                "default_database": env.biz_db,
                "is_read_only": True,
                "databases": [{"name": env.biz_db, "read_only": True}],
            }
        ]

        failed, payload = await call_tool(
            session,
            "describe_datasource",
            {"datasource": "集成测试", "pattern": "手机", "database": env.biz_db},
        )
        assert failed is False, payload
        assert payload["datasource"] == env.ds_name
        assert payload["description"] == "集成测试数据源"
        assert [item["table"] for item in payload["tables"]] == ["users"]
        users = payload["tables"][0]
        assert users["comment"] == "用户信息"
        phone = next(item for item in users["columns"] if item["name"] == "phone")
        assert phone == {
            "name": "phone",
            "data_type": "varchar",
            "nullable": True,
            "primary_key": False,
            "comment": "手机号",
        }

        failed, payload = await call_tool(
            session,
            "describe_datasource",
            {"datasource": env.ds_name, "include_columns": False, "limit": 10},
        )
        assert failed is False
        assert {"users", "orders"} <= {item["table"] for item in payload["tables"]}
        assert all("columns" not in item for item in payload["tables"])

        failed, payload = await call_tool(
            session,
            "describe_datasource",
            {"datasource": env.ds_name, "database": env.biz_db, "limit": 1},
        )
        assert failed is False
        assert len(payload["tables"]) == 1
        assert payload["truncated"] is True

        failed, payload = await call_tool(
            session,
            "describe_datasource",
            {"datasource": env.ds_name, "limit": 0},
        )
        assert failed is True
        assert _error(payload)["code"] == "INVALID_ARGUMENT"

        failed, payload = await call_tool(
            session,
            "describe_datasource",
            {
                "datasource": env.ds_name,
                "pattern": "orders",
                "database": env.biz_db,
                "limit": 5,
            },
        )
        assert failed is False
        orders = next(item for item in payload["tables"] if item["table"] == "orders")
        id_column = next(item for item in orders["columns"] if item["name"] == "id")
        assert id_column["primary_key"] is True

        failed, payload = await call_tool(
            session,
            "execute_sql",
            {"datasource": "集成测试", "sql": "SELECT COUNT(*) AS total FROM users"},
        )
        assert failed is False, payload
        assert payload["datasource"] == env.ds_name
        assert payload["rows"] == [[3]]

        extra = await env.services.datasources.create(
            name="testds-bak",
            host=env.db_host,
            port=env.db_port,
            username=env.db_user,
            password=env.db_password,
            default_database=env.biz_db,
            remark="集成测试数据源备份",
        )
        try:
            failed, payload = await call_tool(
                session, "describe_datasource", {"datasource": "集成测试"}
            )
            assert failed is True
            assert _error(payload)["code"] == "DATASOURCE_AMBIGUOUS"
            assert env.ds_name in _error(payload)["message"]
        finally:
            await env.services.datasources.delete(extra.id)


async def test_select_matches_direct_and_masks(env):
    engine = business_engine(env)
    try:
        async with engine.connect() as conn:
            direct = await conn.exec_driver_sql(
                "SELECT id, note, amount FROM users ORDER BY id"
            )
            expected = [
                [row[0], row[1], str(row[2])] for row in direct.fetchall()
            ]
    finally:
        await engine.dispose()

    async with mcp_session(env) as session:
        failed, payload = await _exec(
            session, env, "SELECT id, note, amount FROM users ORDER BY id"
        )
        assert failed is False, payload
        assert payload["statement_type"] == "Select"
        assert payload["columns"] == ["id", "note", "amount"]
        assert payload["rows"] == expected
        assert payload["row_count"] == 3
        assert payload["masked_columns"] == []

        token = json.dumps(payload, ensure_ascii=False)
        assert "13800138000" not in token
        assert "alice@example.com" not in token
        assert "张三" not in token

        failed, payload = await _exec(
            session, env, "SELECT id, phone, email, real_name FROM users ORDER BY id"
        )
        assert failed is False
        assert payload["masked_columns"] == ["phone", "email", "real_name"]
        assert payload["rows"][0][:4] == [1, "138****8000", "a***@example.com", "张*"]
        assert payload["rows"][1][1] is None
        assert payload["rows"][1][2] != "b@example.com"
        assert payload["rows"][1][2].endswith("com")
        assert payload["rows"][2][1] == "136****6000"
        assert payload["rows"][2][2] is None

        failed, payload = await _exec(
            session, env, "SELECT phone AS contact FROM users WHERE id = 1"
        )
        assert failed is False
        assert payload["columns"] == ["contact"]
        assert payload["masked_columns"] == ["contact"]
        assert payload["rows"][0][0] == "138****8000"

        failed, payload = await _exec(
            session, env, "SELECT COUNT(*) AS total, SUM(amount) AS total_amount FROM users"
        )
        assert failed is False
        assert payload["columns"] == ["total", "total_amount"]
        assert payload["rows"][0][0] == 3
        assert payload["masked_columns"] == []


async def test_wildcard_and_manual_rules(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    async with mcp_session(env) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM users WHERE id = 1")
        assert failed is False
        assert payload["masked_columns"] == [
            "phone",
            "mobile",
            "email",
            "password",
            "real_name",
            "address",
        ]
        row = dict(zip(payload["columns"], payload["rows"][0]))
        assert row["phone"] == "138****8000"
        assert row["mobile"] == "139****9000"
        assert row["email"] == "a***@example.com"
        assert row["password"] is None
        assert row["real_name"] == "张*"
        assert row["note"] == "正常客户"
        assert row["amount"] == "12.34"

        await env.services.masking.add_rule(
            ds.id,
            database=env.biz_db,
            table="users",
            column="phone",
            strategy="off",
            pattern=None,
        )
        failed, payload = await _exec(
            session, env, "SELECT id, phone, email FROM users WHERE id = 1"
        )
        assert failed is False
        assert payload["masked_columns"] == ["email"]
        assert payload["rows"][0][1] == "13800138000"

        failed, payload = await _exec(
            session, env, "SELECT phone AS contact FROM users WHERE id = 1"
        )
        assert failed is False
        assert payload["masked_columns"] == []
        assert payload["rows"][0][0] == "13800138000"

        await env.services.masking.add_rule(
            ds.id,
            database=env.biz_db,
            table="users",
            column="note",
            strategy="hash",
            pattern=None,
        )
        failed, first = await _exec(
            session, env, "SELECT id, note FROM users WHERE id = 1"
        )
        assert failed is False
        assert first["masked_columns"] == ["note"]
        hashed = first["rows"][0][1]
        assert hashed != "正常客户"
        assert len(hashed) == 64
        assert all(char in "0123456789abcdef" for char in hashed)

        failed, second = await _exec(
            session, env, "SELECT id, note FROM users WHERE id = 1"
        )
        assert failed is False
        assert second["rows"][0][1] == hashed

        rules = await env.services.masking.list_rules(ds.id)
        hash_rule = next(rule for rule in rules if rule.column == "note")
        await env.services.masking.delete_rule(ds.id, hash_rule.id)
        await env.services.masking.add_rule(
            ds.id,
            database=env.biz_db,
            table="users",
            column="note",
            strategy="null",
            pattern=None,
        )
        failed, payload = await _exec(
            session, env, "SELECT id, note FROM users WHERE id = 1"
        )
        assert failed is False
        assert payload["masked_columns"] == ["note"]
        assert payload["rows"][0][1] is None


async def test_readonly_write_denied_and_audited(env):
    async with mcp_session(env) as session:
        failed, payload = await _exec(
            session, env, "UPDATE users SET note = 'hacked' WHERE id = 1"
        )
        assert failed is True
        error = _error(payload)
        assert error["code"] == "WRITE_ON_READONLY"
        assert "只读" in error["message"]

        row = await env.services.audit.get(error["request_id"])
        assert row is not None
        assert row.status == "denied"
        assert row.deny_reason == "WRITE_ON_READONLY"
        assert "UPDATE users" in row.sql_text

        engine = business_engine(env)
        try:
            async with engine.connect() as conn:
                result = await conn.exec_driver_sql("SELECT note FROM users WHERE id = 1")
                assert result.scalar() == "正常客户"
                count = await conn.exec_driver_sql("SELECT COUNT(*) FROM users")
                assert count.scalar() == 3
        finally:
            await engine.dispose()

        failed, payload = await _exec(session, env, "DELETE FROM users WHERE id = 1")
        assert failed is True
        assert _error(payload)["code"] == "WRITE_ON_READONLY"

        failed, payload = await _exec(session, env, "INSERT INTO users (phone) VALUES ('x')")
        assert failed is True
        assert _error(payload)["code"] == "WRITE_ON_READONLY"


async def test_policy_denials_and_error_codes(env):
    async with mcp_session(env) as session:
        cases = {
            "DROP TABLE users": "DDL_BLOCKED",
            "CREATE TABLE t (id INT)": "DDL_BLOCKED",
            "SELECT 1; SELECT 2": "MULTI_STATEMENT",
            "SELECT * FROM users FOR UPDATE": "LOCKING_READ",
            "GRANT ALL ON *.* TO 'x'@'%'": "UNSUPPORTED_STATEMENT",
            "SELECT FROM WHERE": "SQL_PARSE_ERROR",
            "SELECT * FROM users INTO OUTFILE '/tmp/users.txt'": "SQL_PARSE_ERROR",
        }
        for sql, expected in cases.items():
            failed, payload = await _exec(session, env, sql)
            assert failed is True, sql
            assert _error(payload)["code"] == expected, (sql, payload)

        await env.services.datasources.set_read_only(
            (await env.services.datasources.get_by_name(env.ds_name)).id, False
        )
        failed, payload = await _exec(session, env, "UPDATE users SET note = 'x'")
        assert failed is True
        assert _error(payload)["code"] == "NO_WHERE"

        failed, payload = await _exec(session, env, "DELETE FROM users")
        assert failed is True
        assert _error(payload)["code"] == "NO_WHERE"

        failed, payload = await _exec(session, env, "   ")
        assert failed is True
        assert _error(payload)["code"] == "INVALID_ARGUMENT"


async def test_writable_write_and_audit_success(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.set_read_only(ds.id, False)

    async with mcp_session(env) as session:
        failed, payload = await _exec(
            session, env, "UPDATE users SET note = 'written' WHERE id = 1"
        )
        assert failed is False, payload
        assert payload["statement_type"] == "Update"
        assert payload["row_count"] == 1
        assert payload["columns"] == []
        assert payload["rows"] == []

        row = await env.services.audit.get(payload["request_id"])
        assert row is not None
        assert row.status == "success"
        assert row.row_count == 1
        assert row.duration_ms is not None

        engine = business_engine(env)
        try:
            async with engine.connect() as conn:
                result = await conn.exec_driver_sql("SELECT note FROM users WHERE id = 1")
                assert result.scalar() == "written"
        finally:
            await engine.dispose()


async def test_new_datasource_immediately_usable(env):
    csrf = await admin_login(env)
    created = await env.client.post(
        "/admin/datasources/new",
        data={
            "_csrf": csrf,
            "name": "live-ds",
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": env.db_password,
            "default_database": env.biz_db,
        },
    )
    assert created.status_code == 302, created.text

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "execute_sql",
            {"datasource": "live-ds", "sql": "SELECT COUNT(*) AS total FROM users"},
        )
        assert failed is False, payload
        assert payload["datasource"] == "live-ds"
        assert payload["rows"] == [[3]]

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": "live-ds", "pattern": "orders", "object_type": "table"},
        )
        assert failed is False
        assert payload["objects"][0]["table"] == "orders"


async def test_token_auth_and_disable(env):
    missing = await env.client.post("/mcp", json={})
    assert missing.status_code == 401
    assert missing.headers.get("www-authenticate") == "Bearer"
    assert missing.json() == {"error": "missing_token"}

    invalid = await env.client.post(
        "/mcp", json={}, headers={"Authorization": "Bearer gw_invalid_token_value"}
    )
    assert invalid.status_code == 401
    assert invalid.json() == {"error": "invalid_token"}

    plaintext, row = await env.services.tokens.create("mcp-user")
    async with mcp_session(env, token=plaintext) as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "list_datasources",
            "describe_datasource",
            "search_objects",
            "execute_sql",
            "issue_export_credential",
        }

    await env.services.tokens.disable(row.id)
    disabled = await env.client.post(
        "/mcp", json={}, headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert disabled.status_code == 401
    assert disabled.json() == {"error": "invalid_token"}

    rows, total = await env.services.audit.query({"status": "denied", "q": "unknown"}, page=1)
    assert total >= 2
    assert all(row.event_type == "auth_failure" for row in rows)


async def test_disabled_and_unknown_datasource(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.set_enabled(ds.id, False)
    async with mcp_session(env) as session:
        failed, payload = await _exec(session, env, "SELECT 1")
        assert failed is True
        assert _error(payload)["code"] == "DATASOURCE_DISABLED"

        failed, payload = await call_tool(
            session,
            "execute_sql",
            {"datasource": "missing-ds", "sql": "SELECT 1"},
        )
        assert failed is True
        error = _error(payload)
        assert error["code"] == "DATASOURCE_NOT_FOUND"
        assert env.ds_name in error["message"]

    await env.services.datasources.set_enabled(ds.id, True)


async def test_connection_error_is_generic(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    async with env.services.session_factory() as session:
        row = await session.get(DataSource, ds.id)
        assert row is not None
        row.port = 1
        await session.commit()
    await env.services.pool.invalidate(ds.id)

    async with mcp_session(env) as session:
        failed, payload = await _exec(session, env, "SELECT 1")
        assert failed is True
        error = _error(payload)
        assert error["code"] == "DB_UNAVAILABLE"
        assert "数据库执行失败" in error["message"]
        assert env.db_password not in error["message"]
        assert env.biz_db not in error["message"]

        audit_row = await env.services.audit.get(error["request_id"])
        assert audit_row is not None
        assert audit_row.status == "error"
        assert audit_row.error_message


@asynccontextmanager
async def _report_database(env, rows: list[tuple[int, str]]):
    """Create a second upstream database with a same-named users table but different row count; drop it when the test ends."""
    name = f"{env.biz_db}_rep"
    engine = create_async_engine(
        URL.create(
            "mysql+aiomysql",
            username=env.db_user,
            password=env.db_password,
            host=env.db_host,
            port=env.db_port,
            query={"charset": "utf8mb4"},
        ),
        connect_args={"connect_timeout": 5},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        await conn.exec_driver_sql(
            f"CREATE TABLE `{name}`.users ("
            "id INT NOT NULL PRIMARY KEY, "
            "phone VARCHAR(32) NULL COMMENT '手机号'"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户信息'"
        )
        await conn.exec_driver_sql(
            f"CREATE TABLE `{name}`.coupons ("
            "id INT NOT NULL PRIMARY KEY, amount DECIMAL(10,2) NOT NULL"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='优惠券'"
        )
        if rows:
            placeholders = ",".join(["(%s,%s)"] * len(rows))
            params = tuple(value for row in rows for value in row)
            await conn.exec_driver_sql(
                f"INSERT INTO `{name}`.users (id, phone) VALUES {placeholders}",
                params,
            )
    try:
        yield name
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await engine.dispose()


def _multi_db_payload(env, report_db: str) -> dict:
    return {
        "name": "multi-ds",
        "host": env.db_host,
        "port": env.db_port,
        "username": env.db_user,
        "password": env.db_password,
        "default_database": env.biz_db,
        "databases": [env.biz_db, report_db],
    }


async def test_execute_sql_database_context_and_audit(env):
    """AC38/AC45/AC48: the effective default database runs through read and write paths, with responses and audits from one source."""
    async with _report_database(env, [(1, "r1"), (2, "r2")]) as report_db:
        ds = await env.services.datasources.create(**_multi_db_payload(env, report_db))
        try:
            async with mcp_session(env) as session:
                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {"datasource": "multi-ds", "sql": "SELECT COUNT(*) AS total FROM users"},
                )
                assert failed is False, payload
                assert payload["database"] == env.biz_db
                assert payload["rows"] == [[3]]
                audit_row = await env.services.audit.get(payload["request_id"])
                assert audit_row is not None and audit_row.database_name == env.biz_db

                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {
                        "datasource": "multi-ds",
                        "sql": "SELECT COUNT(*) AS total FROM users",
                        "database": report_db,
                    },
                )
                assert failed is False, payload
                assert payload["database"] == report_db
                assert payload["rows"] == [[2]]
                audit_row = await env.services.audit.get(payload["request_id"])
                assert audit_row is not None and audit_row.database_name == report_db

                failed, payload = await call_tool(
                    session,
                    "search_objects",
                    {"datasource": "multi-ds", "pattern": "users", "object_type": "table"},
                )
                assert failed is False, payload
                assert payload["database"] is None
                assert {item["database"] for item in payload["objects"]} == {
                    env.biz_db,
                    report_db,
                }
                assert payload["databases"] == [
                    {"name": env.biz_db, "read_only": True},
                    {"name": report_db, "read_only": True},
                ]
                audit_row = await env.services.audit.get(payload["request_id"])
                assert audit_row is not None and audit_row.database_name is None

                failed, payload = await call_tool(
                    session,
                    "describe_datasource",
                    {"datasource": "multi-ds", "include_columns": False},
                )
                assert failed is False, payload
                assert {item["database"] for item in payload["tables"]} == {
                    env.biz_db,
                    report_db,
                }
                assert payload["databases"] == [
                    {"name": env.biz_db, "read_only": True},
                    {"name": report_db, "read_only": True},
                ]
                assert payload["is_read_only"] is True

                failed, payload = await call_tool(session, "list_datasources", {})
                assert failed is False, payload
                item = next(
                    row for row in payload["datasources"] if row["name"] == "multi-ds"
                )
                assert item["databases"] == [
                    {"name": env.biz_db, "read_only": True},
                    {"name": report_db, "read_only": True},
                ]
                assert item["is_read_only"] is True

                # The write path switches database first too: unqualified table names must land in the target database (D63/AC48).
                await env.services.datasources.set_read_only(ds.id, False)
                await env.services.datasources.set_database_read_only(
                    ds.id, env.biz_db, True
                )
                await env.services.datasources.set_database_read_only(
                    ds.id, report_db, False
                )

                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {
                        "datasource": "multi-ds",
                        "sql": "INSERT INTO users (id, phone) VALUES (9, 'report-only')",
                        "database": report_db,
                    },
                )
                assert failed is False, payload
                assert payload["database"] == report_db
                assert payload["row_count"] == 1
                audit_row = await env.services.audit.get(payload["request_id"])
                assert audit_row is not None and audit_row.database_name == report_db

                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {
                        "datasource": "multi-ds",
                        "sql": "SELECT COUNT(*) AS total FROM users WHERE id = 9",
                        "database": report_db,
                    },
                )
                assert failed is False, payload
                assert payload["rows"] == [[1]]

                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {
                        "datasource": "multi-ds",
                        "sql": "SELECT COUNT(*) AS total FROM users WHERE id = 9",
                    },
                )
                assert failed is False, payload
                assert payload["rows"] == [[0]]
        finally:
            await env.services.datasources.delete(ds.id)


async def test_unqualified_table_missing_suggests_candidates(env):
    """AC40/E33: on 1146, list same-named tables in other granted databases without changing the error code."""
    async with _report_database(env, [(1, "r1")]) as report_db:
        ds = await env.services.datasources.create(**_multi_db_payload(env, report_db))
        try:
            async with mcp_session(env) as session:
                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {"datasource": "multi-ds", "sql": "SELECT * FROM coupons LIMIT 1"},
                )
                assert failed is True
                error = _error(payload)
                assert error["code"] == "DB_ERROR"
                assert "coupons" in error["message"]
                assert report_db in error["message"]
                assert "同名表" in error["message"]

                failed, payload = await call_tool(
                    session,
                    "execute_sql",
                    {"datasource": "multi-ds", "sql": "SELECT * FROM nowhere_table"},
                )
                assert failed is True
                assert "同名表" not in _error(payload)["message"]
        finally:
            await env.services.datasources.delete(ds.id)


async def test_token_database_scope_consistency(env):
    """AC47: all three tools expose only the token's usable databases; two-level denials and an empty array."""
    async with _report_database(env, [(1, "r1")]) as report_db:
        ds = await env.services.datasources.create(**_multi_db_payload(env, report_db))
        try:
            # Matches the frontend payload "explicit datasource + whole-database grant": unrestricted_tables is an empty set,
            # and must not fall into the legacy "empty tables means all" semantics (W9).
            scope = TokenScope(
                datasources=frozenset({ds.id}),
                unrestricted_tables=frozenset(),
                unrestricted_databases=frozenset({(ds.id, env.biz_db)}),
            )
            token, _ = await env.services.tokens.create("t45-scoped", scope)
            async with mcp_session(env, token=token) as session:
                failed, payload = await call_tool(session, "list_datasources", {})
                assert failed is False, payload
                assert len(payload["datasources"]) == 1
                item = payload["datasources"][0]
                assert item["databases"] == [{"name": env.biz_db, "read_only": True}]
                assert item["is_read_only"] is True

                failed, payload = await call_tool(
                    session,
                    "search_objects",
                    {"datasource": "multi-ds", "pattern": "users"},
                )
                assert failed is False, payload
                assert payload["database"] is None
                assert payload["databases"] == [
                    {"name": env.biz_db, "read_only": True}
                ]
                assert {row["database"] for row in payload["objects"]} == {env.biz_db}

                for tool, arguments in (
                    (
                        "search_objects",
                        {
                            "datasource": "multi-ds",
                            "pattern": "users",
                            "database": report_db,
                        },
                    ),
                    (
                        "describe_datasource",
                        {"datasource": "multi-ds", "database": report_db},
                    ),
                    (
                        "execute_sql",
                        {
                            "datasource": "multi-ds",
                            "sql": "SELECT 1",
                            "database": report_db,
                        },
                    ),
                ):
                    failed, payload = await call_tool(session, tool, arguments)
                    assert failed is True, payload
                    error = _error(payload)
                    assert error["code"] == "DATABASE_DENIED"
                    assert error["message"] == f"当前 Token 无权访问库：{report_db}"
                    audit_row = await env.services.audit.get(error["request_id"])
                    assert audit_row is not None
                    assert audit_row.database_name == report_db

                for tool, arguments in (
                    (
                        "search_objects",
                        {
                            "datasource": "multi-ds",
                            "pattern": "users",
                            "database": "not_exposed",
                        },
                    ),
                    (
                        "describe_datasource",
                        {"datasource": "multi-ds", "database": "not_exposed"},
                    ),
                    (
                        "execute_sql",
                        {
                            "datasource": "multi-ds",
                            "sql": "SELECT 1",
                            "database": "not_exposed",
                        },
                    ),
                ):
                    failed, payload = await call_tool(session, tool, arguments)
                    assert failed is True, payload
                    error = _error(payload)
                    assert error["code"] == "DATABASE_NOT_EXPOSED"
                    assert "not_exposed" in error["message"]
                    assert "users" not in error["message"]

            empty_scope = TokenScope(
                datasources=frozenset({ds.id}),
                unrestricted_databases=frozenset({(ds.id, "outside")}),
            )
            empty_token, _ = await env.services.tokens.create("t45-empty", empty_scope)
            async with mcp_session(env, token=empty_token) as session:
                failed, payload = await call_tool(session, "list_datasources", {})
                assert failed is False, payload
                assert payload["datasources"][0]["databases"] == []

                failed, payload = await call_tool(
                    session,
                    "search_objects",
                    {"datasource": "multi-ds", "pattern": "users"},
                )
                assert failed is False, payload
                assert payload["databases"] == []
                assert payload["objects"] == []
        finally:
            await env.services.datasources.delete(ds.id)
