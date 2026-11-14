"""Per-request locale selection middleware."""

from __future__ import annotations

from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.i18n import (
    DEFAULT_LOCALE,
    LOCALE_COOKIE,
    LOCALE_MAX_AGE,
    normalize_locale,
    reset_locale,
    resolve_locale,
    set_locale,
)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _cookie(scope: Scope, name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - malformed cookies must not break a request
        return None
    item = jar.get(name)
    return item.value if item is not None else None


def _query_value(scope: Scope, name: str) -> str | None:
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    values = parse_qs(raw.decode("latin-1")).get(name)
    return values[0] if values else None


class LocaleMiddleware:
    """Set the active locale and remember an explicit ``?lang=`` choice in a cookie."""

    def __init__(self, app: ASGIApp, *, mcp_language: str = DEFAULT_LOCALE) -> None:
        self.app = app
        self.mcp_language = normalize_locale(mcp_language) or DEFAULT_LOCALE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        chosen: str | None = None
        if is_mcp:
            locale = self.mcp_language
        else:
            query_lang = _query_value(scope, "lang")
            chosen = normalize_locale(query_lang)
            locale = resolve_locale(
                _cookie(scope, LOCALE_COOKIE),
                _header(scope, b"accept-language"),
                query_lang,
            )

        if chosen is None:
            token = set_locale(locale)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_locale(token)
            return

        async def send_with_cookie(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append(
                    "set-cookie",
                    f"{LOCALE_COOKIE}={chosen}; Path=/; Max-Age={LOCALE_MAX_AGE}; SameSite=Lax",
                )
            await send(message)

        token = set_locale(locale)
        try:
            await self.app(scope, receive, send_with_cookie)
        finally:
            reset_locale(token)
