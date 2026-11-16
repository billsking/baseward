"""Single execution orchestration for MCP tools: auth result -> datasource -> policy -> execute -> mask -> audit."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError
from sqlglot import exp

from app.credentials.service import CREDENTIAL_ISSUE_DENIED
from app.datasources import databases
from app.errors import AuditUnavailableError, ValidationError
from app.masking.engine import apply as apply_masking
from app.mcp import introspect
from app.query import pipeline as query_pipeline
from app.sanitize import error_detail, json_safe
from app.security.principal import Principal
from app.tokens.scope import TokenScope

logger = logging.getLogger(__name__)

OBJECT_TYPES = {"all", "table", "column"}
PATTERN_LIMIT = 128
DATABASE_LIMIT = 128
RESULT_LIMIT = 200
DESCRIBE_LIMIT_MAX = 100
DESCRIBE_COLUMNS_PER_TABLE = 100
DESCRIBE_COLUMNS_TOTAL = 3000
CANDIDATE_LIMIT = query_pipeline.CANDIDATE_LIMIT

HANDOFF_HINT_ISSUED = "结果已截断，请用下方 export 凭证通过 MySQL 协议取全量数据。"
HANDOFF_NOTICE = (
    "该凭证仅用于本次导出，请勿写入代码仓库或配置文件；"
    "凭证绑定 source_ip（本次 MCP 请求被网关看到的来源 IP），取数脚本要从同一来源地址连接，"
    "否则握手会被拒绝；取数完成后可忽略，到期自动失效。"
)
EXPORT_UNAVAILABLE = "EXPORT_UNAVAILABLE"
REASON_LIMIT = 200


def _tool_error(code: str, message: str, request_id: str) -> ToolError:
    payload = {"error": {"code": code, "message": message, "request_id": request_id}}
    return ToolError(json.dumps(payload, ensure_ascii=False))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


_candidate_label = query_pipeline.candidate_label
_candidate_labels = query_pipeline.candidate_labels


async def _get_datasource(
    services,
    name: str,
    request_id: str,
    principal: Principal,
    client_ip: str,
    tool: str,
    sql_text: str | None,
    started: float,
):
    name = (name or "").strip()
    if not name:
        message = "datasource 参数不能为空"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, tool, sql_text, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)

    datasource = await services.datasources.get_by_name(name)
    if datasource is None:
        try:
            candidates = [
                item
                for item in await services.datasources.list_all()
                if principal.scope.allows_datasource(item.id)
            ]
        except Exception:  # noqa: BLE001 - used only for hints, must not block the denial response
            candidates = []
        datasource, ambiguous = introspect.resolve_alias(name, candidates)
        if ambiguous:
            message = (
                f"数据源名称有歧义：{name}；候选：{_candidate_labels(ambiguous)}"
                "。请改用精确名称"
            )
            await services.audit.record_denied(
                request_id, "mcp_tool", principal, None, tool, sql_text, client_ip,
                "DATASOURCE_AMBIGUOUS", message, _elapsed_ms(started),
            )
            raise _tool_error("DATASOURCE_AMBIGUOUS", message, request_id)
        if datasource is None:
            available = _candidate_labels(candidates)
            message = f"数据源不存在：{name}"
            if available:
                message += f"；可用数据源：{available}"
            await services.audit.record_denied(
                request_id, "mcp_tool", principal, None, tool, sql_text, client_ip,
                "DATASOURCE_NOT_FOUND", message, _elapsed_ms(started),
            )
            raise _tool_error("DATASOURCE_NOT_FOUND", message, request_id)

    if not datasource.enabled:
        message = f"数据源已停用：{datasource.name}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, datasource, tool, sql_text, client_ip,
            "DATASOURCE_DISABLED", message, _elapsed_ms(started),
        )
        raise _tool_error("DATASOURCE_DISABLED", message, request_id)

    if not principal.scope.allows_datasource(datasource.id):
        message = f"当前 Token 无权访问数据源：{datasource.name}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, datasource, tool, sql_text, client_ip,
            "DATASOURCE_DENIED", message, _elapsed_ms(started),
        )
        raise _tool_error("DATASOURCE_DENIED", message, request_id)

    return datasource


async def _start_audit(
    services,
    request_id: str,
    principal: Principal,
    datasource,
    tool: str,
    sql_text: str | None,
    client_ip: str,
    database_name: str | None = None,
) -> None:
    try:
        await services.audit.start(
            request_id,
            "mcp_tool",
            principal,
            datasource,
            tool,
            sql_text,
            client_ip,
            credential_account=principal.short_prefix,
            database_name=database_name,
        )
    except AuditUnavailableError as exc:
        raise _tool_error("AUDIT_UNAVAILABLE", "审计服务不可用，查询已阻断", request_id) from exc


def _map_exception(exc: BaseException, request_id: str) -> tuple[str, str]:
    return query_pipeline.map_exception(exc, request_id)


def _literal_sql(statement: str) -> str:
    """exec_driver_sql hands the DBAPI an empty tuple even without bound parameters, and
    aiomysql then runs "%" formatting over the statement; literal percent signs must be doubled."""
    return statement.replace("%", "%%")


def _mysql_errno(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _first_unqualified_table(ast) -> str | None:
    """For the 1146 candidate hint: take the first real table without a database qualifier (skip CTE names)."""
    if ast is None:
        return None
    cte_names = {item.alias_or_name.lower() for item in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        name = table.name or ""
        if not name or table.db or name.lower() in cte_names:
            continue
        return name
    return None


async def _database_denial(
    services,
    request_id: str,
    principal: Principal,
    datasource,
    client_ip: str,
    tool: str,
    database: str | None,
    exposed,
    available,
    started: float,
) -> None:
    """Two-level database check for metadata tools; returns early when it passes."""
    if not database:
        return
    available_names = [item.name for item in available]
    if database not in exposed:
        message = f"库 {database} 未在数据源 {datasource.name} 的可访问库中"
        if available_names:
            message += f"；可用库：{'、'.join(available_names)}"
        code = "DATABASE_NOT_EXPOSED"
    elif database not in available_names:
        message = f"当前 Token 无权访问库：{database}"
        code = "DATABASE_DENIED"
    else:
        return
    await services.audit.record_denied(
        request_id,
        "mcp_tool",
        principal,
        datasource,
        tool,
        None,
        client_ip,
        code,
        message,
        _elapsed_ms(started),
        database_name=database,
    )
    raise _tool_error(code, message, request_id)


async def execute_sql(
    services,
    principal: Principal,
    client_ip: str,
    datasource: str,
    sql: str,
    database: str | None = None,
) -> dict:
    request_id = str(uuid4())
    started = time.perf_counter()

    try:
        prepared = await query_pipeline.prepare_read(
            services=services,
            principal=principal,
            client_ip=client_ip,
            datasource_name=datasource,
            sql=sql,
            channel="mcp",
            row_cap=services.settings.mcp_result_max_rows or None,
            request_id=request_id,
            started_at=started,
            database=database,
        )
    except query_pipeline.GatewayError as exc:
        raise _tool_error(exc.code, exc.message, request_id) from exc

    if prepared.decision.is_write:
        return await _execute_write(services, prepared)

    return await _execute_read(services, principal, client_ip, prepared, sql)


async def _execute_write(services, prepared: query_pipeline.PreparedRead) -> dict:
    request_id = prepared.request_id
    query_started = time.perf_counter()
    try:
        engine = await services.pool.get(prepared.datasource)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                _literal_sql(databases.use_statement(prepared.database))
            )
            result = await conn.exec_driver_sql(_literal_sql(prepared.exec_sql))
            row_count = max(int(result.rowcount or 0), 0)
            duration = _elapsed_ms(query_started)
            await services.audit.mark_success(request_id, duration, row_count)
    except AuditUnavailableError as exc:
        await services.audit.mark_error_best_effort(
            request_id, "AUDIT_UNAVAILABLE", "审计结果更新失败", _elapsed_ms(query_started)
        )
        raise _tool_error("AUDIT_UNAVAILABLE", "审计服务不可用，查询已阻断", request_id) from exc
    except Exception as exc:  # noqa: BLE001
        code, message = _map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), _elapsed_ms(query_started)
        )
        raise _tool_error(code, message, request_id) from exc
    return {
        "request_id": request_id,
        "datasource": prepared.datasource.name,
        "database": prepared.database,
        "statement_type": prepared.decision.statement_type,
        "columns": [],
        "rows": [],
        "row_count": row_count,
        "duration_ms": duration,
        "masked_columns": [],
        "truncated": False,
        "truncated_reason": None,
        "hint": None,
        "export": None,
    }


class _HandoffUnavailable(Exception):
    """Auto-issue failure; carries only the gateway error code shown to the AI, never internal exception details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate_by_bytes(rows: list[tuple], cap: int) -> tuple[list[list], bool]:
    """JSON-safe conversion plus UTF-8 byte trimming; returns (converted and clamped rows, whether the byte cap was hit)."""
    converted = [[json_safe(value) for value in row] for row in rows]
    if cap <= 0:
        return converted, False
    kept: list[list] = []
    total = 0
    for row in converted:
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if total + size > cap:
            return kept, True
        kept.append(row)
        total += size
    return kept, False


