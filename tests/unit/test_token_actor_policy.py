"""Token actor_policy: creation default, validation, update, and authentication carry-through."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.errors import NotFound, ValidationError
from app.security.principal import Principal
from app.tokens.service import TokenService


@pytest.fixture
async def tokens():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TokenService(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def test_create_defaults_to_ignore(tokens) -> None:
    plaintext, row = await tokens.create("alice")
    assert row.actor_policy == "ignore"
    principal = await tokens.authenticate(plaintext)
    assert principal is not None
    assert principal.actor_policy == "ignore"


async def test_create_require_and_authenticate_carries_policy(tokens) -> None:
    plaintext, row = await tokens.create("bot", actor_policy="require")
    assert row.actor_policy == "require"
    principal = await tokens.authenticate(plaintext)
    assert principal is not None
    assert principal.actor_policy == "require"


async def test_create_rejects_unknown_policy(tokens) -> None:
    with pytest.raises(ValidationError):
        await tokens.create("alice", actor_policy="optional")


async def test_update_actor_policy_roundtrip(tokens) -> None:
    plaintext, row = await tokens.create("alice")
    await tokens.update_actor_policy(row.id, "require")
    principal = await tokens.authenticate(plaintext)
    assert principal.actor_policy == "require"
    await tokens.update_actor_policy(row.id, "ignore")
    principal = await tokens.authenticate(plaintext)
    assert principal.actor_policy == "ignore"


async def test_update_actor_policy_rejects_unknown_value(tokens) -> None:
    _, row = await tokens.create("alice")
    with pytest.raises(ValidationError):
        await tokens.update_actor_policy(row.id, "mandatory")


async def test_update_actor_policy_missing_token(tokens) -> None:
    with pytest.raises(NotFound):
        await tokens.update_actor_policy(999, "require")


def test_principal_defaults_to_ignore() -> None:
    principal = Principal(user_name="u", token_id=1, token_prefix="gw_x")
    assert principal.actor_policy == "ignore"