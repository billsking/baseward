"""SQLite management store: the default when no server database is configured."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import text

from app.clock import utcnow
from app.config import Settings
from app.db.models import (
    ApiToken,
    DataSource,
    DataSourceDatabase,
    ExportCredential,
    MaskingRule,
    TokenScopeRow,
)
from app.db.schema import create_management_engine, create_session_factory, ensure_schema
from app.main import create_app

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")
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


@pytest.fixture
def sqlite_settings(monkeypatch, tmp_path) -> tuple[Settings, str]:
    for key in (
        "GATEWAY_DB_URL",
        "GATEWAY_DB_HOST",
        "GATEWAY_DB_USER",
        "GATEWAY_DB_PASSWORD",
        "GATEWAY_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    db_path = tmp_path / "nested" / "gateway.db"
    settings = Settings(
        _env_file=None,
        master_key=MASTER_KEY,
        admin_user="admin",
        admin_password="admin-test-123",
        db_path=str(db_path),
        log_level="WARNING",
    )
    return settings, str(db_path)


async def test_app_boots_without_external_database(sqlite_settings) -> None:
    settings, db_path = sqlite_settings
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            login = await client.get("/admin/login")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert login.status_code == 200
    # The default path lives under a directory that does not exist before the first start.
    assert (settings.management_url.database or "").endswith("/nested/gateway.db")
    assert Path(db_path).exists()


async def test_schema_pragmas_and_foreign_key_cascades(sqlite_settings) -> None:
    settings, _ = sqlite_settings
    engine = create_management_engine(settings)
    try:
        await ensure_schema(engine)
        await ensure_schema(engine)  # idempotent replay

        factory = create_session_factory(engine)
        async with factory() as session:
            journal = await session.scalar(text("PRAGMA journal_mode"))
            foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
            tables = set(
                (await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars()
            )
            retention = await session.scalar(
                text("SELECT setting_value FROM system_setting WHERE setting_key='audit_retention_days'")
            )
        assert journal == "wal"
        assert foreign_keys == 1
        assert EXPECTED_TABLES <= tables
        assert retention == "180"

        now = utcnow()
        async with factory() as session:
            datasource = DataSource(
                name="ds",
                host="127.0.0.1",
                port=3306,
                username="u",
                password_cipher="cipher",
                default_database="demo",
                is_read_only=1,
                enabled=1,
                created_at=now,
                updated_at=now,
            )
            token = ApiToken(
                user_name="u",
                token_hash="h" * 64,
                token_prefix="gw_test",
                enabled=1,
                created_at=now,
            )
            session.add_all([datasource, token])
            await session.flush()
            session.add_all(
                [
                    DataSourceDatabase(
                        datasource_id=datasource.id,
                        name="demo",
                        is_read_only=1,
                        created_at=now,
                        updated_at=now,
                    ),
                    MaskingRule(
                        datasource_id=datasource.id,
                        database_name="demo",
                        table_name="users",
                        column_name="phone",
                        strategy="mask",
                        created_at=now,
                        updated_at=now,
                    ),
                    TokenScopeRow(
                        token_id=token.id,
                        scope_json="{}",
                        created_at=now,
                        updated_at=now,
                    ),
                    ExportCredential(
                        account_name="gw_export",
                        auth_string="a" * 40,
                        token_id=token.id,
                        datasource_id=datasource.id,
                        default_database="demo",
                        issued_by="admin",
                        expires_at=now,
                        created_at=now,
                    ),
                ]
            )
            await session.commit()
            datasource_id, token_id = datasource.id, token.id

        # The services rely on ON DELETE CASCADE instead of deleting child rows themselves.
        async with factory() as session:
            await session.delete(await session.get(DataSource, datasource_id))
            await session.delete(await session.get(ApiToken, token_id))
            await session.commit()

        async with factory() as session:
            for table in (
                "datasource",
                "datasource_database",
                "masking_rule",
                "api_token",
                "token_scope",
                "export_credential",
            ):
                count = await session.scalar(text(f"SELECT COUNT(*) FROM {table}"))
                assert count == 0, table
    finally:
        await engine.dispose()


async def test_missing_columns_are_added_to_existing_sqlite_files(sqlite_settings) -> None:
    settings, _ = sqlite_settings
    engine = create_management_engine(settings)
    try:
        await ensure_schema(engine)
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE audit_log DROP COLUMN credential_account")

        await ensure_schema(engine)

        async with engine.connect() as conn:
            columns = [
                row[1]
                for row in (await conn.exec_driver_sql("PRAGMA table_info(audit_log)")).fetchall()
            ]
        assert columns.count("credential_account") == 1
    finally:
        await engine.dispose()


async def test_missing_actor_columns_and_index_are_added(sqlite_settings) -> None:
    settings, _ = sqlite_settings
    engine = create_management_engine(settings)
    try:
        await ensure_schema(engine)
        async with engine.begin() as conn:
            # SQLite refuses to drop an indexed column; remove the index first.
            await conn.exec_driver_sql("DROP INDEX idx_audit_actor")
            for column in ("actor_provider", "actor_id", "actor_name", "actor_trust"):
                await conn.exec_driver_sql(f"ALTER TABLE audit_log DROP COLUMN {column}")
            await conn.exec_driver_sql("ALTER TABLE api_token DROP COLUMN actor_policy")

        await ensure_schema(engine)

        async with engine.connect() as conn:
            audit_columns = {
                row[1]
                for row in (await conn.exec_driver_sql("PRAGMA table_info(audit_log)")).fetchall()
            }
            token_columns = {
                row[1]
                for row in (await conn.exec_driver_sql("PRAGMA table_info(api_token)")).fetchall()
            }
            audit_indexes = {
                row[1]
                for row in (await conn.exec_driver_sql("PRAGMA index_list(audit_log)")).fetchall()
            }
        assert {"actor_provider", "actor_id", "actor_name", "actor_trust"} <= audit_columns
        assert "actor_policy" in token_columns
        assert "idx_audit_actor" in audit_indexes
    finally:
        await engine.dispose()
