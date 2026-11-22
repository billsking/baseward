"""Admin template rendering smoke tests (no MySQL)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.admin.rendering import render, render_error, render_login
from app.masking.rules import AUTO_RULES, PATTERNS, STRICTNESS


def make_request(path: str = "/admin/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


SESSION = SimpleNamespace(username="admin", csrf="c" * 64)


def make_audit_row() -> SimpleNamespace:
    now = datetime(2026, 10, 22, 8, 30, 0)
    return SimpleNamespace(
        request_id="8f0c0000-0000-0000-0000-000000000001",
        event_type="mcp_tool",
        user_name="alice",
        token_prefix="gw_abcdefg",
        datasource_id=1,
        datasource_name="demo",
        tool="execute_sql",
        sql_text="SELECT id, phone FROM users WHERE id = 1",
        status="success",
        deny_reason=None,
        error_message=None,
        duration_ms=12,
        row_count=1,
        client_ip="10.0.0.8",
        created_at=now,
        finished_at=now,
    )


def make_datasource() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="demo",
        host="10.0.0.9",
        port=3306,
        default_database="demo",
        is_read_only=1,
        enabled=1,
        remark="测试库",
    )


def test_render_login_and_error():
    response = render_login(make_request("/admin/login"))
    body = response.body.decode()
    assert response.status_code == 200
    assert "BaseWard" in body
    assert 'action="/admin/login"' in body

    response = render_error(make_request("/admin/"), 404, "未找到", "页面不存在")
    body = response.body.decode()
    assert response.status_code == 404
    assert "404" in body


def test_render_dashboard():
    row = make_audit_row()
    response = render(
        make_request(),
        "dashboard.html",
        {
            "session": SESSION,
            "page_title": "仪表盘",
            "ds_total": 2,
            "ds_enabled": 1,
            "token_enabled": 3,
            "today_total": 10,
            "today_denied": 2,
            "recent": [row],
        },
    )
    body = response.body.decode()
    assert response.status_code == 200
    assert "今日调用" in body
    assert "/admin/audit/" + row.request_id in body
    assert "成功" in body


def test_render_datasources_and_form():
    ds = make_datasource()
    response = render(
        make_request("/admin/datasources"),
        "datasources.html",
        {"session": SESSION, "rows": [ds]},
    )
    body = response.body.decode()
    assert "demo" in body
    assert f"/admin/datasources/{ds.id}/policy" in body
    assert "_csrf" in body

    response = render(
        make_request("/admin/datasources/new"),
        "datasource_form.html",
        {
            "session": SESSION,
            "mode": "new",
            "values": {"port": 3306},
        },
    )
    assert 'action="/admin/datasources/new"' in response.body.decode()

    response = render(
        make_request("/admin/datasources/1/edit"),
        "datasource_form.html",
        {
            "session": SESSION,
            "mode": "edit",
            "datasource": ds,
            "values": {
                "name": ds.name,
                "host": ds.host,
                "port": ds.port,
                "username": "reader",
                "default_database": ds.default_database,
                "remark": ds.remark,
            },
        },
    )
    body = response.body.decode()
    assert "/admin/datasources/1/edit" in body
    assert "留空沿用原密码" in body


def test_render_policy():
    ds = make_datasource()
    rules = [
        SimpleNamespace(
            id=5,
            database="demo",
            table="users",
            column="phone",
            strategy="mask",
            pattern="phone",
        ),
        SimpleNamespace(
            id=6,
            database="demo",
            table="users",
            column="email",
            strategy="off",
            pattern=None,
        ),
    ]
    response = render(
        make_request("/admin/datasources/1/policy"),
        "policy.html",
        {
            "session": SESSION,
            "datasource": ds,
            "rules": rules,
            "auto_rules": AUTO_RULES,
            "patterns": PATTERNS,
            "strictness": STRICTNESS,
        },
    )
    body = response.body.decode()
    assert "自动识别关键词" in body
    assert "关闭自动" in body
    assert "/admin/datasources/1/masking/5/delete" in body
    assert 'data-metadata-suggest="table"' in body
    assert 'data-metadata-suggest="column"' in body
    assert "/admin/static/metadata_suggest.js" in body


def test_render_tokens():
    rows = [
        SimpleNamespace(
            id=1,
            user_name="alice",
            token_prefix="gw_abcdefg",
            enabled=1,
            created_at=datetime(2026, 10, 22, 1, 0, 0),
            expires_at=datetime(2026, 10, 24, 1, 0, 0),
            last_used_at=datetime(2026, 10, 22, 2, 0, 0),
            disabled_at=None,
        )
    ]
    response = render(
        make_request("/admin/tokens"),
        "tokens.html",
        {
            "session": SESSION,
            "rows": rows,
            "new_token": "gw_plaintext_once",
            "values": {"user_name": "alice"},
            "now": datetime(2026, 10, 23, 1, 0, 0),
            "datasources": [make_datasource()],
            "scope_info": {
                1: {"summary": "数据源 1 · 表 1 · 字段 1", "restricted": True, "broken": False}
            },
        },
    )
    body = response.body.decode()
    assert "gw_plaintext_once" in body
    assert "仅本次展示" in body
    assert "/admin/tokens/1/disable" in body
    assert "/admin/tokens/1/edit" in body
    assert "数据源 1 · 表 1 · 字段 1" in body
    assert 'data-scope-picker' in body
    assert 'data-default-mode="specified"' in body
    assert 'name="expires_at"' in body
    assert "过期时间" in body
    assert "最近使用" in body


def test_render_token_edit():
    token = SimpleNamespace(
        id=7,
        user_name="bob",
        token_prefix="gw_zzzzzzz",
        enabled=1,
        created_at=datetime(2026, 10, 22, 2, 0, 0),
        disabled_at=None,
    )
    response = render(
        make_request("/admin/tokens/7/edit"),
        "token_edit.html",
        {
            "session": SESSION,
            "token": token,
            "scope_json": '{"datasources":[1],"tables":[],"columns":[]}',
            "datasources": [make_datasource()],
        },
    )
    body = response.body.decode()
    assert response.status_code == 200
    assert 'action="/admin/tokens/7/scope"' in body
    assert "不会轮换 Token 明文" in body
    assert "保存授权范围" in body
    assert "data-initial=" in body
    assert "/admin/static/token_scope.js" in body
    assert 'data-default-mode="all"' in body
    assert 'data-token-id="7"' in body
    assert 'value="all"' in body and 'value="specified"' in body
    assert "data-ds-select-all" in body
    assert "data-ds-invert" in body
    assert "data-scope-copy" in body
    assert "data-scope-template" in body
    assert "data-scope-preview" in body
    assert "重置为全部" in body


def test_render_audit_list_and_detail():
    row = make_audit_row()
    raw = {"from": "", "to": "", "user_name": "", "datasource_name": "", "status": "", "q": ""}
    response = render(
        make_request("/admin/audit"),
        "audit.html",
        {
            "session": SESSION,
            "rows": [row],
            "total": 1,
            "page": 1,
            "pages": 1,
            "raw": raw,
            "prev_url": None,
            "next_url": None,
        },
    )
    body = response.body.decode()
    assert "共 1 条" in body
    assert row.request_id in body

    response = render(
        make_request("/admin/audit/" + row.request_id),
        "audit_detail.html",
        {"session": SESSION, "row": row},
    )
    body = response.body.decode()
    assert "SELECT id, phone FROM users WHERE id = 1" in body
    assert "10.0.0.8" in body


def test_render_settings():
    response = render(
        make_request("/admin/settings"),
        "settings.html",
        {"session": SESSION, "retention_days": 180},
    )
    body = response.body.decode()
    assert 'value="180"' in body
    assert "/admin/settings/password" in body
    assert "/admin/settings/cleanup" in body


def test_templates_escape_user_content():
    row = make_audit_row()
    row.sql_text = "<script>alert(1)</script>"
    response = render(
        make_request("/admin/audit/" + row.request_id),
        "audit_detail.html",
        {"session": SESSION, "row": row},
    )
    body = response.body.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.parametrize("path", ["/admin", "/admin/"])
def test_dashboard_route_patterns(path):
    assert make_request(path).url.path in ("/admin", "/admin/")
