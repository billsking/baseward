"""Security response headers, including a per-request CSP nonce for the admin UI.

The admin pages have exactly one inline script (the i18n bootstrap), so CSP can stay strict:
no ``unsafe-inline`` and no ``unsafe-eval``. The nonce is per request and exposed to templates
through ``scope["state"]["csp_nonce"]``.
"""

from __future__ import annotations

import secrets

CSP_NONCE_BYTES = 16

_STATIC_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
)

# Admin pages carry session state and data previews; browsers and proxies must not cache them.
_ADMIN_HTML_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"cross-origin-opener-policy", b"same-origin"),
)

_HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def _csp(nonce: str) -> bytes:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    ).encode("latin-1")


class SecurityHeadersMiddleware:
    """Attach security headers to every HTTP response without touching the body."""

    def __init__(self, app, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(CSP_NONCE_BYTES)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        headers = list(_STATIC_HEADERS)
        headers.append((b"content-security-policy", _csp(nonce)))
        if self.hsts:
            headers.append(_HSTS)

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                existing = {
                    key.lower() for key, _value in message.get("headers") or []
                }
                merged = list(message.get("headers") or [])
                merged.extend(
                    (key, value) for key, value in headers if key not in existing
                )
                if _is_admin_html(scope, merged):
                    merged_keys = {key.lower() for key, _value in merged}
                    merged.extend(
                        (key, value)
                        for key, value in _ADMIN_HTML_HEADERS
                        if key not in merged_keys
                    )
                message = {**message, "headers": merged}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _is_admin_html(scope, headers) -> bool:
    path = scope.get("path", "")
    if not path.startswith("/admin") or path.startswith("/admin/static"):
        return False
    for key, value in headers:
        if key.lower() == b"content-type":
            return b"text/html" in value.lower()
    return False
