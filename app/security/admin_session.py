"""Slide the admin session cookie's idle timer on browser requests.

The signed cookie carries the last-activity timestamp, so no server-side session store is
needed. A valid cookie older than the refresh interval is re-signed with a fresh timestamp;
an expired or unparsable cookie is deleted so browsers stop replaying it.
"""

from __future__ import annotations

from starlette.requests import Request

from app.security.admin_auth import (
    COOKIE_NAME,
    deletion_cookie_header,
    read_session_token,
    refresh_needed,
    refresh_session,
    session_cookie_header,
)


class AdminSessionRefreshMiddleware:
    """Only touch /admin HTML traffic; static assets and the login POST are skipped cheaply."""

    def __init__(self, app, services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _handles(scope):
            await self.app(scope, receive, send)
            return

        token = Request(scope).cookies.get(COOKIE_NAME)
        cookie: str | None = None
        if token:
            session = read_session_token(token, self.services)
            if session is None:
                cookie = deletion_cookie_header()
            elif refresh_needed(session):
                cookie = session_cookie_header(refresh_session(session), self.services)

        if cookie is None:
            await self.app(scope, receive, send)
            return

        encoded = cookie.encode("latin-1")

        async def send_with_cookie(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # Login and logout set or clear the cookie themselves; never override them.
                if not _sets_session_cookie(headers):
                    headers.append((b"set-cookie", encoded))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _handles(scope) -> bool:
    path = scope.get("path", "")
    return path.startswith("/admin") and not path.startswith("/admin/static")


def _sets_session_cookie(headers) -> bool:
    marker = COOKIE_NAME.encode("latin-1")
    for key, value in headers:
        if key.lower() == b"set-cookie" and marker in value.lower():
            return True
    return False
