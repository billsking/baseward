"""Datasource exposed databases: service validation, database-list read, and effective policy (v1.8 T42)."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.tokens.scope import TableRef, TokenScope
from tests.integration.conftest import admin_login

pytestmark = pytest.mark.integration


@asynccontextmanager
async def report_database(env):
    """Create a second upstream database and drop it when the test ends."""
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
    try:
        yield name
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        await engine.dispose()


def _connection_form(env, csrf: str) -> dict[str, str]:
    return {
        "_csrf": csrf,
        "host": env.db_host,
        "port": str(env.db_port),
        "username": env.db_user,
        "password": env.db_password,
        "default_database": env.biz_db,
    }


async def _create_datasource(env, csrf: str, name: str, database_names: list[str]):
    response = await env.client.post(
        "/admin/datasources/new",
        data={
            **_connection_form(env, csrf),
            "name": name,
            "remark": "",
            "db_picker": "1",
            "databases": database_names,
        },
    )
    assert response.status_code == 302, response.text
    datasource = await env.services.datasources.get_by_name(name)
    assert datasource is not None
    return datasource


async def test_refresh_new_form_reads_upstream_databases(env):
    csrf = await admin_login(env)
    async with report_database(env) as report_db:
        response = await env.client.post(
            "/admin/datasources/new/databases/refresh",
            data=_connection_form(env, csrf),
        )
        assert response.status_code == 200, response.text
        names = response.json()["databases"]
        assert env.biz_db in names
        assert report_db in names
        assert "mysql" not in names

    missing_password = await env.client.post(
        "/admin/datasources/new/databases/refresh",
        data={**_connection_form(env, csrf), "password": ""},
    )
    assert missing_password.status_code == 400
    assert "连接参数" in missing_password.text

    unreachable = await env.client.post(
        "/admin/datasources/new/databases/refresh",
        data={**_connection_form(env, csrf), "port": "1"},
    )
    assert unreachable.status_code == 502
    assert "库清单" in unreachable.text


async def test_refresh_new_form_before_default_database_is_pickable(env):
    """The new-form default database is a select fed by the refresh result, so it is empty on the first read."""
    csrf = await admin_login(env)
    page = await env.client.get("/admin/datasources/new")
    assert page.status_code == 200
    select = re.search(
        r'<select name="default_database".*?</select>', page.text, re.DOTALL
    )
    assert select is not None, "新建页缺少默认库下拉"
    assert re.findall(r'<option value="([^"]*)"', select.group(0)) == [""]

    response = await env.client.post(
        "/admin/datasources/new/databases/refresh",
        data={**_connection_form(env, csrf), "default_database": ""},
    )
    assert response.status_code == 200, response.text
    assert env.biz_db in response.json()["databases"]


async def test_saved_refresh_and_effective_policies(env):
    csrf = await admin_login(env)
    async with report_database(env) as report_db:
        datasource = await _create_datasource(
            env, csrf, "policy-ds", [env.biz_db, report_db]
        )
        policies = await env.services.datasources.list_databases(datasource.id)
        assert [item.name for item in policies] == sorted([env.biz_db, report_db])
        assert all(item.is_read_only for item in policies)

        refreshed = await env.client.post(
            f"/admin/datasources/{datasource.id}/databases/refresh",
            data={"_csrf": csrf},
        )
        assert refreshed.status_code == 200
        assert set(refreshed.json()["databases"]) >= {env.biz_db, report_db}

        # New datasources default to read-only at the master switch; open it up first to verify independent per-database policy.
        await env.services.datasources.set_read_only(datasource.id, False)
        async with env.services.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE datasource_database SET is_read_only=0 "
                    "WHERE datasource_id=:datasource_id AND name=:name"
                ),
                {"datasource_id": datasource.id, "name": report_db},
            )
            await session.commit()

        shown = await env.client.get(f"/admin/datasources/{datasource.id}/databases")
        assert shown.status_code == 200
        by_name = {item["name"]: item["read_only"] for item in shown.json()["databases"]}
        assert by_name == {env.biz_db: True, report_db: False}

        await env.services.datasources.set_read_only(datasource.id, True)
        merged = await env.client.get(f"/admin/datasources/{datasource.id}/databases")
        assert all(item["read_only"] for item in merged.json()["databases"])


async def test_upstream_failure_keeps_saved_list_but_blocks_new_save(env):
    csrf = await admin_login(env)
    blocked = await env.client.post(
        "/admin/datasources/new",
        data={
            **_connection_form(env, csrf),
            "name": "no-db-list",
            "remark": "",
            "db_picker": "1",
        },
    )
    assert blocked.status_code == 400
    assert "至少选择一个可访问库" in blocked.text
    assert await env.services.datasources.get_by_name("no-db-list") is None

    datasource = await env.services.datasources.get_by_name(env.ds_name)
    async with env.services.session_factory() as session:
        await session.execute(
            text("UPDATE datasource SET port=1 WHERE id=:id"), {"id": datasource.id}
        )
        await session.commit()

    failed = await env.client.post(
        f"/admin/datasources/{datasource.id}/databases/refresh",
        data={"_csrf": csrf},
    )
    assert failed.status_code == 502
    page = await env.client.get(f"/admin/datasources/{datasource.id}/edit")
    assert page.status_code == 200
    assert env.biz_db in page.text


async def test_database_names_are_validated(env):
    csrf = await admin_login(env)
    duplicate = await env.client.post(
        "/admin/datasources/new",
        data={
            **_connection_form(env, csrf),
            "name": "dup-db",
            "remark": "",
            "db_picker": "1",
            "databases": [env.biz_db, env.biz_db],
        },
    )
    assert duplicate.status_code == 400
    assert "重复项" in duplicate.text
    assert await env.services.datasources.get_by_name("dup-db") is None

    overlong = await env.client.post(
        "/admin/datasources/new",
        data={
            **_connection_form(env, csrf),
            "name": "long-db",
            "remark": "",
            "db_picker": "1",
            "databases": [env.biz_db, "x" * 129],
        },
    )
    assert overlong.status_code == 400
    assert "长度不能超过" in overlong.text
    assert await env.services.datasources.get_by_name("long-db") is None

    outside = await env.client.post(
        "/admin/datasources/new",
        data={
            **_connection_form(env, csrf),
            "name": "default-outside",
            "remark": "",
            "db_picker": "1",
            "databases": ["other_db"],
        },
    )
    assert outside.status_code == 400
    assert "默认库必须在可访问库内" in outside.text
    assert await env.services.datasources.get_by_name("default-outside") is None


async def test_removing_database_needs_confirm_and_deletes_policy_rows(env):
    csrf = await admin_login(env)
    async with report_database(env) as report_db:
        datasource = await _create_datasource(
            env, csrf, "narrow-ds", [env.biz_db, report_db]
        )
        await env.services.tokens.create(
            "report-reader",
            TokenScope(
                datasources=frozenset({datasource.id}),
                tables=frozenset(
                    {TableRef(datasource.id, report_db, "users")}
                ),
            ),
        )

        edit = {
            **_connection_form(env, csrf),
            "name": "narrow-ds",
            "remark": "",
            "db_picker": "1",
            "databases": [env.biz_db],
        }
        asked = await env.client.post(
            f"/admin/datasources/{datasource.id}/edit", data=edit
        )
        assert asked.status_code == 400
        assert "1 个 Token 的授权将失效" in asked.text
        assert "1 个库策略行会删除" in asked.text
        assert len(await env.services.datasources.list_databases(datasource.id)) == 2

        confirmed = await env.client.post(
            f"/admin/datasources/{datasource.id}/edit",
            data={**edit, "confirm": "1"},
        )
        assert confirmed.status_code == 302, confirmed.text
        remaining = await env.services.datasources.list_databases(datasource.id)
        assert [item.name for item in remaining] == [env.biz_db]
