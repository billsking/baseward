"""End-to-end source-IP allowlist: settings save takes effect immediately, DB beats env vars, invalid input is not persisted."""

from __future__ import annotations

import asyncio

import pytest

from app.netallow import EXPORT_ALLOW_KEY, MCP_ALLOW_KEY
from tests.integration.conftest import (
    admin_login,
    connect_export,
    export_server,
    export_settings,
    issue_credential,
    management_scalar,
    wait_deny_reason,
)

pytestmark = pytest.mark.integration

MCP_URL = "/mcp/"


async def _stored(env, key: str) -> str | None:
    return await management_scalar(
        env, "SELECT setting_value FROM system_setting WHERE setting_key = :key", {"key": key}
    )


async def _save(env, csrf: str, export: str, mcp: str):
    return await env.client.post(
        "/admin/settings/allowlist",
        data={"_csrf": csrf, EXPORT_ALLOW_KEY: export, MCP_ALLOW_KEY: mcp},
    )


async def test_settings_page_lists_both_channels(env):
    await admin_login(env)
    page = await env.client.get("/admin/settings")
    assert page.status_code == 200
    assert "来源 IP 白名单" in page.text
    assert f'name="{EXPORT_ALLOW_KEY}"' in page.text
    assert f'name="{MCP_ALLOW_KEY}"' in page.text
    # Before anything is saved, the page must state that the env-var copy is in force
    assert "GATEWAY_MYSQL_EXPORT_ALLOW_CIDRS" in page.text


async def test_saving_takes_effect_without_restart(env):
    csrf = await admin_login(env)
    response = await _save(env, csrf, "192.0.2.0/24\n192.168.1.20", "")
    assert response.status_code == 302
    assert await _stored(env, EXPORT_ALLOW_KEY) == "192.0.2.0/24,192.168.1.20/32"
    allow = env.services.export_allowlist
    assert await allow.is_allowed("192.0.2.5") is True
    assert await allow.is_allowed("198.51.100.5") is False


async def test_blank_save_locks_the_channel(env):
    """Saving both entrypoints empty means deny all and no longer falls back to env vars."""
    csrf = await admin_login(env)
    assert (await _save(env, csrf, "", "")).status_code == 302
    assert await env.services.export_allowlist.is_allowed("127.0.0.1") is False
    assert await env.services.mcp_allowlist.is_allowed("127.0.0.1") is False


async def test_illegal_entry_is_rejected_for_both_channels(env):
    """One invalid entrypoint aborts the whole batch, so nothing is half-updated."""
    csrf = await admin_login(env)
    response = await _save(env, csrf, "192.0.2.5/24", "203.0.113.9")
    assert response.status_code == 400
    assert "含主机位" in response.text
    assert await _stored(env, EXPORT_ALLOW_KEY) is None
    assert await _stored(env, MCP_ALLOW_KEY) is None


async def test_csrf_is_required(env):
    await admin_login(env)
    response = await env.client.post(
        "/admin/settings/allowlist", data={EXPORT_ALLOW_KEY: "192.0.2.0/24"}
    )
    assert response.status_code == 400


async def test_database_value_overrides_env_on_export_channel(env):
    """Env vars allow localhost, but after the settings page changes it to another address, connections must be rejected."""
    csrf = await admin_login(env)
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_allow_cidrs="127.0.0.1/32")

    async with export_server(env, settings) as (port, _):
        connection = await asyncio.to_thread(connect_export, port, account, password)
        await asyncio.to_thread(connection.close)

    assert (await _save(env, csrf, "10.255.255.1", "")).status_code == 302
    async with export_server(env, settings) as (port, _):
        with pytest.raises(Exception):
            await asyncio.to_thread(connect_export, port, account, password)
    await wait_deny_reason(env, "IP_NOT_ALLOWED")


async def test_mcp_channel_is_open_until_a_list_is_saved(env):
    """The allowlist runs before token validation, so an invalid token isolates whether the IP was the blocker."""
    headers = {"Authorization": "Bearer not-a-real-token"}
    csrf = await admin_login(env)

    open_response = await env.client.post(MCP_URL, headers=headers, json={})
    assert open_response.status_code == 401

    assert (await _save(env, csrf, "", "10.255.255.1")).status_code == 302
    denied = await env.client.post(MCP_URL, headers=headers, json={})
    assert denied.status_code == 403
    assert denied.json()["error"] == "ip_not_allowed"
    assert "www-authenticate" not in denied.headers
    await wait_deny_reason(env, "IP_NOT_ALLOWED", event_type="auth_failure")

    assert (await _save(env, csrf, "", "127.0.0.1")).status_code == 302
    allowed = await env.client.post(MCP_URL, headers=headers, json={})
    assert allowed.status_code == 401


async def test_change_is_audited_with_before_and_after(env):
    csrf = await admin_login(env)
    assert (await _save(env, csrf, "192.0.2.0/24", "")).status_code == 302
    detail = await management_scalar(
        env,
        "SELECT deny_reason FROM audit_log WHERE event_type = 'setting_change' "
        "AND tool = :tool",
        {"tool": EXPORT_ALLOW_KEY},
    )
    assert detail is not None
    assert "192.0.2.0/24" in detail
    assert "未保存过" in detail
    actor = await management_scalar(
        env,
        "SELECT user_name FROM audit_log WHERE event_type = 'setting_change' AND tool = :tool",
        {"tool": EXPORT_ALLOW_KEY},
    )
    assert actor == env.admin_user


async def test_saving_identical_value_writes_no_audit(env):
    csrf = await admin_login(env)
    assert (await _save(env, csrf, "192.0.2.0/24", "")).status_code == 302
    first = await management_scalar(
        env, "SELECT COUNT(*) FROM audit_log WHERE event_type = 'setting_change'"
    )
    assert (await _save(env, csrf, "192.0.2.0/24", "")).status_code == 302
    second = await management_scalar(
        env, "SELECT COUNT(*) FROM audit_log WHERE event_type = 'setting_change'"
    )
    assert second == first


async def test_healthz_reports_the_live_allowlist_source(env):
    csrf = await admin_login(env)
    before = (await env.client.get("/healthz")).json()["mysql_export"]
    assert before["allowlist_source"] in ("env", "unset")
    assert before["allowlist_entries"] == 0

    assert (await _save(env, csrf, "192.0.2.0/24\n198.51.100.0/24", "")).status_code == 302
    after = (await env.client.get("/healthz")).json()["mysql_export"]
    assert after["allowlist_source"] == "db"
    assert after["allowlist_entries"] == 2