def _sample_code(host: str, port: int, user: str, database: str, sql: str) -> str:
    """Runnable fetch script; the password is read from the environment only and never written into the generated file."""
    return (
        "import csv\n"
        "import os\n"
        "\n"
        "import pymysql\n"
        "\n"
        "conn = pymysql.connect(\n"
        f"    host={host!r}, port={port}, user={user!r},\n"
        "    password=os.environ['GATEWAY_EXPORT_PASSWORD'],\n"
        f"    database={database!r}, charset='utf8mb4', ssl_disabled=True,\n"
        ")\n"
        "try:\n"
        "    with conn.cursor(pymysql.cursors.SSCursor) as cur:\n"
        f"        cur.execute({sql!r})\n"
        "        with open('export.csv', 'w', encoding='utf-8', newline='') as f:\n"
        "            writer = csv.writer(f)\n"
        "            writer.writerow([d[0] for d in cur.description])\n"
        "            for row in cur:\n"
        "                writer.writerow(row)\n"
        "finally:\n"
        "    conn.close()\n"
    )


async def _maybe_issue_export(
    services,
    principal: Principal,
    prepared: query_pipeline.PreparedRead,
    client_ip: str,
    sql: str,
) -> dict | None:
    """Issue a credential and assemble the handoff payload when preconditions hold.

    Return None when they do not (the caller then emits the truncation hint); raise
    `_HandoffUnavailable` on issue failure so the caller degrades to a hint — the main
    query result is unaffected either way.
    """
    settings = services.settings
    host = settings.export_advertise_host
    if not settings.mcp_export_auto_issue or not settings.mysql_export_enabled or not host:
        return None
    try:
        issued = await services.credentials.issue_auto(
            token_id=principal.token_id,
            datasource_id=prepared.datasource.id,
            database=prepared.database,
            principal=principal,
            trigger_request_id=prepared.request_id,
            client_ip=client_ip,
        )
    except ValidationError as exc:
        logger.warning("自动签发导出凭证被拒 request_id=%s", prepared.request_id)
        raise _HandoffUnavailable(CREDENTIAL_ISSUE_DENIED) from exc
    except Exception as exc:  # noqa: BLE001 - handoff failures must not affect the main query
        logger.warning(
            "自动签发导出凭证失败 request_id=%s error=%s",
            prepared.request_id,
            type(exc).__name__,
        )
        raise _HandoffUnavailable("INTERNAL_ERROR") from exc

    port = settings.export_advertise_port
    database = prepared.database
    return {
        "protocol": "mysql",
        "host": host,
        "port": port,
        "user": issued.account_name,
        "password": issued.password,
        "database": database,
        "source_ip": issued.source_ip,
        "expires_at": _utc_iso(issued.expires_at),
        "sql": sql,
        "notice": HANDOFF_NOTICE,
        "sample_code": _sample_code(host, port, issued.account_name, database, sql),
    }


