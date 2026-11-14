"""Audit actor pass-through (intent/denied/credential events) and actor filtering."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.audit.service import AuditService
from app.db.models import Base
from app.security.actor import Actor, current_actor
from app.security.principal import Principal

PRINCIPAL = Principal(user_name="zhangsan", token_id=1, token_prefix="gw_test")
ACTOR = Actor(provider="dingtalk", actor_id="lisi001", name="李四")


@pytest.fixture
async def audit():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield AuditService(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


@pytest.fixture
def actor_context():
    token = current_actor.set(ACTOR)
    yield
    current_actor.reset(token)


async def _write_intent(audit, actor) -> str:
    token = current_actor.set(actor)
    try:
        request_id = str(uuid4())
        await audit.start(
            request_id, "mcp_tool", PRINCIPAL, None, "execute_sql", "SELECT 1", "127.0.0.1"
        )
        return request_id
    finally:
        current_actor.reset(token)


def _assert_actor(row) -> None:
    assert row.actor_provider == "dingtalk"
    assert row.actor_id == "lisi001"
    assert row.actor_name == "李四"
    assert row.actor_trust == "asserted"


async def test_start_carries_actor(audit, actor_context) -> None:
    request_id = str(uuid4())
    await audit.start(
        request_id, "mcp_tool", PRINCIPAL, None, "execute_sql", "SELECT 1", "127.0.0.1"
    )
    row = await audit.get(request_id)
    assert row.user_name == "zhangsan"
    _assert_actor(row)


async def test_denied_carries_actor(audit, actor_context) -> None:
    request_id = str(uuid4())
    await audit.record_denied(
        request_id, "mcp_tool", PRINCIPAL, None, "execute_sql", None, "127.0.0.1",
        "DATASOURCE_DENIED", "denied",
    )
    row = await audit.get(request_id)
    assert row.status == "denied"
    _assert_actor(row)


async def test_credential_event_carries_actor(audit, actor_context) -> None:
    await audit.record_credential_event(
        account_name="exp_test",
        user_name="zhangsan",
        datasource=None,
        action="issue_auto",
        admin_username="mcp:gw_test",
        client_ip="127.0.0.1",
    )
    rows, total = await audit.query({"q": "exp_test"})
    assert total == 1
    _assert_actor(rows[0])


async def test_without_actor_columns_are_null(audit) -> None:
    request_id = str(uuid4())
    await audit.start(
        request_id, "mcp_tool", PRINCIPAL, None, "execute_sql", "SELECT 1", "127.0.0.1"
    )
    row = await audit.get(request_id)
    assert row.actor_provider is None
    assert row.actor_id is None
    assert row.actor_name is None
    assert row.actor_trust is None


async def test_mark_success_preserves_actor(audit, actor_context) -> None:
    request_id = str(uuid4())
    await audit.start(
        request_id, "mcp_tool", PRINCIPAL, None, "execute_sql", "SELECT 1", "127.0.0.1"
    )
    await audit.mark_success(request_id, 12, 1)
    row = await audit.get(request_id)
    assert row.status == "success"
    _assert_actor(row)


async def test_query_actor_filter_matches_id_and_name(audit) -> None:
    await _write_intent(audit, ACTOR)
    await _write_intent(audit, Actor(provider="dingtalk", actor_id="wangwu002", name="王五"))
    await _write_intent(audit, None)

    rows, total = await audit.query({"actor": "lisi"})
    assert total == 1
    assert rows[0].actor_id == "lisi001"
    _, total = await audit.query({"actor": "王"})
    assert total == 1
    _, total = await audit.query({"actor": "wangwu002"})
    assert total == 1


async def test_query_actor_escapes_wildcards(audit) -> None:
    await _write_intent(audit, Actor(provider="dingtalk", actor_id="a%b", name="x"))
    await _write_intent(audit, Actor(provider="dingtalk", actor_id="axb", name="y"))
    rows, total = await audit.query({"actor": "a%b"})
    assert total == 1
    assert rows[0].actor_id == "a%b"


async def test_q_covers_actor_columns(audit) -> None:
    await _write_intent(audit, ACTOR)
    _, total = await audit.query({"q": "李四"})
    assert total == 1
    _, total = await audit.query({"q": "lisi001"})
    assert total == 1