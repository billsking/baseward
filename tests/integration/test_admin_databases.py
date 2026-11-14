"""Admin exposed-database multi-select: read, save, list display, and picker scope (v1.8 AC35)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.tokens.scope import TableRef, TokenScope
from tests.integration.conftest import admin_login

pytestmark = pytest.mark.integration


@asynccontextmanager
async def extra_databases(env, suffixes: tuple[str, ...]):
    names = [f"{env.biz_db}_{suffix}" for suffix in suffixes]
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
            for name in names:
                await conn.exec_driver_sql(
                    f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        yield names
    finally:
        async with engine.begin() as conn:
            for name in names:
                await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await engine.dispose()


async def test_picker_save_list_display_and_scope(env):
    csrf = await admin_login(env)
    async with extra_databases(env, ("rep", "hidden")) as (report_db, hidden_db):
        page = await env.client.get("/admin/datasources/new")
        assert page.status_code == 200
        assert "data-db-picker" in page.text
        assert "读取库清单" in page.text
        assert "按库名过滤" in page.text
        assert "全选" in page.text and "清空" in page.text

        refreshed = await env.client.post(
            "/admin/datasources/new/databases/refresh",
            data={
                "_csrf": csrf,
                "host": env.db_host,
                "port": str(env.db_port),
                "username": env.db_user,
                "password": env.db_password,
                "default_database": env.biz_db,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        upstream = refreshed.json()["databases"]
        assert {env.biz_db, report_db, hidden_db} <= set(upstream)

        created = await env.client.post(
            "/admin/datasources/new",
            data={
                "_csrf": csrf,
                "name": "multi-ds",
                "host": env.db_host,
                "port": str(env.db_port),
                "username": env.db_user,
                "password": env.db_password,
                "default_database": env.biz_db,
                "remark": "多库演示",
                "db_picker": "1",
                "databases": [env.biz_db, report_db],
            },
        )
        assert created.status_code == 302, created.text
        datasource = await env.services.datasources.get_by_name("multi-ds")
        assert datasource is not None
        policies = await env.services.datasources.list_databases(datasource.id)
        assert [item.name for item in policies] == sorted([env.biz_db, report_db])
        assert all(item.is_read_only for item in policies)

        listing = await env.client.get("/admin/datasources")
        assert listing.status_code == 200
        assert env.biz_db in listing.text and report_db in listing.text
        assert "默认" in listing.text
        assert "全部只读" in listing.text
        assert hidden_db not in listing.text

        scope = await env.client.get(
            f"/admin/tokens/scope/databases?datasource_id={datasource.id}"
        )
        assert scope.status_code == 200
        entries = scope.json()["databases"]
        assert [item["name"] for item in entries] == sorted([env.biz_db, report_db])
        assert hidden_db not in {item["name"] for item in entries}
        assert all(item["state"] == "all" for item in entries)

        edit_page = await env.client.get(f"/admin/datasources/{datasource.id}/edit")
        assert edit_page.status_code == 200
        assert report_db in edit_page.text

        edit_form = {
            "_csrf": csrf,
            "name": "multi-ds",
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": "",
            "default_database": env.biz_db,
            "remark": "多库演示",
            "db_picker": "1",
            "databases": [env.biz_db],
        }
        narrowed = await env.client.post(
            f"/admin/datasources/{datasource.id}/edit", data=edit_form
        )
        assert narrowed.status_code == 400
        assert "确认保存" in narrowed.text
        assert len(await env.services.datasources.list_databases(datasource.id)) == 2

        removed = await env.client.post(
            f"/admin/datasources/{datasource.id}/edit",
            data={**edit_form, "confirm": "1"},
        )
        assert removed.status_code == 302, removed.text
        assert [item.name for item in await env.services.datasources.list_databases(datasource.id)] == [
            env.biz_db
        ]

        readded = await env.client.post(
            f"/admin/datasources/{datasource.id}/edit",
            data={**edit_form, "databases": [env.biz_db, report_db]},
        )
        assert readded.status_code == 302, readded.text
        policies = await env.services.datasources.list_databases(datasource.id)
        assert {item.name: item.is_read_only for item in policies} == {
            env.biz_db: True,
            report_db: True,
        }


async def test_scope_states_follow_token_authorization(env):
    csrf = await admin_login(env)
    async with extra_databases(env, ("rep",)) as (report_db,):
        created = await env.client.post(
            "/admin/datasources/new",
            data={
                "_csrf": csrf,
                "name": "state-ds",
                "host": env.db_host,
                "port": str(env.db_port),
                "username": env.db_user,
                "password": env.db_password,
                "default_database": env.biz_db,
                "remark": "",
                "db_picker": "1",
                "databases": [env.biz_db, report_db],
            },
        )
        assert created.status_code == 302, created.text
        datasource = await env.services.datasources.get_by_name("state-ds")
        assert datasource is not None

        _, restricted = await env.services.tokens.create(
            "report-partial",
            TokenScope(
                datasources=frozenset({datasource.id}),
                tables=frozenset({TableRef(datasource.id, report_db, "users")}),
            ),
        )

        partial = await env.client.get(
            f"/admin/tokens/scope/databases?datasource_id={datasource.id}&token_id={restricted.id}"
        )
        assert partial.status_code == 200
        states = {item["name"]: item["state"] for item in partial.json()["databases"]}
        assert states == {env.biz_db: "none", report_db: "partial"}