async def issue_export_credential(
    services,
    principal: Principal,
    client_ip: str,
    datasource: str,
    sql: str,
    reason: str,
    database: str | None = None,
) -> dict:
    """Explicitly issue an export credential after prechecking authorization against the SQL; the Agent then fetches over the MySQL protocol."""
    request_id = str(uuid4())
    started = time.perf_counter()
    settings = services.settings

    async def deny(code: str, message: str) -> None:
        await services.audit.record_denied(
            request_id,
            "mcp_tool",
            principal,
            None,
            "issue_export_credential",
            sql,
            client_ip,
            code,
            message,
            _elapsed_ms(started),
        )

    if not settings.mysql_export_enabled:
        await deny(EXPORT_UNAVAILABLE, "导出通道未启用")
        raise _tool_error(EXPORT_UNAVAILABLE, "导出通道未启用，无法签发临时凭证", request_id)
    host = settings.export_advertise_host
    if not host:
        await deny(EXPORT_UNAVAILABLE, "导出通道地址未配置")
        raise _tool_error(
            EXPORT_UNAVAILABLE, "导出通道地址未配置，无法签发临时凭证", request_id
        )

    normalized_reason = " ".join(reason.split())
    if not normalized_reason:
        await deny("INVALID_ARGUMENT", "reason 不能为空")
        raise _tool_error("INVALID_ARGUMENT", "reason 不能为空", request_id)
    if len(normalized_reason) > REASON_LIMIT:
        await deny("INVALID_ARGUMENT", "reason 过长")
        raise _tool_error(
            "INVALID_ARGUMENT", f"reason 不能超过 {REASON_LIMIT} 字符", request_id
        )

    try:
        prepared = await query_pipeline.prepare_read(
            services=services,
            principal=principal,
            client_ip=client_ip,
            datasource_name=datasource,
            sql=sql,
            channel="mcp_export",
            force_read_only=True,
            request_id=request_id,
            started_at=started,
            database=database,
        )
    except query_pipeline.GatewayError as exc:
        raise _tool_error(exc.code, exc.message, request_id) from exc

    try:
        issued = await services.credentials.issue_auto(
            token_id=principal.token_id,
            datasource_id=prepared.datasource.id,
            database=prepared.database,
            principal=principal,
            trigger_request_id=request_id,
            client_ip=client_ip,
            reason=normalized_reason,
        )
    except ValidationError as exc:
        message = "导出凭证签发被拒：数据源或授权范围不满足"
        logger.warning("显式签发导出凭证被拒 request_id=%s", request_id)
        await query_pipeline.conclude_denied(
            services, prepared, CREDENTIAL_ISSUE_DENIED, message
        )
        raise _tool_error(CREDENTIAL_ISSUE_DENIED, message, request_id) from exc
    except Exception as exc:  # noqa: BLE001 - internal exceptions must not leak
        logger.warning(
            "显式签发导出凭证失败 request_id=%s error=%s",
            request_id,
            type(exc).__name__,
        )
        await query_pipeline.conclude_error(services, prepared, exc, "INTERNAL_ERROR")
        raise _tool_error("INTERNAL_ERROR", "导出凭证签发失败，详情见审计日志", request_id) from exc

    try:
        await query_pipeline.conclude_success(services, prepared, 0)
    except query_pipeline.GatewayError as exc:
        raise _tool_error(exc.code, exc.message, request_id) from exc

    port = settings.export_advertise_port
    database = prepared.database
    return {
        "request_id": request_id,
        "protocol": "mysql",
        "host": host,
        "port": port,
        "user": issued.account_name,
        "password": issued.password,
        "database": database,
        "source_ip": issued.source_ip,
        "expires_at": _utc_iso(issued.expires_at),
        "sql": sql,
        "notice": HANDOFF_NOTICE,
        "sample_code": _sample_code(host, port, issued.account_name, database, sql),
    }


