"""MCP admission control: global/per-token concurrency caps and the per-token rate limit."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.config import Settings
from app.mcp import pipeline
from app.mcp.limits import (
    RATE_LIMITED,
    TOO_MANY_CONCURRENT,
    McpLimiter,
)
from app.mcp.server import build_mcp
from app.observability import reset_request_id, set_request_id
from app.security.principal import Principal, current_principal

PRINCIPAL = Principal(user_name="reader", token_id=7, token_prefix="gw_test")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ---------- limiter ----------


def test_global_cap_rejects_immediately_and_release_frees_a_slot() -> None:
    limiter = McpLimiter(2, 0, 0)
    assert limiter.try_acquire(1) is None
    assert limiter.try_acquire(2) is None
    rejection = limiter.try_acquire(3)
    assert rejection is not None
    assert rejection.code == TOO_MANY_CONCURRENT
    assert rejection.limit == 2

    limiter.release(1)
    assert limiter.active == 1
    assert limiter.try_acquire(3) is None
    assert limiter.active == 2


def test_per_token_cap_does_not_affect_other_tokens() -> None:
    limiter = McpLimiter(10, 1, 0)
    assert limiter.try_acquire(7) is None
    rejection = limiter.try_acquire(7)
    assert rejection is not None
    assert rejection.code == TOO_MANY_CONCURRENT
    assert rejection.message_key == "error.too_many_concurrent_token"
    assert rejection.limit == 1

    assert limiter.try_acquire(8) is None
    limiter.release(7)
    assert limiter.try_acquire(7) is None


def test_zero_per_token_cap_only_uses_the_global_cap() -> None:
    limiter = McpLimiter(2, 0, 0)
    assert limiter.try_acquire(7) is None
    assert limiter.try_acquire(7) is None
    assert limiter.try_acquire(7) is not None


def test_rate_limit_is_a_fixed_minute_window() -> None:
    clock = _Clock()
    limiter = McpLimiter(10, 0, 2, clock=clock)
    assert limiter.try_acquire(1) is None
    clock.now = 59.0
    assert limiter.try_acquire(1) is None

    rejection = limiter.try_acquire(1)
    assert rejection is not None
    assert rejection.code == RATE_LIMITED
    assert rejection.limit == 2

    clock.now = 60.0
    assert limiter.try_acquire(1) is None


def test_rate_limit_is_counted_per_token() -> None:
    limiter = McpLimiter(10, 0, 1)
    assert limiter.try_acquire(1) is None
    assert limiter.try_acquire(1) is not None
    assert limiter.try_acquire(2) is None


def test_release_never_goes_negative() -> None:
    limiter = McpLimiter(1, 1, 0)
    limiter.release(1)
    limiter.release(1)
    assert limiter.active == 0
    assert limiter.try_acquire(1) is None


# ---------- tool boundary ----------


def _services(limiter: McpLimiter) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(mcp_language="en"), mcp_limiter=limiter
    )


async def _call_list_datasources(services) -> None:
    mcp = build_mcp(services)
    principal_token = current_principal.set(PRINCIPAL)
    request_token = set_request_id("trace-tool-test")
    try:
        await mcp._tool_manager.call_tool("list_datasources", {})
    finally:
        reset_request_id(request_token)
        current_principal.reset(principal_token)


def error_payload(exc: ToolError) -> dict:
    # FastMCP prefixes the message with "Error executing tool ...: "; the JSON follows.
    text = str(exc)
    return json.loads(text[text.index("{"):])


async def test_tool_is_rejected_when_the_token_is_at_its_concurrency_cap() -> None:
    limiter = McpLimiter(10, 1, 0)
    assert limiter.try_acquire(PRINCIPAL.token_id) is None

    with pytest.raises(ToolError) as exc_info:
        await _call_list_datasources(_services(limiter))

    payload = error_payload(exc_info.value)
    assert payload["error"]["code"] == TOO_MANY_CONCURRENT
    assert "concurrent" in payload["error"]["message"]
    assert payload["error"]["request_id"] == "trace-tool-test"
    # The rejected call never took a slot, so the pre-existing holder is still counted.
    limiter.release(PRINCIPAL.token_id)
    assert limiter.active == 0


async def test_tool_is_rejected_when_the_rate_budget_is_spent() -> None:
    limiter = McpLimiter(10, 10, 1)
    assert limiter.try_acquire(PRINCIPAL.token_id) is None
    limiter.release(PRINCIPAL.token_id)

    with pytest.raises(ToolError) as exc_info:
        await _call_list_datasources(_services(limiter))

    payload = error_payload(exc_info.value)
    assert payload["error"]["code"] == RATE_LIMITED
    assert "per minute" in payload["error"]["message"]
    assert payload["error"]["request_id"] == "trace-tool-test"


async def test_missing_principal_error_carries_the_request_id() -> None:
    mcp = build_mcp(_services(McpLimiter(1, 1, 0)))
    request_token = set_request_id("trace-unauthenticated")
    try:
        with pytest.raises(ToolError) as exc_info:
            await mcp._tool_manager.call_tool("list_datasources", {})
    finally:
        reset_request_id(request_token)

    payload = error_payload(exc_info.value)
    assert payload["error"]["code"] == "AUTH_INVALID"
    assert payload["error"]["request_id"] == "trace-unauthenticated"


async def test_slot_is_released_when_the_tool_raises(monkeypatch) -> None:
    limiter = McpLimiter(1, 1, 0)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "list_datasources", boom)

    with pytest.raises(ToolError, match="boom"):
        await _call_list_datasources(_services(limiter))

    assert limiter.active == 0
    assert limiter.try_acquire(PRINCIPAL.token_id) is None


# ---------- configuration ----------


def test_admission_and_pool_defaults() -> None:
    settings = Settings()
    assert settings.mcp_max_concurrent == 32
    assert settings.mcp_max_concurrent_per_token == 4
    assert settings.mcp_rate_limit_per_minute == 120
    assert settings.datasource_pool_size == 5
    assert settings.datasource_pool_max_overflow == 5
    assert settings.datasource_pool_recycle_seconds == 3600
