"""Token lifecycle."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ApiToken
from app.errors import NotFound, ValidationError
from app.security.crypto import hash_token, token_prefix

pytestmark = pytest.mark.integration


async def test_create_authenticate_disable_delete(env):
    with pytest.raises(ValidationError):
        await env.services.tokens.create("tester")

    plaintext, record = await env.services.tokens.create("alice")
    assert plaintext.startswith("gw_")
    assert record.token_prefix == token_prefix(plaintext) == plaintext[:10]
    assert record.token_hash == hash_token(plaintext)
    assert await env.services.tokens.enabled_count() == 2

    principal = await env.services.tokens.authenticate(plaintext)
    assert principal is not None
    assert principal.user_name == "alice"
    assert principal.token_id == record.id
    assert principal.token_prefix == record.token_prefix

    assert await env.services.tokens.authenticate("gw_short") is None
    assert await env.services.tokens.authenticate("not-a-token-but-long-enough") is None
    assert await env.services.tokens.authenticate("gw_" + "x" * 40) is None

    await env.services.tokens.disable(record.id)
    assert await env.services.tokens.authenticate(plaintext) is None
    assert await env.services.tokens.enabled_count() == 1
    async with env.services.session_factory() as session:
        row = await session.scalar(select(ApiToken).where(ApiToken.id == record.id))
    assert row is not None
    assert row.enabled == 0
    assert row.disabled_at is not None

    # After disabling, a new token can be created for the same user.
    new_plaintext, new_record = await env.services.tokens.create("alice")
    assert new_record.id != record.id
    assert await env.services.tokens.authenticate(new_plaintext) is not None

    await env.services.tokens.delete(record.id)
    with pytest.raises(NotFound):
        await env.services.tokens.delete(record.id)


async def test_user_name_validation(env):
    with pytest.raises(ValidationError):
        await env.services.tokens.create("")
    with pytest.raises(ValidationError):
        await env.services.tokens.create("u" * 65)