def _truncation_hint(
    settings, truncated_reason: str, export: dict | None, failure: str | None
) -> str:
    if export is not None:
        return HANDOFF_HINT_ISSUED
    if not settings.mcp_export_auto_issue:
        limit = (
            f"{settings.mcp_result_max_rows} 行"
            if truncated_reason == "rows"
            else f"{settings.mcp_result_max_bytes} 字节"
        )
        return (
            f"结果已截断（{limit}）。如需全量数据，请联系管理员签发临时导出凭证，"
            "或缩小查询范围（加 WHERE / 调小 LIMIT）。"
        )
    if not settings.mysql_export_enabled:
        return "结果已截断。导出通道未启用，请缩小查询范围后重试，或联系管理员。"
    if not settings.export_advertise_host:
        return "结果已截断。导出通道地址未配置，请缩小查询范围后重试，或联系管理员。"
    return (
        f"结果已截断。自动签发导出凭证失败（{failure}），"
        "请缩小查询范围后重试，或联系管理员。"
    )


async def _handoff(
    services,
    principal: Principal,
    prepared: query_pipeline.PreparedRead,
    client_ip: str,
    sql: str,
    truncated_reason: str | None,
) -> tuple[dict | None, str | None]:
    if truncated_reason is None:
        return None, None
    export = None
    failure = None
    try:
        export = await _maybe_issue_export(services, principal, prepared, client_ip, sql)
    except _HandoffUnavailable as exc:
        failure = exc.code
    return export, _truncation_hint(
        services.settings, truncated_reason, export, failure
    )


