"""Blocking and write-transaction rollback when auditing fails (real management and business databases)."""

from __future__ import annotations

import pytest

from app.errors import AuditUnavailableError
from tests.integration.conftest import business_engine, call_tool, mcp_session

pytestmark = pytest.mark.integration


async def _note_of(env, user_id: int = 1) -> str | None:
    engine = business_engine(env)
    try:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql(
                "SELECT note FROM users WHERE id = %s", (user_id,)
            )
            return result.scalar()
    finally:
        await engine.dispose()


async def test_audit_start_failure_blocks_before_pool(env, monkeypatch):
    async def forbidden_get(datasource):
        raise AssertionError("审计 start 失败后不得获取业务库连接")

    async def failing_start(*args, **kwargs):
        raise AuditUnavailableError("审计库不可用")

    monkeypatch.setattr(env.services.audit, "start", failing_start)
    monkeypatch.setattr(env.services.pool, "get", forbidden_get)

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "execute_sql",
            {"datasource": env.ds_name, "sql": "SELECT 1"},
        )
    assert failed is True
    assert payload["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert "审计" in payload["error"]["message"]


async def test_audit_start_failure_does_not_execute_write(env, monkeypatch):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.set_read_only(ds.id, False)

    async def failing_start(*args, **kwargs):
        raise AuditUnavailableError("审计库不可用")

    monkeypatch.setattr(env.services.audit, "start", failing_start)

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "execute_sql",
            {
                "datasource": env.ds_name,
                "sql": "UPDATE users SET note = 'should-not-apply' WHERE id = 1",
            },
        )
    assert failed is True
    assert payload["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert await _note_of(env) == "正常客户"


async def test_pool_session_has_autocommit_off(env):
    """engine.begin() is the only rollback path, so the upstream session must not autocommit."""
    ds = await env.services.datasources.get_by_name(env.ds_name)
    engine = await env.services.pool.get(ds)
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql("SELECT @@autocommit")
        assert int(result.scalar()) == 0


async def test_success_update_failure_rolls_back_write(env, monkeypatch):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    await env.services.datasources.set_read_only(ds.id, False)

    async def failing_mark_success(*args, **kwargs):
        raise AuditUnavailableError("审计结果更新失败")

    monkeypatch.setattr(env.services.audit, "mark_success", failing_mark_success)

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "execute_sql",
            {
                "datasource": env.ds_name,
                "sql": "UPDATE users SET note = 'rolled-back' WHERE id = 1",
            },
        )
    assert failed is True
    error = payload["error"]
    assert error["code"] == "AUDIT_UNAVAILABLE"
    assert error["request_id"]
    assert await _note_of(env) == "正常客户"

    audit_row = await env.services.audit.get(error["request_id"])
    assert audit_row is not None
    assert audit_row.status == "error"
    assert audit_row.deny_reason == "AUDIT_UNAVAILABLE"


async def test_success_update_failure_blocks_read_result(env, monkeypatch):
    async def failing_mark_success(*args, **kwargs):
        raise AuditUnavailableError("审计结果更新失败")

    monkeypatch.setattr(env.services.audit, "mark_success", failing_mark_success)

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session,
            "execute_sql",
            {"datasource": env.ds_name, "sql": "SELECT 1 AS one"},
        )
    assert failed is True
    assert payload["error"]["code"] == "AUDIT_UNAVAILABLE"

    row = await env.services.audit.get(payload["error"]["request_id"])
    assert row is not None
    assert row.status == "error"
