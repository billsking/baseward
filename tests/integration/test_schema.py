"""Management schema creation and the admin seed."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.schema import ensure_schema, seed_admin

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "admin_user",
    "datasource",
    "datasource_database",
    "masking_rule",
    "api_token",
    "token_scope",
    "audit_log",
    "system_setting",
    "export_credential",
    "short_credential",
}


async def _table_names(env) -> set[str]:
    async with env.services.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :name"
            ),
            {"name": env.mgmt_db},
        )
        return {row[0] for row in result.fetchall()}


async def _column_names(env, table: str) -> list[str]:
    async with env.services.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :db AND table_name = :table ORDER BY ordinal_position"
            ),
            {"db": env.mgmt_db, "table": table},
        )
        return [row[0] for row in result.fetchall()]


async def test_schema_tables_and_idempotent(env):
    tables = await _table_names(env)
    assert EXPECTED_TABLES <= tables
    assert "credential_account" in await _column_names(env, "audit_log")
    assert "database_name" in await _column_names(env, "audit_log")

    # Re-running raises nothing and creates no duplicate data.
    await ensure_schema(env.services.engine)
    async with env.services.session_factory() as session:
        await seed_admin(session, env.settings)
    async with env.services.session_factory() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM admin_user"))
    assert count == 1


async def test_ensure_schema_upgrades_v13_database(env):
    """A database with the v1.3 schema: missing tables and columns are added, and re-running raises nothing."""
    async with env.services.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE export_credential")
        await conn.exec_driver_sql("ALTER TABLE audit_log DROP COLUMN credential_account")
    assert "export_credential" not in await _table_names(env)
    assert "credential_account" not in await _column_names(env, "audit_log")

    await ensure_schema(env.services.engine)
    await ensure_schema(env.services.engine)

    assert "export_credential" in await _table_names(env)
    columns = await _column_names(env, "audit_log")
    assert columns.count("credential_account") == 1


async def test_non_tolerated_schema_error_raises(env, tmp_path, monkeypatch):
    (tmp_path / "001_bad.sql").write_text("THIS IS NOT VALID SQL", encoding="utf-8")
    monkeypatch.setattr("app.db.schema.MIGRATIONS_DIR", tmp_path)
    with pytest.raises(DBAPIError):
        await ensure_schema(env.services.engine)


async def test_retention_default_seeded(env):
    days = await env.services.audit.get_retention_days()
    assert 1 <= days <= 3650


async def test_multidb_migration_backfills_and_inherits_read_only(env):
    """AC50: one backfilled row per legacy datasource (read-only/writable), inheriting the original master switch."""
    async with env.services.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE datasource_database")
        await conn.exec_driver_sql("ALTER TABLE audit_log DROP COLUMN database_name")
        await conn.exec_driver_sql(
            "UPDATE datasource SET is_read_only = 0 WHERE name = 'testds'"
        )
        await conn.exec_driver_sql(
            "INSERT INTO datasource "
            "(name, host, port, username, password_cipher, default_database, "
            "is_read_only, enabled, remark, created_at, updated_at) "
            "VALUES ('readonly_ds', '127.0.0.1', 3306, 'u', 'cipher', 'other_db', "
            "1, 1, NULL, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))"
        )

    await ensure_schema(env.services.engine)
    await ensure_schema(env.services.engine)

    async def _rows():
        async with env.services.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT d.name, d.default_database, d.is_read_only, "
                    "dd.name, dd.is_read_only "
                    "FROM datasource d JOIN datasource_database dd "
                    "ON dd.datasource_id = d.id ORDER BY d.name"
                )
            )
            return result.all()

    rows = await _rows()
    assert len(rows) == 2
    for ds_name, default_db, ds_read_only, dd_name, dd_read_only in rows:
        assert dd_name == default_db, ds_name
        assert dd_read_only == ds_read_only, ds_name

    # The second job of INSERT IGNORE: restore a default-database row that was deleted separately.
    async with env.services.engine.begin() as conn:
        await conn.exec_driver_sql(
            "DELETE dd FROM datasource_database dd "
            "JOIN datasource d ON d.id = dd.datasource_id WHERE d.name = 'testds'"
        )
    await ensure_schema(env.services.engine)
    assert len(await _rows()) == 2

    columns = await _column_names(env, "audit_log")
    assert columns.count("database_name") == 1