async def _execute_read(
    services,
    principal: Principal,
    client_ip: str,
    prepared: query_pipeline.PreparedRead,
    sql: str,
) -> dict:
    request_id = prepared.request_id
    truncated_reason: str | None = None
    try:
        engine = await services.pool.get(prepared.datasource)
        async with engine.connect() as conn:
            await conn.exec_driver_sql(
                _literal_sql(databases.use_statement(prepared.database))
            )
            result = await conn.exec_driver_sql(_literal_sql(prepared.exec_sql))
            columns = list(result.keys())
            raw_rows = result.fetchall()
        if prepared.row_cap and len(raw_rows) > prepared.row_cap:
            # The extra row fetched at the SQL layer is used only to detect truncation and is not returned to the client.
            raw_rows = raw_rows[: prepared.row_cap]
            truncated_reason = "rows"
        masked_rows, masked_columns = apply_masking(
            prepared.plan, columns, raw_rows, services.mask_key
        )
        rows, byte_truncated = _truncate_by_bytes(
            masked_rows, services.settings.mcp_result_max_bytes
        )
        truncated_reason = truncated_reason or ("bytes" if byte_truncated else None)
        duration = await query_pipeline.conclude_success(
            services, prepared, len(rows)
        )
    except query_pipeline.GatewayError as exc:
        raise _tool_error(exc.code, exc.message, request_id) from exc
    except Exception as exc:  # noqa: BLE001
        code, message = _map_exception(exc, request_id)
        await query_pipeline.conclude_error(services, prepared, exc, code)
        if _mysql_errno(exc) == 1146:
            table_name = _first_unqualified_table(prepared.decision.ast)
            candidates = await query_pipeline.suggest_databases(
                services,
                principal,
                prepared.datasource,
                table_name or "",
                prepared.database,
            )
            if candidates:
                message += (
                    f"；表 `{table_name}` 不在库 `{prepared.database}`；"
                    f"已授权的其他库中存在同名表：{'、'.join(candidates)}"
                )
        raise _tool_error(code, message, request_id) from exc

    export, hint = await _handoff(
        services, principal, prepared, client_ip, sql, truncated_reason
    )
    return {
        "request_id": request_id,
        "datasource": prepared.datasource.name,
        "database": prepared.database,
        "statement_type": prepared.decision.statement_type,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "duration_ms": duration,
        "masked_columns": masked_columns,
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
        "hint": hint,
        "export": export,
    }


async def _fetch_metadata(conn, query) -> list[tuple]:
    if query is None:
        return []
    result = await conn.exec_driver_sql(query[0], tuple(query[1]))
    return list(result.fetchall())


def _scope_allowed_tables(
    scope: TokenScope, datasource_id: int
) -> tuple[list[tuple[str, str]] | None, list[str]]:
    """Return (explicit tables, whole-database grants); None for tables means every table of the datasource."""
    table_scope = scope.table_scope(datasource_id)
    if table_scope is None:
        return None, []
    databases, pairs = table_scope
    return sorted(pairs), sorted(databases)


