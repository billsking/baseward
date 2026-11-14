"""Require-policy tokens cannot issue export credentials; denials are audited."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.audit.service import AuditService
from app.config import Settings
from app.credentials.service import CREDENTIAL_ISSUE_DENIED, CredentialService
from app.db.models import Base
from app.errors import ValidationError
from app.tokens.service import TokenService


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        master_key=base64.b64encode(b"k" * 32).decode("ascii"),
        admin_user="admin",
        admin_password="admin-test-123",
    )
    tokens = TokenService(factory)
    audit = AuditService(factory)
    credentials = CredentialService(factory, settings, tokens, None, audit)
    yield SimpleNamespace(credentials=credentials, tokens=tokens, audit=audit)
    await engine.dispose()


async def test_require_token_admin_issue_denied(env) -> None:
    _, row = await env.tokens.create("agent", actor_policy="require")
    with pytest.raises(ValidationError) as exc:
        await env.credentials.issue(
            token_id=row.id,
            datasource_id=1,
            database="db",
            ttl_hours=1,
            admin_username="admin",
            client_ip="127.0.0.1",
        )
    assert "代理型" in str(exc.value)
    rows, total = await env.audit.query({"status": "denied"})
    assert total == 1
    assert rows[0].deny_reason == CREDENTIAL_ISSUE_DENIED
    assert rows[0].tool == "issue"
    assert rows[0].event_type == "credential_lifecycle"


async def test_require_token_auto_issue_denied(env) -> None:
    plaintext, row = await env.tokens.create("agent", actor_policy="require")
    principal = await env.tokens.authenticate(plaintext)
    with pytest.raises(ValidationError):
        await env.credentials.issue_auto(
            token_id=row.id,
            datasource_id=1,
            database="db",
            principal=principal,
            trigger_request_id="trigger",
            client_ip="127.0.0.1",
        )
    rows, total = await env.audit.query({"status": "denied"})
    assert total == 1
    assert rows[0].tool == "issue_auto"


async def test_ignore_token_passes_issue_token_check(env) -> None:
    _, row = await env.tokens.create("personal")
    token = await env.credentials._token_for_issue(row.id)
    assert token.id == row.id