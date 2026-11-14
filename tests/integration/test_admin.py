"""End-to-end flows for admin authentication, CSRF, and the four business modules."""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

import pytest

from app.clock import utcnow
from app.db.models import AuditLog
from tests.integration.conftest import admin_login, csrf_from

pytestmark = pytest.mark.integration

COOKIE_NAME = "gw_admin_session"


async def _csrf(env) -> str:
    response = await env.client.get("/admin")
    assert response.status_code == 200, response.text
    return csrf_from(response.text)


async def test_unauthenticated_and_login_flow(env):
    response = await env.client.get("/admin")
    assert response.status_code == 401
    assert "登录" in response.text
    assert "用户名或密码错误" not in response.text

    response = await env.client.get("/admin/audit")
    assert response.status_code == 401

    wrong = await env.client.post(
        "/admin/login",
        data={"username": env.admin_user, "password": "definitely-wrong"},
    )
    assert wrong.status_code == 401
    assert "用户名或密码错误" in wrong.text
    assert COOKIE_NAME not in env.client.cookies

    ok = await env.client.post(
        "/admin/login",
        data={"username": env.admin_user, "password": env.admin_password},
    )
    assert ok.status_code == 302
    assert ok.headers["location"] == "/admin"
    assert COOKIE_NAME in env.client.cookies

    dashboard = await env.client.get("/admin")
    assert dashboard.status_code == 200
    assert "仪表盘" in dashboard.text
    assert "启用 Token" in dashboard.text
    assert "暂无审计记录" in dashboard.text

    login_page = await env.client.get("/admin/login")
    assert login_page.status_code == 302
    assert login_page.headers["location"] == "/admin"


async def test_csrf_required_and_logout(env):
    csrf = await admin_login(env)

    missing = await env.client.post(
        "/admin/datasources/new",
        data={
            "name": "no-csrf",
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": env.db_password,
            "default_database": env.biz_db,
        },
    )
    assert missing.status_code == 400
    assert "CSRF" in missing.text

    bad = await env.client.post(
        "/admin/datasources/new",
        data={"_csrf": "forged", "name": "bad-csrf"},
    )
    assert bad.status_code == 400

    logout = await env.client.post("/admin/logout", data={"_csrf": csrf})
    assert logout.status_code == 302
    assert logout.headers["location"] == "/admin/login"

    after = await env.client.get("/admin")
    assert after.status_code == 401


async def test_datasource_crud_and_connection_test(env):
    csrf = await admin_login(env)
    ds = await env.services.datasources.get_by_name(env.ds_name)
    original_cipher = ds.password_cipher
    assert original_cipher != env.db_password

    created = await env.client.post(
        "/admin/datasources/new",
        data={
            "_csrf": csrf,
            "name": "ui-ds",
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": env.db_password,
            "default_database": env.biz_db,
            "remark": "创建于管理端",
        },
    )
    assert created.status_code == 302, created.text
    assert created.headers["location"].startswith("/admin/datasources?msg=datasource_created")
    created_ds = await env.services.datasources.get_by_name("ui-ds")
    assert created_ds is not None
    assert created_ds.password_cipher != env.db_password

    edit_page = await env.client.get(f"/admin/datasources/{created_ds.id}/edit")
    assert edit_page.status_code == 200
    assert "ui-ds" in edit_page.text

    updated = await env.client.post(
        f"/admin/datasources/{created_ds.id}/edit",
        data={
            "_csrf": csrf,
            "name": "ui-ds-renamed",
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": "",
            "default_database": env.biz_db,
            "remark": "改名且不重置密码",
        },
    )
    assert updated.status_code == 302, updated.text
    renamed = await env.services.datasources.get_by_name("ui-ds-renamed")
    assert renamed is not None
    assert renamed.password_cipher == created_ds.password_cipher

    duplicate = await env.client.post(
        "/admin/datasources/new",
        data={
            "_csrf": csrf,
            "name": env.ds_name,
            "host": env.db_host,
            "port": str(env.db_port),
            "username": env.db_user,
            "password": env.db_password,
            "default_database": env.biz_db,
        },
    )
    assert duplicate.status_code == 400
    assert "已存在" in duplicate.text

    bad_connection = await env.client.post(
        "/admin/datasources/new",
        data={
            "_csrf": csrf,
            "name": "ui-broken",
            "host": env.db_host,
            "port": "1",
            "username": env.db_user,
            "password": env.db_password,
            "default_database": env.biz_db,
        },
    )
    assert bad_connection.status_code == 400
    assert await env.services.datasources.get_by_name("ui-broken") is None

    tested = await env.client.post(
        f"/admin/datasources/{created_ds.id}/test", data={"_csrf": csrf}
    )
    assert tested.status_code == 302
    assert "msg=connection_ok" in tested.headers["location"]

    deleted = await env.client.post(
        f"/admin/datasources/{created_ds.id}/delete", data={"_csrf": csrf}
    )
    assert deleted.status_code == 302
    assert await env.services.datasources.get_by_name("ui-ds-renamed") is None