def _scope_allowed_columns(
    scope: TokenScope, datasource_id: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], list[str]] | None:
    table_scope = scope.table_scope(datasource_id)
    if table_scope is None:
        return None
    databases, tables = table_scope
    explicit = sorted(
        {
            (item.database, item.table, item.column)
            for item in scope.columns
            if item.datasource_id == datasource_id
        }
    )
    narrowed = {(item[0], item[1]) for item in explicit}
    return sorted(tables - narrowed), explicit, sorted(databases)


async def search_objects(
    services,
    principal: Principal,
    client_ip: str,
    datasource: str,
    pattern: str,
    object_type: str = "all",
    database: str | None = None,
) -> dict:
    request_id = str(uuid4())
    started = time.perf_counter()

    pattern = (pattern or "").strip()
    object_type = (object_type or "all").strip().lower()
    database = (database or "").strip() or None
    if not pattern or len(pattern) > PATTERN_LIMIT:
        message = f"pattern 不能为空且长度不超过 {PATTERN_LIMIT}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "search_objects", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)
    if object_type not in OBJECT_TYPES:
        message = "object_type 必须是 all/table/column 之一"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "search_objects", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)
    if database and len(database) > DATABASE_LIMIT:
        message = f"database 长度不能超过 {DATABASE_LIMIT}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "search_objects", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)

    ds = await _get_datasource(
        services, datasource, request_id, principal, client_ip, "search_objects", None, started
    )
    exposed, _policies, available = await query_pipeline.database_options(
        services, ds, principal.scope
    )
    await _database_denial(
        services,
        request_id,
        principal,
        ds,
        client_ip,
        "search_objects",
        database,
        exposed,
        available,
        started,
    )
    # With a database, search only it; otherwise search every usable database of the token (an empty list is always false, never no filter).
    search_scope: str | list[str] = database or [item.name for item in available]
    allowed_tables, unrestricted_databases = _scope_allowed_tables(
        principal.scope, ds.id
    )
    column_scope = _scope_allowed_columns(principal.scope, ds.id)
    await _start_audit(
        services,
        request_id,
        principal,
        ds,
        "search_objects",
        None,
        client_ip,
        database_name=database,
    )

    terms = introspect.tokenize(pattern)
    query_started = time.perf_counter()
    objects: list[tuple[int, dict]] = []
    truncated = False
    try:
        engine = await services.pool.get(ds)
        async with engine.connect() as conn:
            if object_type in ("all", "table"):
                rows = await _fetch_metadata(
                    conn,
                    introspect.search_tables_query(
                        terms,
                        search_scope,
                        allowed_tables,
                        unrestricted_databases=unrestricted_databases,
                    ),
                )
                truncated = truncated or len(rows) >= introspect.FETCH_LIMIT
                for table_schema, table_name, comment in rows:
                    objects.append(
                        (
                            introspect.score(terms, table_name, comment),
                            {
                                "database": table_schema,
                                "table": table_name,
                                "type": "table",
                                "comment": comment,
                            },
                        )
                    )
            if object_type in ("all", "column"):
                rows = await _fetch_metadata(
                    conn,
                    introspect.search_columns_query(
                        terms,
                        search_scope,
                        allowed_tables,
                        column_scope,
                        unrestricted_databases=unrestricted_databases,
                    ),
                )
                truncated = truncated or len(rows) >= introspect.FETCH_LIMIT
                for table_schema, table_name, column_name, data_type, comment, _ in rows:
                    objects.append(
                        (
                            introspect.score(
                                terms, column_name, comment, table_name=table_name
                            ),
                            {
                                "database": table_schema,
                                "table": table_name,
                                "type": "column",
                                "column": column_name,
                                "data_type": data_type,
                                "comment": comment,
                            },
                        )
                    )
        objects.sort(
            key=lambda item: (
                -item[0],
                item[1]["database"].lower(),
                item[1]["table"].lower(),
                item[1].get("column", "").lower(),
            )
        )
        if len(objects) > RESULT_LIMIT:
            truncated = True
            objects = objects[:RESULT_LIMIT]
        payload_objects = [item[1] for item in objects]
        duration = _elapsed_ms(query_started)
        await services.audit.mark_success(request_id, duration, len(objects))
    except AuditUnavailableError as exc:
        await services.audit.mark_error_best_effort(
            request_id, "AUDIT_UNAVAILABLE", "审计结果更新失败", _elapsed_ms(query_started)
        )
        raise _tool_error("AUDIT_UNAVAILABLE", "审计服务不可用，查询已阻断", request_id) from exc
    except Exception as exc:  # noqa: BLE001
        code, message = _map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), _elapsed_ms(query_started)
        )
        raise _tool_error(code, message, request_id) from exc

    return {
        "request_id": request_id,
        "datasource": ds.name,
        "database": database,
        "databases": [
            {"name": item.name, "read_only": item.is_read_only}
            for item in available
        ],
        "objects": payload_objects,
        "truncated": truncated,
    }


