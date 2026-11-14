"""Audit write, query, retention, and cleanup."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.clock import utcnow
from app.db.models import AuditLog
from app.errors import AuditUnavailableError, ValidationError
from app.security.principal import Principal

pytestmark = pytest.mark.integration


async def _ds(env):
    ds = await env.services.datasources.get_by_name(env.ds_name)
    assert ds is not None
    return ds


async def test_start_success_and_query(env):
    ds = await _ds(env)
    principal = Principal("tester", 1, env.token[:10])
    request_id = str(uuid4())
    await env.services.audit.start(
        request_id, "mcp_tool", principal, ds, "execute_sql", "SELECT 1", "10.0.0.1"
    )
    pending = await env.services.audit.get(request_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.finished_at is None

    await env.services.audit.mark_success(request_id, 12, 1)
    row = await env.services.audit.get(request_id)
    assert row.status == "success"
    assert row.duration_ms == 12
    assert row.row_count == 1
    assert row.finished_at is not None
    assert row.datasource_name == env.ds_name
    assert row.token_prefix == env.token[:10]

    filters = {
        "from": utcnow() - timedelta(hours=1),
        "to": utcnow() + timedelta(hours=1),
        "user_name": "tester",
        "datasource_name": env.ds_name,
        "status": "success",
        "q": "SELECT 1",
    }
    rows, total = await env.services.audit.query(filters, page=1, page_size=50)
    assert total == 1
    assert rows[0].request_id == request_id

    rows, total = await env.services.audit.query({"q": "不存在的关键词"}, page=1)
    assert total == 0
    assert rows == []


async def test_error_from_pending_and_success(env):
    ds = await _ds(env)
    principal = Principal("tester", 1, env.token[:10])

    pending_id = str(uuid4())
    await env.services.audit.start(
        pending_id, "mcp_tool", principal, ds, "execute_sql", "SELECT 2", "10.0.0.2"
    )
    await env.services.audit.mark_error(pending_id, "DB_ERROR", "boom", 5)
    row = await env.services.audit.get(pending_id)
    assert row.status == "error"
    assert row.deny_reason == "DB_ERROR"
    assert row.error_message == "boom"

    success_id = str(uuid4())
    await env.services.audit.start(
        success_id, "mcp_tool", principal, ds, "execute_sql", "SELECT 3", "10.0.0.2"
    )
    await env.services.audit.mark_success(success_id, 1, 1)
    await env.services.audit.mark_error(success_id, "INTERNAL_ERROR", "late", 2)
    row = await env.services.audit.get(success_id)
    assert row.status == "error"


async def test_missing_request_id_raises(env):
    with pytest.raises(AuditUnavailableError):
        await env.services.audit.mark_success(str(uuid4()), 1, 1)
    with pytest.raises(AuditUnavailableError):
        await env.services.audit.mark_error(str(uuid4()), "DB_ERROR", "x", 1)


async def test_denied_and_auth_failure(env):
    ds = await _ds(env)
    principal = Principal("tester", 1, env.token[:10])
    denied_id = str(uuid4())
    await env.services.audit.record_denied(
        denied_id,
        "mcp_tool",
        principal,
        ds,
        "execute_sql",
        "DROP TABLE users",
        "10.0.0.3",
        "DDL_BLOCKED",
        "DDL 被禁止",
    )
    row = await env.services.audit.get(denied_id)
    assert row.status == "denied"
    assert row.deny_reason == "DDL_BLOCKED"
    assert row.finished_at is not None

    auth_id = str(uuid4())
    await env.services.audit.record_auth_failure(auth_id, "gw_deadbeef", "10.0.0.4")
    row = await env.services.audit.get(auth_id)
    assert row.event_type == "auth_failure"
    assert row.user_name == "unknown"
    assert row.status == "denied"
    assert row.deny_reason == "AUTH_INVALID"


async def test_retention_setting_and_cleanup(env):
    assert await env.services.audit.get_retention_days() == 180
    await env.services.audit.set_retention_days(30)
    assert await env.services.audit.get_retention_days() == 30
    with pytest.raises(ValidationError):
        await env.services.audit.set_retention_days(0)
    with pytest.raises(ValidationError):
        await env.services.audit.set_retention_days(3651)

    now = utcnow()
    old_id = str(uuid4())
    fresh_id = str(uuid4())
    async with env.services.session_factory() as session:
        session.add(
            AuditLog(
                request_id=old_id,
                event_type="mcp_tool",
                user_name="tester",
                status="success",
                sql_text="SELECT old",
                created_at=now - timedelta(days=40),
                finished_at=now - timedelta(days=40),
            )
        )
        session.add(
            AuditLog(
                request_id=fresh_id,
                event_type="mcp_tool",
                user_name="tester",
                status="success",
                sql_text="SELECT fresh",
                created_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1),
            )
        )
        await session.commit()

    deleted = await env.services.audit.cleanup_once(now_utc=now)
    assert deleted == 1
    assert await env.services.audit.get(old_id) is None
    assert await env.services.audit.get(fresh_id) is not None


async def test_stats_and_recent(env):
    total, denied = await env.services.audit.today_stats()
    assert total >= 0
    assert denied >= 0
    recent = await env.services.audit.recent(10)
    assert len(recent) <= 10


async def test_mark_interrupted_from_pending_and_success(env):
    ds = await _ds(env)
    principal = Principal("tester", 1, env.token[:10])

    for reason in ("CLIENT_DISCONNECTED", "ROW_LIMIT_EXCEEDED", "TIME_LIMIT_EXCEEDED"):
        request_id = str(uuid4())
        await env.services.audit.start(
            request_id,
            "mysql_export",
            principal,
            ds,
            "mysql_query",
            "SELECT * FROM big",
            "10.0.0.5",
            credential_account="exp_deadbeef0001",
        )
        await env.services.audit.mark_interrupted(request_id, 1200, 8001, reason)
        row = await env.services.audit.get(request_id)
        assert row.status == "interrupted"
        assert row.deny_reason == reason
        assert row.row_count == 8001
        assert row.duration_ms == 1200
        assert row.finished_at is not None
        assert row.credential_account == "exp_deadbeef0001"

    success_id = str(uuid4())
    await env.services.audit.start(
        success_id, "mysql_export", principal, ds, "mysql_query", "SELECT 1", "10.0.0.5"
    )
    await env.services.audit.mark_success(success_id, 10, 1)
    await env.services.audit.mark_interrupted(success_id, 20, 1, "CLIENT_DISCONNECTED")
    row = await env.services.audit.get(success_id)
    assert row.status == "interrupted"

    rows, total = await env.services.audit.query(
        {"status": "interrupted", "datasource_name": env.ds_name}, page=1
    )
    assert total >= 4
    assert all(item.status == "interrupted" for item in rows)


async def test_credential_account_is_written_and_searchable(env):
    ds = await _ds(env)
    principal = Principal("tester", 1, env.token[:10])

    denied_id = str(uuid4())
    await env.services.audit.record_denied(
        denied_id,
        "mysql_export",
        principal,
        ds,
        "mysql_query",
        None,
        "10.0.0.6",
        "DATASOURCE_DENIED",
        "无权访问",
        credential_account="exp_search_me",
    )
    row = await env.services.audit.get(denied_id)
    assert row.credential_account == "exp_search_me"

    rows, total = await env.services.audit.query({"q": "exp_search_me"}, page=1)
    assert total == 1
    assert rows[0].request_id == denied_id


async def test_record_export_auth_failure(env):
    for code, account, user_name in (
        ("CREDENTIAL_NOT_FOUND", None, "unknown"),
        ("CREDENTIAL_EXPIRED", "exp_expired0001", "alice"),
        ("IP_NOT_ALLOWED", "exp_ipcheck0001", "alice"),
    ):
        request_id = str(uuid4())
        await env.services.audit.record_export_auth_failure(
            request_id, account, user_name, "10.0.0.7", code
        )
        row = await env.services.audit.get(request_id)
        assert row is not None
        assert row.event_type == "mysql_auth"
        assert row.status == "denied"
        assert row.deny_reason == code
        assert row.credential_account == account
        assert row.user_name == user_name


async def test_record_credential_event_fields(env):
    ds = await _ds(env)
    await env.services.audit.record_credential_event(
        account_name="exp_lifecycle001",
        user_name="alice",
        datasource=ds,
        action="issue",
        admin_username="admin",
        client_ip="10.0.0.8",
    )
    rows, total = await env.services.audit.query({"q": "exp_lifecycle001"}, page=1)
    assert total == 1
    row = rows[0]
    assert row.event_type == "credential_lifecycle"
    assert row.status == "success"
    assert row.tool == "issue"
    assert row.deny_reason == "admin"
    assert row.datasource_name == env.ds_name
    assert row.sql_text is None
