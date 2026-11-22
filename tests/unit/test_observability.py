"""Request correlation: inbound header handling, context propagation and log fields."""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from app.logging_setup import LOG_FORMAT, JsonFormatter
from app.observability import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    RequestIdFilter,
    current_request_id,
    new_request_id,
    reset_request_id,
    sanitize_request_id,
    set_request_id,
)


async def _call_middleware(inbound: str | None, status: int = 200):
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        seen["request_id"] = current_request_id()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = []
    headers = [(b"x-request-id", inbound.encode("latin-1"))] if inbound else []
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}

    async def send(message):
        messages.append(message)

    middleware = RequestContextMiddleware(app)
    await middleware(scope, None, send)
    response_headers = dict(messages[0]["headers"])
    return seen["request_id"], response_headers.get(REQUEST_ID_HEADER.lower().encode())


async def test_valid_inbound_id_is_reused_and_echoed() -> None:
    request_id, echoed = await _call_middleware("trace-abc.1:2")
    assert request_id == "trace-abc.1:2"
    assert echoed == b"trace-abc.1:2"


async def test_missing_inbound_id_is_generated() -> None:
    request_id, echoed = await _call_middleware(None)
    uuid.UUID(request_id)  # a plain UUID, not attacker-controlled text
    assert echoed == request_id.encode()


@pytest.mark.parametrize("raw", ["spaces here", "a\r\nb", "x" * 129, "\u4e2d\u6587"])
def test_invalid_inbound_id_is_replaced(raw: str) -> None:
    generated = sanitize_request_id(raw)
    assert generated != raw
    uuid.UUID(generated)


async def test_context_is_reset_after_the_request() -> None:
    request_id, _ = await _call_middleware("trace-finish")
    assert request_id == "trace-finish"
    assert current_request_id() == ""


def test_new_request_id_prefers_the_bound_request() -> None:
    token = set_request_id("trace-bound")
    try:
        assert new_request_id() == "trace-bound"
    finally:
        reset_request_id(token)
    uuid.UUID(new_request_id())


def test_log_filter_attaches_the_request_id() -> None:
    token = set_request_id("trace-log")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", (), None)
        RequestIdFilter().filter(record)
        assert record.request_id == "trace-log"
        assert logging.Formatter(LOG_FORMAT).format(record).endswith("hello")
    finally:
        reset_request_id(token)

    outside = logging.LogRecord("app", logging.INFO, __file__, 1, "job", (), None)
    RequestIdFilter().filter(outside)
    assert outside.request_id == "-"


def test_json_formatter_emits_one_object_with_the_request_id() -> None:
    record = logging.LogRecord(
        "app.mcp.pipeline", logging.WARNING, __file__, 1, "拒绝查询", (), None
    )
    RequestIdFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.mcp.pipeline"
    assert payload["message"] == "拒绝查询"
    assert payload["request_id"] == "-"
    assert payload["ts"].endswith("Z")
