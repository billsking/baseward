"""MCP actor enforcement end to end: require/ignore policies, 403 denials, audit dual identity."""

from __future__ import annotations

import base64

import httpx
import pytest

from tests.integration.conftest import call_tool, mcp_session

pytestmark = pytest.mark.integration


def _actor_headers(actor_id: str = "lisi001", name: str | None = "李四") -> dict:
    headers = {"X-BaseWard-Actor-Provider": "dingtalk", "X-BaseWard-Actor-Id": actor_id}
    if name is not None:
        headers["X-BaseWard-Actor-Name"] = (
            base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
        )
    return headers


async def _raw_initialize(env, token: str, extra_headers: dict | None = None):
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(extra_headers or {}),
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "actor-test", "version": "0"},
                },
            },
        )


async def test_require_token_all_tools_record_actor(env) -> None:
    token, _ = await env.services.tokens.create("bot", actor_policy="require")
    async with mcp_session(env, token=token, extra_headers=_actor_headers()) as session:
        for name, arguments in (
            ("list_datasources", {}),
            ("describe_datasource", {"datasource": env.ds_name}),
            ("search_objects", {"datasource": env.ds_name, "pattern": "user"}),
            ("execute_sql", {"datasource": env.ds_name, "sql": "SELECT id FROM users LIMIT 1"}),
        ):
            failed, payload = await call_tool(session, name, arguments)
            assert failed is False, (name, payload)
        # Export issuance is unavailable or banned for require tokens; either way the
        # middleware admitted the call and the tool layer produced the denial.
        failed, payload = await call_tool(
            session,
            "issue_export_credential",
            {"datasource": env.ds_name, "sql": "SELECT id FROM users", "reason": "actor test"},
        )
        assert failed is True
        assert payload["error"]["code"] in {"EXPORT_UNAVAILABLE", "CREDENTIAL_ISSUE_DENIED"}

    rows, total = await env.services.audit.query({"actor": "lisi001"})
    assert total >= 5
    for row in rows:
        assert row.actor_provider == "dingtalk"
        assert row.actor_id == "lisi001"
        assert row.actor_name == "李四"
        assert row.actor_trust == "asserted"
        assert row.user_name == "bot"
    tools = {row.tool for row in rows}
    assert {
        "list_datasources",
        "describe_datasource",
        "search_objects",
        "execute_sql",
        "issue_export_credential",
    } <= tools


async def test_require_token_missing_headers_denied(env) -> None:
    token, _ = await env.services.tokens.create("bot", actor_policy="require")
    response = await _raw_initialize(env, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_required"

    rows, total = await env.services.audit.query({"user_name": "bot", "status": "denied"})
    assert total == 1
    assert rows[0].deny_reason == "ACTOR_REQUIRED"
    assert rows[0].event_type == "auth_failure"
    assert rows[0].actor_id is None


@pytest.mark.parametrize(
    "extra_headers",
    [
        {"X-BaseWard-Actor-Provider": "dingtalk"},
        {"X-BaseWard-Actor-Provider": "DingTalk", "X-BaseWard-Actor-Id": "u1"},
        {
            "X-BaseWard-Actor-Provider": "dingtalk",
            "X-BaseWard-Actor-Id": "u1",
            "X-BaseWard-Actor-Name": "!!!bad-base64!!!",
        },
    ],
)
async def test_require_token_invalid_headers_denied(env, extra_headers) -> None:
    token, _ = await env.services.tokens.create("bot", actor_policy="require")
    response = await _raw_initialize(env, token, extra_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_invalid"

    rows, total = await env.services.audit.query({"user_name": "bot", "status": "denied"})
    assert total == 1
    assert rows[0].deny_reason == "ACTOR_INVALID"


async def test_ignore_token_headers_are_ignored(env) -> None:
    async with mcp_session(env, extra_headers=_actor_headers()) as session:
        failed, payload = await call_tool(
            session, "execute_sql", {"datasource": env.ds_name, "sql": "SELECT id FROM users LIMIT 1"}
        )
        assert failed is False, payload

    rows, total = await env.services.audit.query({"user_name": "tester", "tool": "execute_sql"})
    assert total >= 1
    for row in rows:
        assert row.actor_provider is None
        assert row.actor_id is None
        assert row.actor_name is None
        assert row.actor_trust is None


async def test_policy_switch_enforces_immediately(env) -> None:
    token, row = await env.services.tokens.create("switcher")
    async with mcp_session(env, token=token) as session:
        failed, payload = await call_tool(session, "list_datasources", {})
        assert failed is False, payload

    await env.services.tokens.update_actor_policy(row.id, "require")

    response = await _raw_initialize(env, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "actor_required"

    async with mcp_session(env, token=token, extra_headers=_actor_headers()) as session:
        failed, payload = await call_tool(session, "list_datasources", {})
        assert failed is False, payload