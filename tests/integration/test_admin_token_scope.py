"""Admin token authorization: metadata endpoints plus creating and editing scopes."""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import admin_login

pytestmark = pytest.mark.integration


def _scope_payload(env, ds_id: int, table: str, columns: list[str]) -> str:
    return json.dumps(
        {
            "datasources": [ds_id],
            "tables": [[ds_id, env.biz_db, table]],
            "columns": [[ds_id, env.biz_db, table, column] for column in columns],
        }
    )


async def test_scope_picker_metadata_requires_admin(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    response = await env.client.get(
        f"/admin/tokens/scope/tables?datasource_id={ds.id}"
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"

    presets = await env.client.get("/admin/tokens/scope/presets")
    assert presets.status_code == 401
    assert presets.json()["error"]["code"] == "UNAUTHORIZED"

    token_row = (await env.services.tokens.list_all())[0]
    snapshot = await env.client.get(f"/admin/tokens/{token_row.id}/scope")
    assert snapshot.status_code == 401

    resolved = await env.client.post(
        "/admin/tokens/scope/resolve",
        json={"datasource_id": ds.id, "items": []},
    )
    assert resolved.status_code == 401
    assert resolved.json()["error"]["code"] == "UNAUTHORIZED"


async def test_scope_picker_page_and_metadata(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await admin_login(env)

    page = await env.client.get("/admin/tokens")
    assert page.status_code == 200
    assert "授权范围" in page.text
    assert "/admin/static/token_scope.js" in page.text
    assert f'data-ds-id="{ds.id}"' in page.text

    tables = await env.client.get(
        "/admin/tokens/scope/tables",
        params={"datasource_id": ds.id, "database": env.biz_db},
    )
    assert tables.status_code == 200
    payload = tables.json()
    assert payload["default_database"] == env.biz_db
    rows = [
        {"database": item["database"], "table": item["table"]}
        for item in payload["tables"]
    ]
    assert {"database": env.biz_db, "table": "users"} in rows
    assert payload["total"] == len(rows)
    assert payload["has_more"] is False
    assert payload["truncated"] is False
    assert {"database": env.biz_db, "total": len(rows)} in payload["databases"]

    searched = await env.client.get(
        "/admin/tokens/scope/tables",
        params={
            "datasource_id": ds.id,
            "database": env.biz_db,
            "q": "phone",
            "match": "column",
        },
    )
    assert searched.status_code == 200
    found = searched.json()
    assert any(
        item["table"] == "users"
        and {"column": "phone", "data_type": "varchar"} in item["matched_columns"]
        for item in found["tables"]
    )

    columns = await env.client.get(
        "/admin/tokens/scope/columns",
        params={
            "datasource_id": ds.id,
            "database": env.biz_db,
            "table": "users",
        },
    )
    assert columns.status_code == 200
    data = columns.json()
    names = {item["column"] for item in data["columns"]}
    assert {"id", "phone", "email", "note"} <= names
    phone = next(item for item in data["columns"] if item["column"] == "phone")
    assert phone["data_type"] == "varchar"
    assert phone["sensitive"] is True
    assert next(item for item in data["columns"] if item["column"] == "id")["sensitive"] is False
    assert data["total"] == len(data["columns"])
    assert data["has_more"] is False

    missing = await env.client.get(
        f"/admin/tokens/scope/tables?datasource_id={ds.id + 9999}"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    invalid = await env.client.get(
        "/admin/tokens/scope/columns",
        params={"datasource_id": ds.id, "database": "", "table": "users"},
    )
    assert invalid.status_code == 400


async def test_admin_creates_and_edits_scoped_token(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    csrf = await admin_login(env)

    created = await env.client.post(
        "/admin/tokens/new",
        data={
            "_csrf": csrf,
            "user_name": "scoped-ui",
            "scope_payload": _scope_payload(env, ds.id, "users", ["phone"]),
        },
    )
    assert created.status_code == 200
    assert "新 Token 已创建" in created.text

    row = next(
        item
        for item in await env.services.tokens.list_all()
        if item.user_name == "scoped-ui"
    )
    scope = await env.services.tokens.scope_for(row.id)
    assert scope.allowed_columns(ds.id, env.biz_db, "users") == frozenset({"phone"})
    assert scope.allowed_columns(ds.id, env.biz_db, "orders") == frozenset()

    page = await env.client.get("/admin/tokens")
    assert "数据源 1 · 表 1 · 字段 1" in page.text

    edit = await env.client.get(f"/admin/tokens/{row.id}/edit")
    assert edit.status_code == 200
    assert "保存授权范围" in edit.text
    assert "不会轮换 Token 明文" in edit.text

    invalid = await env.client.post(
        f"/admin/tokens/{row.id}/scope",
        data={
            "_csrf": csrf,
            "scope_payload": json.dumps(
                {
                    "datasources": [],
                    "tables": [[ds.id, env.biz_db, "users"]],
                    "columns": [],
                }
            ),
        },
    )
    assert invalid.status_code == 400
    assert "选择表之前必须先选择数据源" in invalid.text
    assert (
        await env.services.tokens.scope_for(row.id)
    ).allowed_columns(ds.id, env.biz_db, "users") == frozenset({"phone"})

    updated = await env.client.post(
        f"/admin/tokens/{row.id}/scope",
        data={
            "_csrf": csrf,
            "scope_payload": _scope_payload(
                env, ds.id, "users", ["phone", "email"]
            ),
        },
    )
    assert updated.status_code == 302
    assert updated.headers["location"].startswith("/admin/tokens?msg=token_scope_updated")
    scope = await env.services.tokens.scope_for(row.id)
    assert scope.allowed_columns(ds.id, env.biz_db, "users") == frozenset(
        {"phone", "email"}
    )

    unrestricted = await env.client.post(
        f"/admin/tokens/{row.id}/scope",
        data={"_csrf": csrf, "scope_payload": ""},
    )
    assert unrestricted.status_code == 302
    assert not (await env.services.tokens.scope_for(row.id)).restricted

    broken = await env.client.post(
        f"/admin/tokens/{row.id}/scope",
        data={"_csrf": csrf, "scope_payload": "{not-json"},
    )
    assert broken.status_code == 400
    assert "授权范围格式不正确" in broken.text


async def test_scope_presets_and_scope_snapshot(env):
    await admin_login(env)

    presets = await env.client.get("/admin/tokens/scope/presets")
    assert presets.status_code == 200
    tester = next(
        item for item in presets.json()["tokens"] if item["user_name"] == "tester"
    )
    assert tester["enabled"] is True
    assert tester["summary"] == "全部"

    snapshot = await env.client.get(f"/admin/tokens/{tester['id']}/scope")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["token_id"] == tester["id"]
    assert body["scope"] == {"datasources": [], "tables": [], "columns": []}

    missing = await env.client.get("/admin/tokens/999999/scope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


async def test_scope_resolve_import_list(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    csrf = await admin_login(env)
    body = {
        "datasource_id": ds.id,
        "items": [
            [env.biz_db, "users"],
            [env.biz_db, "users", "phone"],
            [env.biz_db, "ghost"],
        ],
    }
    resolved = await env.client.post(
        "/admin/tokens/scope/resolve",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )
    assert resolved.status_code == 200
    data = resolved.json()
    assert [env.biz_db, "users"] in data["matched"]
    assert [env.biz_db, "users", "phone"] in data["matched"]
    assert [env.biz_db, "ghost"] in data["missing"]

    no_csrf = await env.client.post("/admin/tokens/scope/resolve", json=body)
    assert no_csrf.status_code == 400
    assert no_csrf.json()["error"]["code"] == "CSRF_FAILED"

    too_many = await env.client.post(
        "/admin/tokens/scope/resolve",
        json={"datasource_id": ds.id, "items": [[env.biz_db, "users"]] * 2001},
        headers={"X-CSRF-Token": csrf},
    )
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "INVALID_ARGUMENT"


async def test_admin_saves_tristate_scope(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    csrf = await admin_login(env)
    payload = {
        "datasources": [ds.id],
        "tables": [],
        "columns": [],
        "unrestricted_tables": [ds.id],
    }
    created = await env.client.post(
        "/admin/tokens/new",
        data={
            "_csrf": csrf,
            "user_name": "tristate-ui",
            "scope_payload": json.dumps(payload),
        },
    )
    assert created.status_code == 200
    row = next(
        item
        for item in await env.services.tokens.list_all()
        if item.user_name == "tristate-ui"
    )
    scope = await env.services.tokens.scope_for(row.id)
    assert scope.unrestricted_tables == frozenset({ds.id})
    assert scope.table_pairs(ds.id) is None

    page = await env.client.get("/admin/tokens")
    assert "数据源 1 · 表 全部(1 个数据源) · 字段 全部" in page.text
