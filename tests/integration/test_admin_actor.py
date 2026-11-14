"""Admin UI for actor policy management and audit dual-identity display/filtering."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest

from app.security.actor import Actor, current_actor
from tests.integration.conftest import admin_login, management_scalar

pytestmark = pytest.mark.integration


async def test_create_require_token_via_form(env) -> None:
    csrf = await admin_login(env)
    created = await env.client.post(
        "/admin/tokens/new",
        data={"_csrf": csrf, "user_name": "ui-agent", "actor_policy": "require"},
    )
    assert created.status_code == 200
    match = re.search(r'<code id="new-token">([^<]+)</code>', created.text)
    assert match is not None, created.text
    principal = await env.services.tokens.authenticate(match.group(1))
    assert principal is not None
    assert principal.actor_policy == "require"

    page = await env.client.get("/admin/tokens")
    assert "ui-agent" in page.text
    assert "代理（强制身份头）" in page.text


async def test_policy_update_via_form_and_audit(env) -> None:
    csrf = await admin_login(env)
    plaintext, row = await env.services.tokens.create("ui-switch")

    edit = await env.client.get(f"/admin/tokens/{row.id}/edit")
    assert edit.status_code == 200
    assert f"/admin/tokens/{row.id}/policy" in edit.text

    updated = await env.client.post(
        f"/admin/tokens/{row.id}/policy",
        data={"_csrf": csrf, "actor_policy": "require"},
    )
    assert updated.status_code == 302
    principal = await env.services.tokens.authenticate(plaintext)
    assert principal.actor_policy == "require"

    count = await management_scalar(
        env,
        "SELECT COUNT(*) FROM audit_log WHERE event_type='setting_change' "
        "AND tool='token_actor_policy' AND deny_reason LIKE :pattern",
        {"pattern": "%ignore ⇒%require%"},
    )
    assert count == 1

    invalid = await env.client.post(
        f"/admin/tokens/{row.id}/policy",
        data={"_csrf": csrf, "actor_policy": "bogus"},
    )
    assert invalid.status_code == 400
    principal = await env.services.tokens.authenticate(plaintext)
    assert principal.actor_policy == "require"


async def test_audit_actor_filter_and_detail(env) -> None:
    await admin_login(env)
    principal = await env.services.tokens.authenticate(env.token)
    request_id = str(uuid4())
    token = current_actor.set(Actor(provider="dingtalk", actor_id="lisi001", name="李四"))
    try:
        await env.services.audit.start(
            request_id, "mcp_tool", principal, None, "execute_sql", "SELECT 1", "127.0.0.1"
        )
    finally:
        current_actor.reset(token)

    filtered = await env.client.get("/admin/audit", params={"actor": "lisi"})
    assert filtered.status_code == 200
    assert "李四" in filtered.text
    assert request_id in filtered.text

    empty = await env.client.get("/admin/audit", params={"actor": "no-such-actor"})
    assert empty.status_code == 200
    assert "李四" not in empty.text

    by_keyword = await env.client.get("/admin/audit", params={"q": "李四"})
    assert by_keyword.status_code == 200
    assert request_id in by_keyword.text

    detail = await env.client.get(f"/admin/audit/{request_id}")
    assert detail.status_code == 200
    assert "dingtalk:lisi001" in detail.text
    assert "自报" in detail.text