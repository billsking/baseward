"""Token three-level authorization: datasource/table/column denials, wildcard rewriting, search filtering, and scope edits."""

from __future__ import annotations

import pytest

from app.datasources import databases
from app.tokens.scope import (
    UNRESTRICTED,
    ColumnRef,
    TableRef,
    TokenScope,
    build_scope,
)
from tests.integration.conftest import business_engine, call_tool, mcp_session

pytestmark = pytest.mark.integration


def _error(payload: dict) -> dict:
    assert "error" in payload, payload
    return payload["error"]


async def _exec(session, env, sql: str, datasource: str | None = None):
    return await call_tool(
        session,
        "execute_sql",
        {"datasource": datasource or env.ds_name, "sql": sql},
    )


async def _users_scope(env, columns: list[str], tables: list[str] | None = None):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    table_names = tables or ["users"]
    return build_scope(
        [ds.id],
        [TableRef(ds.id, env.biz_db, table) for table in table_names],
        [ColumnRef(ds.id, env.biz_db, "users", column) for column in columns],
    )


async def test_datasource_denied_and_not_listed(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.create(
        name="otherds",
        host=env.db_host,
        port=env.db_port,
        username=env.db_user,
        password=env.db_password,
        default_database=env.biz_db,
    )
    plaintext, _ = await env.services.tokens.create(
        "scoped-ds", TokenScope(datasources=frozenset({ds.id}))
    )
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT 1", datasource="otherds")
        assert failed is True
        assert _error(payload)["code"] == "DATASOURCE_DENIED"

        failed, payload = await call_tool(
            session, "search_objects", {"datasource": "otherds", "pattern": "%"}
        )
        assert failed is True
        assert _error(payload)["code"] == "DATASOURCE_DENIED"

        failed, payload = await call_tool(
            session, "search_objects", {"datasource": "missing", "pattern": "%"}
        )
        assert failed is True
        error = _error(payload)
        assert error["code"] == "DATASOURCE_NOT_FOUND"
        assert "otherds" not in error["message"]

    with pytest.raises(Exception):
        await env.services.tokens.create(
            "bad-scope", TokenScope(datasources=frozenset({999999}))
        )


async def test_table_and_field_denied(env):
    scope = await _users_scope(env, ["phone"])
    plaintext, _ = await env.services.tokens.create("scoped-read", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM orders")
        assert failed is True
        assert _error(payload)["code"] == "TABLE_DENIED"

        failed, payload = await _exec(session, env, "SELECT email FROM users")
        assert failed is True
        error = _error(payload)
        assert error["code"] == "FIELD_DENIED"
        assert "email" in error["message"]

        failed, payload = await _exec(session, env, "SELECT * FROM users WHERE id = 1")
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

        failed, payload = await _exec(
            session, env, "SELECT * FROM users u JOIN orders o ON u.phone = o.order_no"
        )
        assert failed is True
        assert _error(payload)["code"] == "TABLE_DENIED"

        failed, payload = await _exec(
            session, env, "SELECT email AS phone FROM users"
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

        row = await env.services.audit.get(error["request_id"])
        assert row is not None
        assert row.status == "denied"


async def test_star_rewrite_and_masking(env):
    scope = await _users_scope(env, ["phone"])
    plaintext, _ = await env.services.tokens.create("scoped-star", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM users")
        assert failed is False, payload
        assert payload["columns"] == ["phone"]
        assert payload["row_count"] == 3
        assert payload["masked_columns"] == ["phone"]
        phones = [row[0] for row in payload["rows"]]
        assert None in phones
        assert sorted(item for item in phones if item) == ["136****6000", "138****8000"]

        failed, payload = await _exec(session, env, "SELECT u.* FROM users u")
        assert failed is False, payload
        assert payload["columns"] == ["phone"]
        assert payload["row_count"] == 3

        failed, payload = await _exec(
            session, env, "SELECT * FROM (SELECT * FROM users) t"
        )
        assert failed is False, payload
        assert payload["columns"] == ["phone"]

        failed, payload = await _exec(
            session, env, "WITH x AS (SELECT * FROM users) SELECT * FROM x"
        )
        assert failed is False, payload
        assert payload["columns"] == ["phone"]

        failed, payload = await _exec(
            session,
            env,
            "SELECT * FROM users WHERE phone = '13800138000'",
        )
        assert failed is False, payload
        assert payload["columns"] == ["phone"]
        assert payload["row_count"] == 1


async def test_join_star_rewrite_both_tables(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    scope = build_scope(
        [ds.id],
        [
            TableRef(ds.id, env.biz_db, "users"),
            TableRef(ds.id, env.biz_db, "orders"),
        ],
        [
            ColumnRef(ds.id, env.biz_db, "users", "phone"),
            ColumnRef(ds.id, env.biz_db, "orders", "order_no"),
        ],
    )
    plaintext, _ = await env.services.tokens.create("scoped-join", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(
            session,
            env,
            "SELECT * FROM users u JOIN orders o ON u.phone = o.order_no",
        )
        assert failed is False, payload
        assert payload["columns"] == ["phone", "order_no"]
        assert payload["row_count"] == 0

        failed, payload = await _exec(
            session,
            env,
            "SELECT u.phone, o.amount FROM users u JOIN orders o ON u.phone = o.order_no",
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

        failed, payload = await _exec(
            session,
            env,
            "SELECT * FROM users u JOIN orders o ON u.id = o.user_id",
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"


async def test_search_objects_filtered(env):
    scope = await _users_scope(env, ["phone"])
    plaintext, _ = await env.services.tokens.create("scoped-search", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await call_tool(
            session, "search_objects", {"datasource": env.ds_name, "pattern": "user"}
        )
        assert failed is False, payload
        assert payload["objects"] == [
            {
                "database": env.biz_db,
                "table": "users",
                "type": "table",
                "comment": "用户信息",
            },
            {
                "database": env.biz_db,
                "table": "users",
                "type": "column",
                "column": "phone",
                "data_type": "varchar",
                "comment": "手机号",
            },
        ]

        failed, payload = await call_tool(
            session,
            "search_objects",
            {"datasource": env.ds_name, "pattern": "order", "object_type": "all"},
        )
        assert failed is False, payload
        assert payload["objects"] == []


async def test_scoped_writes(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.set_read_only(ds.id, False)
    scope = await _users_scope(env, ["phone", "note"])
    plaintext, _ = await env.services.tokens.create("scoped-write", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(
            session, env, "UPDATE users SET phone = '13911112222' WHERE phone = '13800138000'"
        )
        assert failed is False, payload
        assert payload["row_count"] == 1

        failed, payload = await _exec(
            session, env, "UPDATE users SET email = 'x@example.com' WHERE phone = '13911112222'"
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

        failed, payload = await _exec(
            session,
            env,
            "INSERT INTO users (phone) VALUES ('13722223333')",
        )
        assert failed is False, payload

        failed, payload = await _exec(
            session,
            env,
            "INSERT INTO users (email) VALUES ('y@example.com')",
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

        failed, payload = await _exec(
            session, env, "INSERT INTO users VALUES (NULL, 'x', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
        )
        assert failed is True
        assert _error(payload)["code"] == "FIELDS_REQUIRED"

        failed, payload = await _exec(
            session, env, "DELETE FROM users WHERE phone = '19999999999'"
        )
        assert failed is False, payload

        engine = business_engine(env)
        try:
            async with engine.connect() as conn:
                result = await conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM users WHERE phone = '13911112222'"
                )
                assert result.scalar() == 1
                result = await conn.exec_driver_sql(
                    "SELECT email FROM users WHERE id = 1"
                )
                assert result.scalar() == "alice@example.com"
        finally:
            await engine.dispose()


async def test_scope_edit_takes_effect(env):
    scope = await _users_scope(env, ["phone"])
    plaintext, row = await env.services.tokens.create("scoped-edit", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT email FROM users")
        assert failed is True
        assert _error(payload)["code"] == "FIELD_DENIED"

    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.tokens.update_scope(
        row.id,
        build_scope(
            [ds.id],
            [TableRef(ds.id, env.biz_db, "users")],
            [ColumnRef(ds.id, env.biz_db, "users", "phone"), ColumnRef(ds.id, env.biz_db, "users", "email")],
        ),
    )
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT email FROM users")
        assert failed is False, payload
        assert payload["columns"] == ["email"]

    await env.services.tokens.update_scope(row.id, UNRESTRICTED)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM users")
        assert failed is False, payload
        assert len(payload["columns"]) == 10


async def test_describe_requires_full_fields(env):
    scope = await _users_scope(env, ["phone"])
    plaintext, _ = await env.services.tokens.create("scoped-describe", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "DESCRIBE users")
        assert failed is True
        assert _error(payload)["code"] == "FIELDS_REQUIRED"


# ---------- v1.8: database visibility and four-level authorization ----------


async def _expose(env, *names: str):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await databases.sync_on_save(
        env.services.session_factory, ds.id, [env.biz_db, *names], env.biz_db
    )
    return ds


async def test_unexposed_database_denied_even_for_unrestricted_token(env):
    await _expose(env, env.mgmt_db)
    async with mcp_session(env, token=env.token) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM mysql.user")
        assert failed is True
        error = _error(payload)
        assert error["code"] == "DATABASE_NOT_EXPOSED"
        assert "mysql" in error["message"]

        row = await env.services.audit.get(error["request_id"])
        assert row is not None
        assert row.status == "denied"


async def test_exposed_but_unauthorized_database_returns_database_denied(env):
    ds = await _expose(env, env.mgmt_db)
    scope = build_scope([ds.id], [TableRef(ds.id, env.biz_db, "users")], [])
    plaintext, _ = await env.services.tokens.create("db-denied", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(
            session, env, f"SELECT * FROM `{env.mgmt_db}`.admin_user"
        )
        assert failed is True
        error = _error(payload)
        assert error["code"] == "DATABASE_DENIED"
        assert env.mgmt_db in error["message"]
        assert "admin_user" not in error["message"]

        failed, payload = await _exec(session, env, "SELECT * FROM users")
        assert failed is False, payload

        failed, payload = await _exec(session, env, "SELECT * FROM ods.absent")
        assert failed is True
        assert _error(payload)["code"] == "DATABASE_NOT_EXPOSED"


async def test_cross_database_qualified_names_are_checked_per_database(env):
    ds = await _expose(env, env.mgmt_db)
    scope = build_scope(
        [ds.id],
        [
            TableRef(ds.id, env.biz_db, "users"),
            TableRef(ds.id, env.mgmt_db, "admin_user"),
        ],
        [],
    )
    plaintext, _ = await env.services.tokens.create("db-cross", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(
            session,
            env,
            f"SELECT 1 FROM users u JOIN `{env.mgmt_db}`.admin_user a ON 1 = 1 LIMIT 1",
        )
        assert failed is False, payload


async def test_library_level_all_tables_allows_existing_and_new_tables(env):
    ds = await _expose(env)
    engine = business_engine(env)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE extra_table (id INT)")
    finally:
        await engine.dispose()

    scope = build_scope(
        [ds.id], [], [], unrestricted_databases=[(ds.id, env.biz_db)]
    )
    plaintext, _ = await env.services.tokens.create("db-all", scope)
    async with mcp_session(env, token=plaintext) as session:
        failed, payload = await _exec(session, env, "SELECT * FROM users")
        assert failed is False, payload
        assert payload["row_count"] == 3

        failed, payload = await _exec(session, env, "SELECT * FROM extra_table")
        assert failed is False, payload