async def test_policy_and_masking_management(env):
    csrf = await admin_login(env)
    ds = await env.services.datasources.get_by_name(env.ds_name)

    policy_page = await env.client.get(f"/admin/datasources/{ds.id}/policy")
    assert policy_page.status_code == 200
    assert "脱敏" in policy_page.text

    saved = await env.client.post(
        f"/admin/datasources/{ds.id}/policy",
        data={"_csrf": csrf, "is_read_only": "0"},
    )
    assert saved.status_code == 302
    assert (await env.services.datasources.get(ds.id)).is_read_only == 0

    await env.services.datasources.set_read_only(ds.id, True)
    assert (await env.services.datasources.get(ds.id)).is_read_only == 1

    added = await env.client.post(
        f"/admin/datasources/{ds.id}/masking/new",
        data={
            "_csrf": csrf,
            "database": env.biz_db,
            "table": "users",
            "column": "note",
            "strategy": "mask",
            "pattern": "generic",
        },
    )
    assert added.status_code == 302, added.text
    rules = await env.services.masking.list_rules(ds.id)
    assert [(rule.table, rule.column, rule.strategy) for rule in rules] == [
        ("users", "note", "mask")
    ]

    invalid = await env.client.post(
        f"/admin/datasources/{ds.id}/masking/new",
        data={
            "_csrf": csrf,
            "database": env.biz_db,
            "table": "users",
            "column": "note",
            "strategy": "hash",
            "pattern": "phone",
        },
    )
    assert invalid.status_code == 400
    assert "不允许指定脱敏模式" in invalid.text

    removed = await env.client.post(
        f"/admin/datasources/{ds.id}/masking/{rules[0].id}/delete", data={"_csrf": csrf}
    )
    assert removed.status_code == 302
    assert await env.services.masking.list_rules(ds.id) == []

    missing = await env.client.post(
        "/admin/datasources/999999/delete", data={"_csrf": csrf}
    )
    assert missing.status_code == 404


async def test_token_management(env):
    csrf = await admin_login(env)

    page = await env.client.get("/admin/tokens")
    assert page.status_code == 200
    assert "tester" in page.text

    created = await env.client.post(
        "/admin/tokens/new", data={"_csrf": csrf, "user_name": "ui-user"}
    )
    assert created.status_code == 200
    assert "新 Token 已创建" in created.text
    match = re.search(r'<code id="new-token">([^<]+)</code>', created.text)
    assert match is not None, created.text
    plaintext = match.group(1)
    assert plaintext.startswith("gw_")
    principal = await env.services.tokens.authenticate(plaintext)
    assert principal is not None and principal.user_name == "ui-user"

    duplicate = await env.client.post(
        "/admin/tokens/new", data={"_csrf": csrf, "user_name": "ui-user"}
    )
    assert duplicate.status_code == 200
    assert "已有启用 Token" in duplicate.text

    row = next(
        item for item in await env.services.tokens.list_all() if item.user_name == "ui-user"
    )
    disabled = await env.client.post(
        f"/admin/tokens/{row.id}/disable", data={"_csrf": csrf}
    )
    assert disabled.status_code == 302
    assert await env.services.tokens.authenticate(plaintext) is None

    deleted = await env.client.post(f"/admin/tokens/{row.id}/delete", data={"_csrf": csrf})
    assert deleted.status_code == 302
    assert all(item.user_name != "ui-user" for item in await env.services.tokens.list_all())


async def test_audit_list_filter_and_detail(env):
    csrf = await admin_login(env)
    ds = await env.services.datasources.get_by_name(env.ds_name)
    request_id = str(uuid4())
    await env.services.audit.record_denied(
        request_id,
        "mcp_tool",
        None,
        ds,
        "execute_sql",
        "SELECT 42 AS answer",
        "10.1.2.3",
        "DDL_BLOCKED",
        "测试拒绝",
    )
    pending_id = str(uuid4())
    await env.services.audit.start(
        pending_id, "mcp_tool", None, ds, "search_objects", None, "10.1.2.3"
    )

    listing = await env.client.get("/admin/audit?status=denied&q=42")
    assert listing.status_code == 200
    assert request_id in listing.text
    assert pending_id not in listing.text

    detail = await env.client.get(f"/admin/audit/{request_id}")
    assert detail.status_code == 200
    assert "SELECT 42 AS answer" in detail.text
    assert "DDL_BLOCKED" in detail.text
    assert "10.1.2.3" in detail.text

    assert (await env.client.get("/admin/audit?from=invalid")).status_code == 200
    missing = await env.client.get(f"/admin/audit/{uuid4()}")
    assert missing.status_code == 404
    assert csrf


