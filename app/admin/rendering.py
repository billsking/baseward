"""Admin template rendering and fixed flash messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import Response

from app.clock import format_shanghai
from app.i18n import get_locale, js_messages, t, translate_error

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["dt"] = format_shanghai
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True

# Flash codes stay stable in URLs; the text comes from the i18n catalog.
MESSAGES = {
    "datasource_created": "flash.datasource_created",
    "datasource_updated": "flash.datasource_updated",
    "datasource_deleted": "flash.datasource_deleted",
    "connection_ok": "flash.connection_ok",
    "policy_saved": "flash.policy_saved",
    "rule_added": "flash.rule_added",
    "rule_deleted": "flash.rule_deleted",
    "token_created": "flash.token_created",
    "token_scope_updated": "flash.token_scope_updated",
    "token_disabled": "flash.token_disabled",
    "token_deleted": "flash.token_deleted",
    "credential_revoked": "flash.credential_revoked",
    "password_changed": "flash.password_changed",
    "retention_saved": "flash.retention_saved",
    "allowlist_saved": "flash.allowlist_saved",
    "cleanup_done": "flash.cleanup_done",
    "short_credential_revoked": "flash.short_credential_revoked",
}

STATUS_LABELS = {
    "pending": "audit.status.pending",
    "success": "audit.status.success",
    "denied": "audit.status.denied",
    "error": "audit.status.error",
    "interrupted": "audit.status.interrupted",
}

EVENT_LABELS = {
    "mcp_tool": "audit.event.mcp_tool",
    "auth_failure": "audit.event.auth_failure",
    "mysql_export": "audit.event.mysql_export",
    "mysql_auth": "audit.event.mysql_auth",
    "credential_lifecycle": "audit.event.credential_lifecycle",
    "exchange": "audit.event.exchange",
    "setting_change": "audit.event.setting_change",
    "policy_change": "audit.event.policy_change",
    "admin_login": "audit.event.admin_login",
}


def _localized_labels(mapping: dict[str, str]) -> dict[str, str]:
    return {code: t(key) for code, key in mapping.items()}


def _language_url(request: Request, locale: str) -> str:
    params = dict(request.query_params)
    params["lang"] = locale
    return f"{request.url.path}?{urlencode(params)}"


templates.env.globals["language_url"] = _language_url


def message_context(request: Request) -> dict[str, Any]:
    """Map ?msg=<code> to a fixed message; never render arbitrary text."""
    code = request.query_params.get("msg", "")
    key = MESSAGES.get(code)
    text = t(key) if key else None
    extra = ""
    if text and request.query_params.get("n"):
        n = request.query_params["n"]
        if code == "connection_ok":
            extra = t("flash.connection_ok_extra", n=n)
        elif code == "cleanup_done":
            extra = t("flash.cleanup_done_extra", n=n)
    return {"msg": text, "msg_extra": extra}


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    values: dict[str, Any] = {
        "request": request,
        "status_labels": _localized_labels(STATUS_LABELS),
        "event_labels": _localized_labels(EVENT_LABELS),
        "t": t,
        "locale": get_locale(),
        "js_messages": js_messages(),
    }
    values.update(context or {})
    if "error" in values and isinstance(values["error"], str):
        values["error"] = translate_error(values["error"])
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=values,
        status_code=status_code,
    )


def render_error(
    request: Request,
    status_code: int,
    title: str,
    message: str,
) -> Response:
    return render(
        request,
        "error.html",
        {
            "session": None,
            "title": title,
            "message": translate_error(message),
            "status_code": status_code,
        },
        status_code=status_code,
    )


def render_login(
    request: Request,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "login.html",
        {
            "session": None,
            "error": translate_error(error) if error else error,
            **message_context(request),
        },
        status_code=status_code,
    )
