"""Build the FastMCP instance and register tools."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp import pipeline
from app.mcp.texts import mcp_texts
from app.security.principal import current_client_ip, current_principal


def _principal_or_error(texts: dict[str, str]):
    principal = current_principal.get()
    if principal is None:
        raise ToolError(
            json.dumps(
                {
                    "error": {
                        "code": "AUTH_INVALID",
                        "message": texts["error.unauthenticated"],
                        "request_id": "",
                    }
                },
                ensure_ascii=False,
            )
        )
    return principal


def build_mcp(services) -> FastMCP:
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
