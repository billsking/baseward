"""Build the FastMCP instance and register tools."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp import pipeline
from app.mcp.limits import LimitRejection
from app.mcp.texts import mcp_texts
from app.observability import new_request_id
from app.security.principal import current_client_ip, current_principal

logger = logging.getLogger(__name__)


def _tool_error(code: str, message: str) -> ToolError:
    payload = {"error": {"code": code, "message": message, "request_id": new_request_id()}}
    return ToolError(json.dumps(payload, ensure_ascii=False))


def _principal_or_error(texts: dict[str, str]):
    principal = current_principal.get()
    if principal is None:
        raise _tool_error("AUTH_INVALID", texts["error.unauthenticated"])
    return principal


@asynccontextmanager
async def _admission(services, texts: dict[str, str], principal, tool: str, metrics: Any):
    """Reject immediately when the gateway or the token is at its concurrency/rate cap.

    Throttled calls are logged but not audited: writing one audit row per rejected request
    would amplify exactly the load the limiter exists to shed.
    """
    rejection: LimitRejection | None = services.mcp_limiter.try_acquire(
        principal.token_id
    )
    if rejection is not None:
        logger.warning(
            "channel=mcp MCP 请求被限流 token_id=%s tool=%s code=%s limit=%s",
            principal.token_id,
            tool,
            rejection.code,
            rejection.limit,
        )
        if metrics is not None:
            metrics.mcp_tool_calls.labels(tool, "throttled").inc()
        raise _tool_error(
            rejection.code, texts[rejection.message_key].format(limit=rejection.limit)
        )
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        services.mcp_limiter.release(principal.token_id)
        if metrics is not None:
            metrics.mcp_tool_duration.labels(tool).observe(time.perf_counter() - started)
            metrics.mcp_tool_calls.labels(tool, outcome).inc()


def build_mcp(services, metrics: Any = None) -> FastMCP:
    texts = mcp_texts(services.settings.mcp_language)
    mcp = FastMCP(
        "baseward",
        instructions=texts["instructions"],
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(description=texts["tool.list_datasources"])
    async def list_datasources() -> dict:
        principal = _principal_or_error(texts)
        async with _admission(services, texts, principal, "list_datasources", metrics):
            return await pipeline.list_datasources(
                services, principal, current_client_ip.get()
            )

    @mcp.tool(description=texts["tool.describe_datasource"])
    async def describe_datasource(
        datasource: str,
        pattern: str | None = None,
        database: str | None = None,
        include_columns: bool = True,
        limit: int = 50,
    ) -> dict:
        principal = _principal_or_error(texts)
        async with _admission(services, texts, principal, "describe_datasource", metrics):
            return await pipeline.describe_datasource(
                services,
                principal,
                current_client_ip.get(),
                datasource,
                pattern=pattern,
                database=database,
                include_columns=include_columns,
                limit=limit,
            )

    @mcp.tool(description=texts["tool.search_objects"])
    async def search_objects(
        datasource: str,
        pattern: str,
        object_type: str = "all",
        database: str | None = None,
    ) -> dict:
        principal = _principal_or_error(texts)
        async with _admission(services, texts, principal, "search_objects", metrics):
            return await pipeline.search_objects(
                services,
                principal,
                current_client_ip.get(),
                datasource,
                pattern,
                object_type,
                database,
            )

    @mcp.tool(description=texts["tool.execute_sql"])
    async def execute_sql(
        datasource: str, sql: str, database: str | None = None
    ) -> dict:
        principal = _principal_or_error(texts)
        async with _admission(services, texts, principal, "execute_sql", metrics):
            return await pipeline.execute_sql(
                services,
                principal,
                current_client_ip.get(),
                datasource,
                sql,
                database,
            )

    @mcp.tool(description=texts["tool.issue_export_credential"])
    async def issue_export_credential(
        datasource: str,
        sql: str,
        reason: str,
        database: str | None = None,
    ) -> dict:
        principal = _principal_or_error(texts)
        async with _admission(
            services, texts, principal, "issue_export_credential", metrics
        ):
            return await pipeline.issue_export_credential(
                services,
                principal,
                current_client_ip.get(),
                datasource,
                sql,
                reason,
                database,
            )

    return mcp
