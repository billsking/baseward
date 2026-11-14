"""i18n catalog, negotiation, and English rendering tests (no MySQL)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.admin.rendering import render, render_error, render_login
from app.i18n import (
    ERROR_MESSAGES,
    LABELS,
    MESSAGES,
    js_messages,
    parse_accept_language,
    reset_locale,
    resolve_locale,
    set_locale,
    t,
    translate_error,
    translate_label,
)
from app.mcp.server import build_mcp
from app.mcp.texts import TEXTS, mcp_texts

CJK = re.compile(r"[\u4e00-\u9fff]")


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


@pytest.fixture
def english_locale():
    token = set_locale("en")
    try:
        yield
    finally:
        reset_locale(token)


def test_accept_language_negotiation():
    assert parse_accept_language("en-US,en;q=0.9,zh-CN;q=0.8") == "en"
    assert parse_accept_language("zh;q=0.8,en;q=0.9") == "en"
    assert parse_accept_language("fr-FR") is None
    assert parse_accept_language(None) is None


def test_resolve_locale_precedence():
    assert resolve_locale("en", "zh-CN,zh;q=0.9") == "en"
    assert resolve_locale(None, "en-US") == "en"
    assert resolve_locale(None, None) == "zh-CN"
    assert resolve_locale("zh-CN", "en-US", "en") == "en"


def test_catalog_is_complete_and_consistent():
    assert MESSAGES
    for key, entry in MESSAGES.items():
        assert set(entry) == {"zh-CN", "en"}, key
        assert entry["zh-CN"].strip(), key
        assert entry["en"].strip(), key
        assert not CJK.search(entry["en"]), key
        placeholders = {
            locale: set(re.findall(r"\{(\w+)\}", text))
            for locale, text in entry.items()
        }
        assert placeholders["zh-CN"] == placeholders["en"], key
    for key, entry in {
        **{f"error:{k}": v for k, v in ERROR_MESSAGES.items()},
        **{f"label:{k}": v for k, v in LABELS.items()},
    }.items():
        assert entry.strip(), key
        assert not CJK.search(entry), key


def test_translate_error_and_label(english_locale):
    assert translate_error("数据源不存在") == "Datasource not found"
    assert translate_error("unknown message") == "unknown message"
    assert translate_label("库名") == "Database name"
    assert translate_label("unknown label") == "unknown label"


def test_t_falls_back_to_key_for_unknown():
    assert t("missing.key") == "missing.key"


def test_js_messages_are_prefixed_and_translated(english_locale):
    messages = js_messages()
    assert "scope.apply" in messages
    assert "scope.apply" not in messages["scope.apply"]
    assert messages["scope.apply"] == "Apply"
    assert all(not key.startswith("js.") for key in messages)


def test_english_error_page(english_locale):
    response = render_error(make_request(), 404, t("error.not_found"), "审计记录不存在")
    body = response.body.decode()
    assert "Audit record not found" in body


def test_english_login_page(english_locale):
    response = render_login(make_request("/admin/login"), "用户名或密码错误")
    body = response.body.decode()
    assert "Incorrect username or password" in body
    assert "Sign in" in body


def test_english_dashboard_render(english_locale):
    row = SimpleNamespace(
        request_id="8f0c0000-0000-0000-0000-000000000001",
        event_type="mcp_tool",
        user_name="alice",
        token_prefix="gw_abcdefg",
        datasource_id=1,
        datasource_name="demo",
        tool="execute_sql",
        sql_text="SELECT 1",
        status="success",
        deny_reason=None,
        error_message=None,
        duration_ms=12,
        row_count=1,
        client_ip="192.0.2.8",
        created_at=datetime(2026, 10, 22, 8, 30, 0),
        finished_at=datetime(2026, 10, 22, 8, 30, 0),
    )
    response = render(
        make_request(),
        "dashboard.html",
        {
            "session": SimpleNamespace(username="admin", csrf="c" * 64),
            "page_title": t("nav.dashboard"),
            "ds_total": 2,
            "ds_enabled": 1,
            "token_enabled": 3,
            "today_total": 10,
            "today_denied": 2,
            "recent": [row],
        },
    )
    body = response.body.decode()
    assert "Dashboard" in body
    assert "Success" in body
    assert '<html lang="en">' in body


def test_static_js_has_no_chinese_literals():
    static = Path(__file__).resolve().parents[2] / "app" / "admin" / "static"
    for path in static.glob("*.js"):
        assert not CJK.search(path.read_text(encoding="utf-8")), path.name


def test_mcp_texts_cover_both_languages():
    zh = mcp_texts("zh-CN")
    en = mcp_texts("en")
    assert set(zh) == set(en)
    assert set(zh) == set(TEXTS["zh-CN"])
    assert "数据库查询网关" in zh["instructions"]
    assert "Database query gateway" in en["instructions"]
    for key, text in en.items():
        if key == "error.unauthenticated":
            continue
        assert text.strip(), key
        assert not CJK.search(text), key
    assert mcp_texts("fr") is TEXTS["zh-CN"]


def test_mcp_registration_uses_selected_language():
    services = SimpleNamespace(settings=SimpleNamespace(mcp_language="en"))
    mcp = build_mcp(services)
    assert "Database query gateway" in mcp.instructions
    tools = {tool.name: tool.description for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {
        "list_datasources",
        "describe_datasource",
        "search_objects",
        "execute_sql",
        "issue_export_credential",
    }
    assert (tools["execute_sql"] or "").startswith("Run a single SQL statement")
    assert not any(CJK.search(text or "") for text in tools.values())
