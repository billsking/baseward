"""Policy-page table/column autocomplete endpoints: auth, fuzzy matching, and parameter validation."""

from __future__ import annotations

import pytest

from tests.integration.conftest import admin_login

pytestmark = pytest.mark.integration


async def test_metadata_suggest_requires_admin(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    for path in ("tables", "columns"):
        response = await env.client.get(
            f"/admin/datasources/{ds.id}/metadata/{path}"
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_table_suggest_fuzzy_search(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await admin_login(env)
    url = f"/admin/datasources/{ds.id}/metadata/tables"

    prefix = await env.client.get(url, params={"q": "ord", "database": env.biz_db})
    assert prefix.status_code == 200
    data = prefix.json()
    assert {"database": env.biz_db, "table": "orders"} in data["tables"]
    assert all(item["table"] != "users" for item in data["tables"])
    assert data["truncated"] is False

    infix = await env.client.get(url, params={"q": "der", "database": env.biz_db})
    assert "orders" in {item["table"] for item in infix.json()["tables"]}

    upper = await env.client.get(url, params={"q": "ORD", "database": env.biz_db})
    assert "orders" in {item["table"] for item in upper.json()["tables"]}

    missing = await env.client.get(url, params={"q": "no_such_table"})
    assert missing.json()["tables"] == []

    other_db = await env.client.get(url, params={"database": "no_such_database"})
    assert other_db.json()["tables"] == []

    all_tables = await env.client.get(url, params={"database": env.biz_db})
    names = {item["table"] for item in all_tables.json()["tables"]}
    assert {"users", "orders"} <= names


async def test_column_suggest_fuzzy_search(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await admin_login(env)
    url = f"/admin/datasources/{ds.id}/metadata/columns"
    base = {"database": env.biz_db, "table": "users"}

    prefix = await env.client.get(url, params={**base, "q": "ph"})
    assert prefix.status_code == 200
    data = prefix.json()
    assert [item["column"] for item in data["columns"]] == ["phone"]
    assert data["columns"][0]["data_type"] == "varchar"
    assert data["truncated"] is False

    infix = await env.client.get(url, params={**base, "q": "ail"})
    assert [item["column"] for item in infix.json()["columns"]] == ["email"]

    all_columns = await env.client.get(url, params=base)
    names = {item["column"] for item in all_columns.json()["columns"]}
    assert {"id", "phone", "email", "note"} <= names

    no_match = await env.client.get(url, params={**base, "q": "zzz"})
    assert no_match.json()["columns"] == []

    missing_table = await env.client.get(
        url, params={"database": env.biz_db, "table": "no_such_table"}
    )
    assert missing_table.json()["columns"] == []


async def test_metadata_suggest_rejects_invalid_arguments(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await admin_login(env)

    invalid_columns = await env.client.get(
        f"/admin/datasources/{ds.id}/metadata/columns",
        params={"database": "", "table": "users"},
    )
    assert invalid_columns.status_code == 400
    assert invalid_columns.json()["error"]["code"] == "INVALID_ARGUMENT"

    long_term = await env.client.get(
        f"/admin/datasources/{ds.id}/metadata/tables",
        params={"q": "a" * 129},
    )
    assert long_term.status_code == 400

    missing = await env.client.get(
        f"/admin/datasources/{ds.id + 9999}/metadata/tables"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
