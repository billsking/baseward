"""Token expiration and fail-open last-used telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.db.models import ApiToken, Base
from app.errors import ValidationError
from app.tokens.service import TokenService


@pytest.fixture
async def tokens():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TokenService(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def test_expiry_and_last_used_are_persisted(tokens) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    plaintext, row = await tokens.create("alice", expires_at=expires_at)
    assert row.expires_at is not None and row.expires_at.tzinfo is None
    assert row.last_used_at is None

    principal = await tokens.authenticate(plaintext)
    assert principal is not None
    async with tokens._session_factory() as session:
        stored = await session.get(ApiToken, row.id)
        assert stored is not None and stored.last_used_at is not None
        first_used_at = stored.last_used_at

    assert await tokens.authenticate(plaintext) is not None
    async with tokens._session_factory() as session:
        stored = await session.get(ApiToken, row.id)
        assert stored.last_used_at == first_used_at


async def test_expired_token_is_rejected_and_can_be_replaced(tokens) -> None:
    plaintext, row = await tokens.create("alice", expires_at=utcnow() + timedelta(hours=1))
    async with tokens._session_factory() as session:
        stored = await session.get(ApiToken, row.id)
        stored.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    assert await tokens.authenticate(plaintext) is None
    assert await tokens.enabled_count() == 0
    _, replacement = await tokens.create("alice")
    assert replacement.enabled == 1
    assert await tokens.enabled_count() == 1


async def test_create_rejects_past_expiry(tokens) -> None:
    with pytest.raises(ValidationError, match="过期时间必须晚于当前时间"):
        await tokens.create("alice", expires_at=utcnow() - timedelta(seconds=1))


async def test_last_used_write_failure_does_not_block_authentication(tokens, monkeypatch) -> None:
    plaintext, _ = await tokens.create("alice")

    async def fail_commit(self) -> None:
        raise SQLAlchemyError("write failed")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    principal = await tokens.authenticate(plaintext)
    assert principal is not None and principal.user_name == "alice"
