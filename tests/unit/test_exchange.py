"""Exchange tokens and short-lived credentials: endpoint, MCP edge, binding, and lifecycle."""

from __future__ import annotations

import base64
from datetime import timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import select, update

from app.clock import utcnow
from app.config import Settings
from app.db.models import ApiToken, AuditLog, ShortCredential
from app.errors import ValidationError
from app.exchange.service import ShortCredentialError
from app.main import create_app
from app.security.actor import Actor
from app.tokens.scope import TokenScope

MASTER_KEY = base64.b64encode(b"k" * 32).decode("ascii")
ACTOR_HEADERS = {
    "X-BaseWard-Actor-Provider": "dingtalk",
    "X-BaseWard-Actor-Id": "manager1234",
    "X-BaseWard-Actor-Name": base64.urlsafe_b64encode("李四".encode("utf-8"))
    .decode("ascii")
    .rstrip("="),
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "unit", "version": "1"},
    },
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
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app.state.services, client


async def _exchange_token(services, *, scope: TokenScope | None = None, name="bot"):
    plaintext, row = await services.tokens.create(
        name, scope=scope, token_kind="exchange"
    )
    return plaintext, row


async def _do_exchange(client, token, headers=None, json=None):
    return await client.post(
        "/api/exchange",
        headers={"Authorization": f"Bearer {token}", **(ACTOR_HEADERS if headers is None else headers)},
        json={} if json is None else json,
    )