async def list_datasources(
    services,
    principal: Principal,
    client_ip: str,
) -> dict:
    request_id = str(uuid4())
    await _start_audit(
        services, request_id, principal, None, "list_datasources", None, client_ip
    )

    query_started = time.perf_counter()
    try:
        items = []
        for item in await services.datasources.list_all():
            if not (item.enabled and principal.scope.allows_datasource(item.id)):
                continue
            _exposed, policies, available = await query_pipeline.database_options(
                services, item, principal.scope
            )
            items.append(
                {
                    "name": item.name,
                    "description": item.remark,
                    "default_database": item.default_database,
                    # Aggregate field independent of the token (A29/D70); check databases[] for per-database decisions.
                    "is_read_only": bool(item.is_read_only)
                    or all(policy.is_read_only for policy in policies),
                    "databases": [
                        {"name": policy.name, "read_only": policy.is_read_only}
                        for policy in available
                    ],
                }
            )
        duration = _elapsed_ms(query_started)
        await services.audit.mark_success(request_id, duration, len(items))
    except AuditUnavailableError as exc:
        await services.audit.mark_error_best_effort(
            request_id, "AUDIT_UNAVAILABLE", "审计结果更新失败", _elapsed_ms(query_started)
        )
        raise _tool_error("AUDIT_UNAVAILABLE", "审计服务不可用，查询已阻断", request_id) from exc
    except Exception as exc:  # noqa: BLE001
        code, message = _map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), _elapsed_ms(query_started)
        )
        raise _tool_error(code, message, request_id) from exc

    return {
        "request_id": request_id,
        "datasources": items,
        "count": len(items),
    }


