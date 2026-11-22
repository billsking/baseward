"""Prometheus metrics, opt-in via GATEWAY_METRICS_ENABLED.

Each app instance owns its registry so repeated ``create_app`` calls in tests never collide on
duplicate metric names. Labels stay bounded: routes are collapsed, never raw paths.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# Probes are scraped far more often than real traffic, and static files would only add noise.
_EXCLUDED_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
_EXCLUDED_PREFIXES = ("/admin/static/",)


def route_label(path: str) -> str | None:
    """Collapse a request path onto a bounded label; ``None`` means "do not record"."""
    if not path or path in _EXCLUDED_PATHS or path.startswith(_EXCLUDED_PREFIXES):
        return None
    if path.startswith("/mcp"):
        return "/mcp"
    if path.startswith("/admin"):
        return "/admin"
    if path.startswith("/api/exchange"):
        return "/api/exchange"
    return "/" if path == "/" else "other"


class Metrics:
    def __init__(self, services: Any) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "baseward_http_requests_total",
            "HTTP responses by method, route and status.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "baseward_http_request_duration_seconds",
            "HTTP request duration.",
            ["method", "route"],
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.mcp_tool_calls = Counter(
            "baseward_mcp_tool_calls_total",
            "MCP tool calls by tool and outcome (ok, error, throttled).",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.mcp_tool_duration = Histogram(
            "baseward_mcp_tool_duration_seconds",
            "MCP tool call duration (successful and failed calls; throttled calls are not timed).",
            ["tool"],
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.mcp_active = Gauge(
            "baseward_mcp_active_calls",
            "MCP tool calls currently in flight.",
            registry=self.registry,
        )
        self.mcp_active.set_function(lambda: services.mcp_limiter.active)
        self.registry.register(_PoolCollector(services))

    def render(self) -> bytes:
        return generate_latest(self.registry)


class _PoolCollector:
    """Report upstream pool occupancy at scrape time; idle scrapes never touch a datasource."""

    def __init__(self, services: Any) -> None:
        self._services = services

    def collect(self) -> Iterable[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "baseward_datasource_pool_connections",
            "Business datasource pool connections by state.",
            labels=["datasource_id", "state"],
        )
        for datasource_id, checked_out, size, overflow in self._services.pool.snapshot():
            values = (("checked_out", checked_out), ("size", size), ("overflow", overflow))
            for state, value in values:
                family.add_metric([str(datasource_id), state], value)
        yield family


class MetricsMiddleware:
    """Record one counter sample and one latency sample per HTTP response."""

    def __init__(self, app, metrics: Metrics) -> None:
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope, receive, send) -> None:
        route = route_label(scope.get("path", "")) if scope["type"] == "http" else None
        if route is None:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        started = time.perf_counter()
        status = 0

        async def send_with_status(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # An unhandled exception still reached the client as a 500 unless a handler said otherwise.
            self._metrics.http_requests.labels(method, route, str(status or 500)).inc()
            self._metrics.http_duration.labels(method, route).observe(
                time.perf_counter() - started
            )