async def test_exchange_issue_returns_short_credential(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    response = await _do_exchange(client, token)
    assert response.status_code == 200
    body = response.json()
    assert body["credential"].startswith("sc_")
    assert body["ttl_seconds"] == 900
    assert body["expires_at"].endswith("Z")
    assert body["actor"] == {"provider": "dingtalk", "id": "manager1234", "name": "李四"}


async def test_exchange_forces_require_policy(env) -> None:
    services, _ = env
    _, row = await services.tokens.create("bot", actor_policy="ignore", token_kind="exchange")
    assert row.actor_policy == "require"
    assert row.token_kind == "exchange"


async def test_exchange_policy_cannot_downgrade(env) -> None:
    services, _ = env
    _, row = await _exchange_token(services)
    with pytest.raises(ValidationError):
        await services.tokens.update_actor_policy(row.id, "ignore")


async def test_exchange_rejects_access_token(env) -> None:
    services, client = env
    token, _ = await services.tokens.create("alice")
    response = await _do_exchange(client, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "exchange_token_required"


async def test_exchange_rejects_missing_actor(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    response = await _do_exchange(client, token, headers={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_required"


async def test_exchange_rejects_invalid_actor(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    response = await _do_exchange(
        client, token, headers={"X-BaseWard-Actor-Provider": "DingTalk", "X-BaseWard-Actor-Id": "x"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_invalid"


async def test_exchange_rejects_bad_bearer(env) -> None:
    _, client = env
    response = await client.post(
        "/api/exchange", headers={"X-Request-ID": "trace-exchange"}, json={}
    )
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "missing_token",
        "message": "缺少 Bearer Token",
        "request_id": "trace-exchange",
    }
    assert response.headers["x-request-id"] == "trace-exchange"
    response = await client.post(
        "/api/exchange", headers={"Authorization": "Bearer gw_unknown"}, json={}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


async def test_exchange_ttl_bounds(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    assert (await _do_exchange(client, token, json={"ttl_minutes": 60})).status_code == 200
    too_long = await _do_exchange(client, token, json={"ttl_minutes": 61})
    assert too_long.status_code == 400
    assert too_long.json()["error"]["code"] == "invalid_request"
    assert (await _do_exchange(client, token, json={"ttl_minutes": 0})).status_code == 400


async def test_exchange_custom_ttl(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    body = (await _do_exchange(client, token, json={"ttl_minutes": 5})).json()
    assert body["ttl_seconds"] == 300


async def test_exchange_supersedes_previous_credential(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    first = (await _do_exchange(client, token)).json()["credential"]
    second = (await _do_exchange(client, token)).json()["credential"]
    assert first != second
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(first, "127.0.0.1")
    assert exc.value.code == "SHORT_CREDENTIAL_REVOKED"
    principal = await services.short_credentials.authenticate(second, "127.0.0.1")
    assert principal is not None


async def test_exchange_writes_audit_with_actor(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    await _do_exchange(client, token)
    async with services.session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.tool == "exchange_issue")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].actor_id == "manager1234"
    assert rows[0].actor_name == "李四"
    assert rows[0].actor_trust == "asserted"
    assert rows[0].user_name == "bot"


async def test_mcp_rejects_exchange_token(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    response = await client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {token}", **MCP_HEADERS},
        json=INITIALIZE,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "exchange_only"


async def test_mcp_accepts_short_credential_without_headers(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    response = await client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {credential}", **MCP_HEADERS},
        json=INITIALIZE,
    )
    assert response.status_code == 200
    async with services.session_factory() as session:
        row = await session.scalar(select(ShortCredential))
    assert row is not None
    assert row.last_used_at is not None


async def test_mcp_short_credential_actor_mismatch_denied(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    response = await client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {credential}",
            "X-BaseWard-Actor-Provider": "dingtalk",
            "X-BaseWard-Actor-Id": "someone-else",
            **MCP_HEADERS,
        },
        json=INITIALIZE,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_mismatch"


async def test_mcp_short_credential_matching_headers_allowed(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    response = await client.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {credential}", **ACTOR_HEADERS, **MCP_HEADERS},
        json=INITIALIZE,
    )
    assert response.status_code == 200


async def test_short_credential_expiry_and_revocation(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    async with services.session_factory() as session:
        row = await session.scalar(select(ShortCredential))
        credential_id = row.id
        await session.execute(
            update(ShortCredential)
            .where(ShortCredential.id == credential_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert exc.value.code == "SHORT_CREDENTIAL_EXPIRED"

    await services.short_credentials.revoke(credential_id, "admin", "127.0.0.1")
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert exc.value.code == "SHORT_CREDENTIAL_REVOKED"


async def test_short_credential_ip_binding(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(credential, "10.0.0.9")
    assert exc.value.code == "SHORT_CREDENTIAL_IP_MISMATCH"


async def test_short_credential_parent_disabled(env) -> None:
    services, client = env
    token, row = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    await services.tokens.disable(row.id)
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert exc.value.code == "PARENT_TOKEN_DISABLED"


async def test_short_credential_parent_expired(env) -> None:
    services, client = env
    token, row = await _exchange_token(services)
    credential = (await _do_exchange(client, token)).json()["credential"]
    async with services.session_factory() as session:
        await session.execute(
            update(ApiToken).where(ApiToken.id == row.id).values(expires_at=utcnow())
        )
        await session.commit()
    with pytest.raises(ShortCredentialError) as exc:
        await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert exc.value.code == "PARENT_TOKEN_DISABLED"


async def test_short_credential_inherits_scope_snapshot(env) -> None:
    services, client = env
    async with services.session_factory() as session:
        from app.db.models import DataSource

        datasource = DataSource(
            name="ds",
            host="127.0.0.1",
            port=3306,
            username="u",
            password_cipher="cipher",
            default_database="demo",
            is_read_only=1,
            enabled=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(datasource)
        await session.commit()
        datasource_id = datasource.id
    scope = TokenScope(datasources=frozenset({datasource_id}))
    token, parent = await _exchange_token(services, scope=scope)
    credential = (await _do_exchange(client, token)).json()["credential"]
    principal = await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert principal is not None
    assert principal.scope.allows_datasource(datasource_id)
    assert principal.token_kind == "short"
    assert principal.bound_actor == Actor(provider="dingtalk", actor_id="manager1234", name="李四")
    assert principal.user_name == "bot"

    # Widening the parent afterwards must not widen an already-issued credential.
    await services.tokens.update_scope(parent.id, TokenScope())
    again = await services.short_credentials.authenticate(credential, "127.0.0.1")
    assert again is not None
    assert again.scope.restricted
    assert again.scope.allows_datasource(datasource_id)


async def test_cleanup_removes_expired(env) -> None:
    services, client = env
    token, _ = await _exchange_token(services)
    await _do_exchange(client, token)
    async with services.session_factory() as session:
        await session.execute(
            update(ShortCredential).values(expires_at=utcnow() - timedelta(days=1))
        )
        await session.commit()
    removed = await services.short_credentials.cleanup_expired()
    assert removed == 1
    async with services.session_factory() as session:
        assert await session.scalar(select(ShortCredential)) is None


async def test_exchange_token_cannot_issue_export_credentials(env) -> None:
    services, _ = env
    _, row = await _exchange_token(services)
    with pytest.raises(ValidationError) as exc:
        await services.credentials.issue(
            token_id=row.id,
            datasource_id=1,
            database="demo",
            ttl_hours=1,
            admin_username="admin",
            client_ip="127.0.0.1",
        )
    assert "不支持签发导出凭证" in str(exc.value)


async def test_sqlite_upgrade_adds_kind_and_short_credential(tmp_path) -> None:
    """An existing SQLite store gains the new column and table in place, and replays cleanly."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.schema import ensure_schema

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE api_token (id INTEGER PRIMARY KEY, user_name VARCHAR(64), "
                "token_hash CHAR(64), token_prefix VARCHAR(16), enabled INTEGER, "
                "created_at DATETIME, disabled_at DATETIME)"
            )
        )
    await ensure_schema(engine)
    await ensure_schema(engine)
    async with engine.connect() as conn:
        columns = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(api_token)"))).all()
        }
        tables = {
            row[0]
            for row in (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    await engine.dispose()
    assert "token_kind" in columns
    assert "expires_at" in columns
    assert "last_used_at" in columns
    assert "short_credential" in tables