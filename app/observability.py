"""Request correlation: a request id per HTTP call, echoed back and attached to every log record."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from logging import Filter, LogRecord
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_MAX_LEN = 128
# Accept what reverse proxies actually emit (trace ids, UUIDs, W3C-style values); anything else is replaced.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_request_id: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    """Use the id of the HTTP request being served; empty outside a request (scripts, jobs, tests)."""
    return _request_id.get()


def new_request_id() -> str:
    """Reuse the inbound id so HTTP logs and audit rows share one correlation key."""
    return _request_id.get() or str(uuid4())


def sanitize_request_id(value: str | None) -> str:
    value = (value or "").strip()
    if not value or len(value) > REQUEST_ID_MAX_LEN or not _VALID_REQUEST_ID.match(value):
        return str(uuid4())
    return value


def set_request_id(value: str) -> Token:
    return _request_id.set(value)


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


class RequestIdFilter(Filter):
    """Give every record a ``request_id`` field so one format works for in-request and job logs."""

    def filter(self, record: LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = _request_id.get() or "-"
        return True


class RequestContextMiddleware:
    """Outermost HTTP middleware: bind the request id and echo it on the response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = sanitize_request_id(_header(scope, REQUEST_ID_HEADER))
        token = _request_id.set(request_id)

        async def send_with_request_id(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((REQUEST_ID_HEADER.lower().encode("latin-1"), request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)


def _header(scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key.lower() == target:
            return value.decode("latin-1")
    return None
