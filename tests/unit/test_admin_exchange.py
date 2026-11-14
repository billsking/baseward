"""Admin UI for token kinds and short-lived credentials (SQLite, no MySQL)."""

from __future__ import annotations

import base64
import re

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import select

from app.config import Settings
from app.db.models import ApiToken, ShortCredential
from app.main import create_app

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")
ACTOR_HEADERS = {
    "X-BaseWard-Actor-Provider": "dingtalk",
    "X-BaseWard-Actor-Id": "manager1234",
}


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
            yield app.state.services, client, csrf


async def _create_token(client, csrf, *, kind="access", name="bot", policy="ignore"):
    return await client.post(
        "/admin/tokens/new",
        data={
            "_csrf": csrf,
            "user_name": name,
            "actor_policy": policy,
            "token_kind": kind,
        },
    )


async def test_create_exchange_token_forces_require(env) -> None:
    services, client, csrf = env
    response = await _create_token(client, csrf, kind="exchange", policy="ignore")
    assert response.status_code == 200
    assert "gw_" in response.text
    async with services.session_factory() as session:
        row = await session.scalar(select(ApiToken).where(ApiToken.user_name == "bot"))
    assert row.token_kind == "exchange"
    assert row.actor_policy == "require"
    listing = await client.get("/admin/tokens")
    assert 'name="token_kind"' in listing.text
    assert "仅换短期凭证" in listing.text


async def test_create_access_token_by_default(env) -> None:
    services, client, csrf = env
    await _create_token(client, csrf, name="alice")
    async with services.session_factory() as session:
        row = await session.scalar(select(ApiToken).where(ApiToken.user_name == "alice"))
    assert row.token_kind == "access"
    assert row.actor_policy == "ignore"


async def test_create_rejects_unknown_kind(env) -> None:
    _, client, csrf = env
    response = await _create_token(client, csrf, kind="root")
    assert response.status_code == 200
    assert "token_kind 必须是 access/exchange 之一" in response.text


async def test_exchange_policy_downgrade_rejected(env) -> None:
    services, client, csrf = env
    await _create_token(client, csrf, kind="exchange")
    async with services.session_factory() as session:
        row = await session.scalar(select(ApiToken))
    response = await client.post(
        f"/admin/tokens/{row.id}/policy", data={"_csrf": csrf, "actor_policy": "ignore"}
    )
    assert response.status_code == 400
    assert "固定为 require" in response.text
    async with services.session_factory() as session:
        again = await session.get(ApiToken, row.id)
    assert again.actor_policy == "require"


async def test_short_credentials_page_and_revoke(env) -> None:
    services, client, csrf = env
    plaintext, _ = await services.tokens.create("bot", token_kind="exchange")
    issued = await client.post(
        "/api/exchange",
        headers={"Authorization": f"Bearer {plaintext}", **ACTOR_HEADERS},
        json={},
    )
    assert issued.status_code == 200
    page = await client.get("/admin/short-credentials")
    assert page.status_code == 200
    assert "manager1234" in page.text
    assert "sc_" in page.text
    async with services.session_factory() as session:
        row = await session.scalar(select(ShortCredential))
    revoked = await client.post(
        f"/admin/short-credentials/{row.id}/revoke", data={"_csrf": csrf}
    )
    assert revoked.status_code == 302
    assert "msg=short_credential_revoked" in revoked.headers["location"]
    async with services.session_factory() as session:
        again = await session.get(ShortCredential, row.id)
    assert again.revoked_at is not None
    follow = await client.get("/admin/short-credentials?msg=short_credential_revoked")
    assert "短期凭证已吊销" in follow.text


async def test_nav_links_short_credentials(env) -> None:
    _, client, _ = env
    page = await client.get("/admin")
    assert "/admin/short-credentials" in page.text