async def describe_datasource(
    services,
    principal: Principal,
    client_ip: str,
    datasource: str,
    pattern: str | None = None,
    database: str | None = None,
    include_columns: bool = True,
    limit: int = 50,
) -> dict:
    request_id = str(uuid4())
    started = time.perf_counter()

    pattern = (pattern or "").strip()
    database = (database or "").strip() or None
    if pattern and len(pattern) > PATTERN_LIMIT:
        message = f"pattern 长度不能超过 {PATTERN_LIMIT}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "describe_datasource", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)
    if database and len(database) > DATABASE_LIMIT:
        message = f"database 长度不能超过 {DATABASE_LIMIT}"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "describe_datasource", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)
    if not 1 <= limit <= DESCRIBE_LIMIT_MAX:
        message = f"limit 必须在 1-{DESCRIBE_LIMIT_MAX} 之间"
        await services.audit.record_denied(
            request_id, "mcp_tool", principal, None, "describe_datasource", None, client_ip,
            "INVALID_ARGUMENT", message, _elapsed_ms(started),
        )
        raise _tool_error("INVALID_ARGUMENT", message, request_id)

    ds = await _get_datasource(
        services, datasource, request_id, principal, client_ip, "describe_datasource", None, started
    )
    exposed, policies, available = await query_pipeline.database_options(
        services, ds, principal.scope
    )
    await _database_denial(
        services,
        request_id,
        principal,
        ds,
        client_ip,
        "describe_datasource",
        database,
        exposed,
        available,
        started,
    )
    # Same rule as search_objects: with a database search only it, otherwise search every usable database.
    search_scope: str | list[str] = database or [item.name for item in available]
    allowed_tables, unrestricted_databases = _scope_allowed_tables(
        principal.scope, ds.id
    )
    column_scope = _scope_allowed_columns(principal.scope, ds.id)
    await _start_audit(
        services,
        request_id,
        principal,
        ds,
        "describe_datasource",
        None,
        client_ip,
        database_name=database,
    )

    terms = introspect.tokenize(pattern)
    query_started = time.perf_counter()
    try:
        engine = await services.pool.get(ds)
        async with engine.connect() as conn:
            table_rows = await _fetch_metadata(
                conn,
                introspect.search_tables_query(
                    terms,
                    search_scope,
                    allowed_tables,
                    unrestricted_databases=unrestricted_databases,
                ),
            )
            matched: dict[tuple[str, str], str] = {
                (schema, table): comment for schema, table, comment in table_rows
            }
            column_hits: set[tuple[str, str]] = set()
            column_table_rows = await _fetch_metadata(
                conn,
                introspect.describe_column_tables_query(
                    terms,
                    search_scope,
                    allowed_tables,
                    unrestricted_databases=unrestricted_databases,
                ),
            )
            if terms:
                truncated = len(column_table_rows) >= introspect.FETCH_LIMIT
                for schema, table, comment in column_table_rows:
                    matched.setdefault((schema, table), comment)
                    column_hits.add((schema, table))
            else:
                truncated = False

            ordered = sorted(
                matched.items(),
                key=lambda item: (
                    -(
                        introspect.score(terms, item[0][1], item[1])
                        + (10 if item[0] in column_hits else 0)
                    ),
                    item[0][0].lower(),
                    item[0][1].lower(),
                ),
            )
            truncated = (
                truncated
                or len(table_rows) >= introspect.FETCH_LIMIT
                or len(ordered) > limit
            )
            selected = ordered[:limit]
            tables = [
                {"database": schema, "table": table, "comment": comment}
                for (schema, table), comment in selected
            ]
            if include_columns and selected:
                refs = [key for key, _ in selected]
                rows = await _fetch_metadata(
                    conn,
                    introspect.columns_for_tables_query(
                        refs, column_scope, limit=DESCRIBE_COLUMNS_TOTAL + 1
                    ),
                )
                buckets: dict[tuple[str, str], list[dict]] = {ref: [] for ref in refs}
                total = 0
                for (
                    schema,
                    table,
                    column,
                    data_type,
                    is_nullable,
                    column_key,
                    comment,
                    _,
                ) in rows:
                    if total >= DESCRIBE_COLUMNS_TOTAL:
                        truncated = True
                        break
                    bucket = buckets.get((schema, table))
                    if bucket is None:
                        continue
                    if len(bucket) >= DESCRIBE_COLUMNS_PER_TABLE:
                        truncated = True
                        continue
                    bucket.append(
                        {
                            "name": column,
                            "data_type": data_type,
                            "nullable": str(is_nullable).upper() == "YES",
                            "primary_key": str(column_key).upper() == "PRI",
                            "comment": comment,
                        }
                    )
                    total += 1
                for item in tables:
                    item["columns"] = buckets.get(
                        (item["database"], item["table"]), []
                    )
        duration = _elapsed_ms(query_started)
        await services.audit.mark_success(request_id, duration, len(tables))
    except AuditUnavailableError as exc:
        await services.audit.mark_error_best_effort(
            request_id, "AUDIT_UNAVAILABLE", "审计结果更新失败", _elapsed_ms(query_started)
        )
        raise _tool_error("AUDIT_UNAVAILABLE", "审计服务不可用，查询已阻断", request_id) from exc
    except Exception as exc:  # noqa: BLE001
        code, message = _map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), _elapsed_ms(query_started)
        )
        raise _tool_error(code, message, request_id) from exc

    return {
        "request_id": request_id,
        "datasource": ds.name,
        "description": getattr(ds, "remark", None),
        "default_database": ds.default_database,
        "is_read_only": bool(ds.is_read_only)
        or all(policy.is_read_only for policy in policies),
        "databases": [
            {"name": item.name, "read_only": item.is_read_only}
            for item in available
        ],
        "tables": tables,
        "truncated": truncated,
    }
