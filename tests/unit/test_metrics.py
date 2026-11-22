"""Prometheus metrics: bounded route labels, HTTP samples, tool outcomes and pool gauges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp import pipeline
from app.mcp.limits import McpLimiter
from app.mcp.server import build_mcp
from app.metrics import Metrics, MetricsMiddleware, route_label
from app.security.principal import Principal, current_principal


class _FakeLimiter:
    active = 3


class _FakePool:
    def snapshot(self) -> list[tuple[int, int, int, int]]:
        return [(2, 1, 5, 0)]


def make_services(limiter: McpLimiter | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(mcp_language="en"),
        mcp_limiter=limiter or _FakeLimiter(),
        pool=_FakePool(),
    )


async def _noop_send(message) -> None:
    return None


class _OkApp:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp", "/mcp"),
        ("/mcp/", "/mcp"),
        ("/admin", "/admin"),
        ("/admin/datasources/7/edit", "/admin"),
        ("/api/exchange/abc", "/api/exchange"),
        ("/", "/"),
        ("/whatever", "other"),
        ("/healthz", None),
        ("/readyz", None),
        ("/metrics", None),
        ("/admin/static/app.css", None),
        ("", None),
    ],
)
def test_route_labels_stay_bounded(path: str, expected: str | None) -> None:
    assert route_label(path) == expected


async def test_http_middleware_counts_status_and_latency() -> None:
    metrics = Metrics(make_services())
    middleware = MetricsMiddleware(_OkApp(status=201), metrics)
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": []}

    await middleware(scope, None, _noop_send)
    body = metrics.render().decode()

    assert 'baseward_http_requests_total{method="POST",route="/mcp",status="201"} 1.0' in body
    assert 'baseward_http_request_duration_seconds_count{method="POST",route="/mcp"} 1.0' in body


async def test_http_middleware_skips_health_probes() -> None:
    metrics = Metrics(make_services())
    middleware = MetricsMiddleware(_OkApp(), metrics)
    await middleware({"type": "http", "method": "GET", "path": "/healthz", "headers": []}, None, _noop_send)

    render = metrics.render().decode()
    assert 'route="/healthz"' not in render
    assert "baseward_http_requests_total{" not in render


def test_render_exposes_active_calls_and_pool_occupancy() -> None:
    body = Metrics(make_services()).render().decode()
    assert "baseward_mcp_active_calls 3.0" in body
    assert (
        'baseward_datasource_pool_connections{datasource_id="2",state="checked_out"} 1.0'
        in body
    )
    assert (
        'baseward_datasource_pool_connections{datasource_id="2",state="size"} 5.0' in body
    )


def _bind_principal(token_id: int):
    return current_principal.set(
        Principal(user_name="reader", token_id=token_id, token_prefix="gw_test")
    )


async def test_successful_tool_call_is_counted(monkeypatch) -> None:
    async def ok(*args, **kwargs):
        return {"datasources": []}

    monkeypatch.setattr(pipeline, "list_datasources", ok)
    limiter = McpLimiter(10, 10, 0)
    services = make_services(limiter)
    metrics = Metrics(services)
    mcp = build_mcp(services, metrics)

    token = _bind_principal(7)
    try:
        await mcp._tool_manager.call_tool("list_datasources", {})
    finally:
        current_principal.reset(token)

    body = metrics.render().decode()
    assert 'outcome="ok"' in body
    assert 'tool="list_datasources"' in body
    assert "baseward_mcp_tool_duration_seconds_count" in body
    assert limiter.active == 0


async def test_throttled_tool_call_is_counted() -> None:
    limiter = McpLimiter(10, 10, 1)
    services = make_services(limiter)
    metrics = Metrics(services)
    mcp = build_mcp(services, metrics)
    # Spend the single per-minute slot without holding a concurrency slot.
    assert limiter.try_acquire(7) is None
    limiter.release(7)

    token = _bind_principal(7)
    try:
        with pytest.raises(ToolError):
            await mcp._tool_manager.call_tool("list_datasources", {})
    finally:
        current_principal.reset(token)

    body = metrics.render().decode()
    assert 'outcome="throttled"' in body
    assert 'tool="list_datasources"' in body
