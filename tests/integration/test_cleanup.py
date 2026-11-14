"""Retention and manual cleanup endpoints."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from app.audit import cleanup as cleanup_module
from app.clock import utcnow
from app.db.models import AuditLog
from tests.integration.conftest import admin_login

pytestmark = pytest.mark.integration


async def _insert_audit(env, created_at) -> str:
    request_id = str(uuid4())
    async with env.services.session_factory() as session:
        session.add(
            AuditLog(
                request_id=request_id,
                event_type="mcp_tool",
                user_name="tester",
                datasource_name=env.ds_name,
                status="success",
                sql_text="SELECT cleanup",
                created_at=created_at,
                finished_at=created_at,
            )
        )
        await session.commit()
    return request_id


async def test_manual_cleanup_deletes_only_expired(env):
    csrf = await admin_login(env)
    saved = await env.client.post(
        "/admin/settings/retention", data={"_csrf": csrf, "retention_days": "30"}
    )
    assert saved.status_code == 302
    assert await env.services.audit.get_retention_days() == 30

    now = utcnow()
    expired_id = await _insert_audit(env, now - timedelta(days=31))
    fresh_id = await _insert_audit(env, now - timedelta(days=29))
    today_id = await _insert_audit(env, now - timedelta(minutes=5))

    missing_csrf = await env.client.post("/admin/settings/cleanup")
    assert missing_csrf.status_code == 400
    assert await env.services.audit.get(expired_id) is not None

    cleaned = await env.client.post("/admin/settings/cleanup", data={"_csrf": csrf})
    assert cleaned.status_code == 302
    assert "msg=cleanup_done" in cleaned.headers["location"]
    assert "n=1" in cleaned.headers["location"]

    assert await env.services.audit.get(expired_id) is None
    assert await env.services.audit.get(fresh_id) is not None
    assert await env.services.audit.get(today_id) is not None


async def test_cleanup_uses_configured_retention(env):
    now = utcnow()
    old_id = await _insert_audit(env, now - timedelta(days=2))
    recent_id = await _insert_audit(env, now - timedelta(hours=1))

    await env.services.audit.set_retention_days(1)
    deleted = await env.services.audit.cleanup_once(now_utc=now)
    assert deleted == 1
    assert await env.services.audit.get(old_id) is None
    assert await env.services.audit.get(recent_id) is not None

    await env.services.audit.set_retention_days(180)
    assert await env.services.audit.cleanup_once(now_utc=now) == 0
    assert await env.services.audit.get(recent_id) is not None


async def test_cleanup_loop_runs_both_audit_and_credentials(env, monkeypatch):
    calls = {"audit": 0, "credentials": 0}
    stop = asyncio.Event()

    async def fake_audit_cleanup(*args, **kwargs):
        calls["audit"] += 1
        return 5

    async def fake_credential_cleanup(*args, **kwargs):
        calls["credentials"] += 1
        stop.set()
        return 3

    monkeypatch.setattr(env.services.audit, "cleanup_once", fake_audit_cleanup)
    monkeypatch.setattr(env.services.credentials, "cleanup_expired", fake_credential_cleanup)
    monkeypatch.setattr(cleanup_module, "seconds_until_next_run", lambda now=None: 0)

    await asyncio.wait_for(cleanup_module.cleanup_loop(env.services, stop), timeout=5)
    assert calls == {"audit": 1, "credentials": 1}


async def test_cleanup_loop_keeps_credentials_when_audit_fails(env, monkeypatch):
    calls = {"audit": 0, "credentials": 0}
    stop = asyncio.Event()

    async def failing_audit_cleanup(*args, **kwargs):
        calls["audit"] += 1
        raise RuntimeError("audit down")

    async def fake_credential_cleanup(*args, **kwargs):
        calls["credentials"] += 1
        stop.set()
        return 3

    monkeypatch.setattr(env.services.audit, "cleanup_once", failing_audit_cleanup)
    monkeypatch.setattr(env.services.credentials, "cleanup_expired", fake_credential_cleanup)
    monkeypatch.setattr(cleanup_module, "seconds_until_next_run", lambda now=None: 0)

    await asyncio.wait_for(cleanup_module.cleanup_loop(env.services, stop), timeout=5)
    assert calls == {"audit": 1, "credentials": 1}
