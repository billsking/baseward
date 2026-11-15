"""New-datasource database-list refresh: the default database is not yet pickable (SQLite, no MySQL)."""

from __future__ import annotations

import base64
import re

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.datasources import databases as databases_module
from app.main import create_app

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")
UPSTREAM = ["mall_it", "mall_report"]


@pytest.fixture
def settings(monkeypatch, tmp_path) -> Settings:
    for key in (
        "GATEWAY_DB_URL",
        "GATEWAY_DB_HOST",
        "GATEWAY_DB_USER",
        "GATEWAY_DB_PASSWORD",
        "GATEWAY_DB_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings(
        _env_file=None,
        master_key=MASTER_KEY,
        admin_user="admin",
        admin_password="admin-test-123",
        db_path=str(tmp_path / "gateway.db"),
        log_level="WARNING",
    )


@pytest.fixture
async def env(settings):
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.post(
                "/admin/login", data={"username": "admin", "password": "admin-test-123"}
            )
            assert response.status_code == 302
            dashboard = await client.get("/admin")
            csrf = re.search(r'name="_csrf" value="([^"]+)"', dashboard.text).group(1)
            yield client, csrf


@pytest.fixture
def upstream(monkeypatch):
    seen: list[str] = []

    async def fake_list(datasource, *, master_key=None):
        seen.append(datasource.default_database)
        return list(UPSTREAM)

    monkeypatch.setattr(databases_module, "list_upstream_databases", fake_list)
    return seen


def _form(csrf: str, **overrides) -> dict[str, str]:
    return {
        "_csrf": csrf,
        "host": "db.example.com",
        "port": "3306",
        "username": "mall_it",
        "password": "secret-password",
        "default_database": "",
        **overrides,
    }


async def test_new_form_default_database_starts_empty(env) -> None:
    client, _ = env
    page = await client.get("/admin/datasources/new")
    assert page.status_code == 200
    select = re.search(
        r'<select name="default_database".*?</select>', page.text, re.DOTALL
    )
    assert select is not None
    assert re.findall(r'<option value="([^"]*)"', select.group(0)) == [""]


async def test_refresh_accepts_empty_default_database(env, upstream) -> None:
    client, csrf = env
    response = await client.post(
        "/admin/datasources/new/databases/refresh", data=_form(csrf)
    )
    assert response.status_code == 200, response.text
    assert response.json()["databases"] == UPSTREAM
    assert upstream == [""]


@pytest.mark.parametrize(
    "override",
    [
        {"password": ""},
        {"host": ""},
        {"username": ""},
        {"port": "0"},
        {"port": "65536"},
        {"port": "abc"},
    ],
)
async def test_refresh_still_rejects_incomplete_connection(
    env, upstream, override
) -> None:
    client, csrf = env
    response = await client.post(
        "/admin/datasources/new/databases/refresh", data=_form(csrf, **override)
    )
    assert response.status_code == 400
    assert "连接参数" in response.text
    assert upstream == []