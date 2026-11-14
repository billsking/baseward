"""Per-database read/write policy page and policy_change auditing (v1.8 AC49/E44)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import admin_login, call_tool, mcp_session

pytestmark = pytest.mark.integration


@asynccontextmanager
async def extra_database(env):
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
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            await conn.exec_driver_sql(
                f"CREATE TABLE `{name}`.notes "
                "(id INT NOT NULL PRIMARY KEY, note VARCHAR(32) NULL)"
            )
        yield name
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await engine.dispose()


async def configure_databases(env, report_db: str, *, total_read_only: bool):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    await env.services.datasources.update(
        ds.id,
        name=ds.name,
        host=ds.host,
        port=ds.port,
        username=ds.username,
        password="",
        default_database=ds.default_database,
        remark=ds.remark,
        databases=[env.biz_db, report_db],
    )
    await env.services.datasources.set_read_only(ds.id, total_read_only)
    return ds


async def policy_audits(env) -> list[dict]:
    async with env.services.session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT datasource_name, tool, database_name, deny_reason "
                    "FROM audit_log WHERE event_type='policy_change' ORDER BY id"
                )
            )
        ).all()
    return [dict(row._mapping) for row in rows]


async def insert_note(session, env, report_db: str, note_id: int, note: str):
    return await call_tool(
        session,
        "execute_sql",
        {
            "datasource": env.ds_name,
            "sql": (
                f"INSERT INTO `{report_db}`.notes (id, note) "
                f"VALUES ({note_id}, '{note}')"
            ),
        },
    )


async def test_database_policy_confirm_effect_and_audit(env):
    csrf = await admin_login(env)
    async with extra_database(env) as report_db:
        ds = await configure_databases(env, report_db, total_read_only=False)

        page = await env.client.get(f"/admin/datasources/{ds.id}/policy")
        assert page.status_code == 200
        assert "按库策略" in page.text
        assert report_db in page.text
        assert "确认改为可写" in page.text
        assert "总开关：可写" in page.text
        assert await policy_audits(env) == []

        async with mcp_session(env) as session:
            failed, payload = await insert_note(session, env, report_db, 1, "a")
            assert failed is True
            assert payload["error"]["code"] == "WRITE_ON_READONLY"

            missing = await env.client.post(
                f"/admin/datasources/{ds.id}/databases/{report_db}/policy",
                data={"_csrf": csrf, "read_only": "0"},
            )
            assert missing.status_code == 400
            assert "改为可写需勾选确认" in missing.text
            stored = await env.services.datasources.stored_database_read_only(ds.id)
            assert stored[report_db] is True
            assert await policy_audits(env) == []

            saved = await env.client.post(
                f"/admin/datasources/{ds.id}/databases/{report_db}/policy",
                data={"_csrf": csrf, "read_only": "0", "confirm": "1"},
            )
            assert saved.status_code == 302, saved.text
            audits = await policy_audits(env)
            assert len(audits) == 1
            assert audits[0]["datasource_name"] == ds.name
            assert audits[0]["tool"] == "datasource_policy"
            assert audits[0]["database_name"] is None
            assert audits[0]["deny_reason"] == f"{report_db}：只读 ⇒ 可写"

            ok, payload = await insert_note(session, env, report_db, 2, "b")
            assert ok is False, payload
            assert payload["row_count"] == 1

            readonly = await env.client.post(
                f"/admin/datasources/{ds.id}/databases/{report_db}/policy",
                data={"_csrf": csrf, "read_only": "1"},
            )
            assert readonly.status_code == 302, readonly.text
            failed, payload = await insert_note(session, env, report_db, 3, "c")
            assert failed is True
            assert payload["error"]["code"] == "WRITE_ON_READONLY"

        noop = await env.client.post(
            f"/admin/datasources/{ds.id}/databases/{report_db}/policy",
            data={"_csrf": csrf, "read_only": "1"},
        )
        assert noop.status_code == 302
        assert len(await policy_audits(env)) == 2

        toggled = await env.client.post(
            f"/admin/datasources/{ds.id}/policy",
            data={"_csrf": csrf, "is_read_only": "1"},
        )
        assert toggled.status_code == 302
        audits = await policy_audits(env)
        assert len(audits) == 3
        assert audits[-1]["deny_reason"] == "总开关：可写 ⇒ 只读"

        await env.client.post(
            f"/admin/datasources/{ds.id}/policy",
            data={"_csrf": csrf, "is_read_only": "1"},
        )
        assert len(await policy_audits(env)) == 3


async def test_preconfigured_writable_database_shows_e44(env):
    csrf = await admin_login(env)
    async with extra_database(env) as report_db:
        ds = await configure_databases(env, report_db, total_read_only=True)

        saved = await env.client.post(
            f"/admin/datasources/{ds.id}/databases/{report_db}/policy",
            data={"_csrf": csrf, "read_only": "0", "confirm": "1"},
        )
        assert saved.status_code == 302, saved.text

        stored = await env.services.datasources.stored_database_read_only(ds.id)
        assert stored[report_db] is False
        policies = {
            item.name: item.is_read_only
            for item in await env.services.datasources.list_databases(ds.id)
        }
        assert policies[report_db] is True

        page = await env.client.get(f"/admin/datasources/{ds.id}/policy")
        assert page.status_code == 200
        assert "只读（总开关）" in page.text
        assert "仅作预配置" in page.text

        audits = await policy_audits(env)
        assert [item["deny_reason"] for item in audits] == [f"{report_db}：只读 ⇒ 可写"]

        async with mcp_session(env) as session:
            failed, payload = await insert_note(session, env, report_db, 1, "a")
            assert failed is True
            assert payload["error"]["code"] == "WRITE_ON_READONLY"