async def test_audit_database_column(env):
    """AC45: one record per event type; a supplied database shows its name, an omitted one shows —."""
    csrf = await admin_login(env)
    assert csrf
    ds = await env.services.datasources.get_by_name(env.ds_name)
    rows = {
        "mcp_tool": "report",
        "mysql_export": "report",
        "credential_lifecycle": "report",
        "mysql_auth": "report",
    }
    request_ids = {}
    for event_type, database in rows.items():
        request_id = str(uuid4())
        request_ids[event_type] = request_id
        await env.services.audit.start(
            request_id,
            event_type,
            None,
            ds,
            "execute_sql",
            "SELECT 1",
            "10.1.2.3",
            database_name=database,
        )
    null_id = str(uuid4())
    await env.services.audit.start(
        null_id, "mcp_tool", None, ds, "search_objects", None, "10.1.2.3"
    )

    listing = await env.client.get("/admin/audit?datasource_name=" + ds.name)
    assert listing.status_code == 200
    assert "<th>库</th>" in listing.text
    for request_id in request_ids.values():
        detail = await env.client.get(f"/admin/audit/{request_id}")
        assert detail.status_code == 200
        assert "<dt>库</dt><dd class=\"mono\">report</dd>" in detail.text
    null_detail = await env.client.get(f"/admin/audit/{null_id}")
    assert '<dt>库</dt><dd class="mono">—</dd>' in null_detail.text


async def test_settings_retention_and_password_change(env):
    csrf = await admin_login(env)
    old_cookie = env.client.cookies.get(COOKIE_NAME)
    assert old_cookie

    settings = await env.client.get("/admin/settings")
    assert settings.status_code == 200
    assert "保留天数" in settings.text

    saved = await env.client.post(
        "/admin/settings/retention", data={"_csrf": csrf, "retention_days": "30"}
    )
    assert saved.status_code == 302
    assert await env.services.audit.get_retention_days() == 30

    invalid = await env.client.post(
        "/admin/settings/retention", data={"_csrf": csrf, "retention_days": "0"}
    )
    assert invalid.status_code == 400
    assert "保留天数" in invalid.text
    assert await env.services.audit.get_retention_days() == 30

    wrong_old = await env.client.post(
        "/admin/settings/password",
        data={
            "_csrf": csrf,
            "old_password": "not-the-password",
            "new_password": "new-password-123",
            "confirm_password": "new-password-123",
        },
    )
    assert wrong_old.status_code == 400
    assert "旧密码不正确" in wrong_old.text

    mismatch = await env.client.post(
        "/admin/settings/password",
        data={
            "_csrf": csrf,
            "old_password": env.admin_password,
            "new_password": "new-password-123",
            "confirm_password": "new-password-456",
        },
    )
    assert mismatch.status_code == 400
    assert "不一致" in mismatch.text

    changed = await env.client.post(
        "/admin/settings/password",
        data={
            "_csrf": csrf,
            "old_password": env.admin_password,
            "new_password": "new-password-123",
            "confirm_password": "new-password-123",
        },
    )
    assert changed.status_code == 302
    assert "/admin/login" in changed.headers["location"]
    assert COOKIE_NAME not in env.client.cookies

    env.client.cookies.clear()
    expired = await env.client.get(
        "/admin", headers={"Cookie": f"{COOKIE_NAME}={old_cookie}"}
    )
    assert expired.status_code == 401
    assert "会话已失效" in expired.text

    old_login = await env.client.post(
        "/admin/login",
        data={"username": env.admin_user, "password": env.admin_password},
    )
    assert old_login.status_code == 401

    new_login = await env.client.post(
        "/admin/login",
        data={"username": env.admin_user, "password": "new-password-123"},
    )
    assert new_login.status_code == 302
    assert (await env.client.get("/admin")).status_code == 200


async def test_dashboard_counts_and_manual_cleanup(env):
    csrf = await admin_login(env)
    ds = await env.services.datasources.get_by_name(env.ds_name)
    now = utcnow()
    old_id = str(uuid4())
    fresh_id = str(uuid4())
    async with env.services.session_factory() as session:
        session.add(
            AuditLog(
                request_id=old_id,
                event_type="mcp_tool",
                user_name="tester",
                datasource_id=ds.id,
                datasource_name=ds.name,
                status="success",
                created_at=now - timedelta(days=400),
                finished_at=now - timedelta(days=400),
            )
        )
        session.add(
            AuditLog(
                request_id=fresh_id,
                event_type="mcp_tool",
                user_name="tester",
                datasource_id=ds.id,
                datasource_name=ds.name,
                status="success",
                created_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1),
            )
        )
        await session.commit()

    cleaned = await env.client.post("/admin/settings/cleanup", data={"_csrf": csrf})
    assert cleaned.status_code == 302
    assert "msg=cleanup_done" in cleaned.headers["location"]
    assert "n=1" in cleaned.headers["location"]
    assert await env.services.audit.get(old_id) is None
    assert await env.services.audit.get(fresh_id) is not None

    dashboard = await env.client.get("/admin")
    assert dashboard.status_code == 200
    assert "今日" in dashboard.text
