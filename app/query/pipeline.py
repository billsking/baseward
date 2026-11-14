"""Channel-agnostic read-path orchestration and audit finalization (v1.4).

MCP and the MySQL export channel must share one orchestration of policy, authorization,
masking, and auditing; only this module can guarantee that consistency. The two channels only
own parameter validation, result delivery, and error mapping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Awaitable
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

from app.datasources import databases
from app.errors import AuditUnavailableError
from app.errors import DataSourceConnectionError
from app.errors import GatewayError as BaseGatewayError
from app.masking import engine as masking
from app.mcp import introspect
from app.policy import engine as policy
from app.policy import scope as scope_policy
from app.sanitize import error_detail
from app.sanitize import map_db_error as sanitize_db_error
from app.security.principal import Principal

logger = logging.getLogger(__name__)

CANDIDATE_LIMIT = 10

# channel -> (event_type, tool)
CHANNEL_META = {
    "mcp": ("mcp_tool", "execute_sql"),
    "mcp_export": ("mcp_tool", "issue_export_credential"),
    "mysql_export": ("mysql_export", "mysql_query"),
}

EngineProvider = Callable[[Any], Awaitable[Any]]


class GatewayError(BaseGatewayError):
    """Channel-agnostic orchestration failure, mapped to each channel's own error shape by the adapters.

    audited means the orchestration layer already persisted the audit (denied written or error
    updated); channel adapters must not write it again.
    """

    def __init__(
        self,
        code: str,
        message: str,
        audit_status: str = "denied",
        *,
        request_id: str | None = None,
        datasource: Any | None = None,
        audited: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.audit_status = audit_status
        self.request_id = request_id
        self.datasource = datasource
        self.audited = audited


@dataclass(frozen=True)
class PreparedRead:
    request_id: str
    datasource: Any
    database: str
    decision: Any
    exec_sql: str
    plan: Any
    started_at: float
    channel: str
    credential_account: str | None = None
    row_cap: int | None = None


def elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def candidate_label(datasource) -> str:
    label = datasource.name
    remark = getattr(datasource, "remark", None)
    if remark:
        label += f"（{remark}）"
    if not getattr(datasource, "enabled", 1):
        label += "[已停用]"
    return label


def candidate_labels(datasources) -> str:
    return "、".join(candidate_label(item) for item in datasources[:CANDIDATE_LIMIT])


DRIVER_MODULE_ROOTS = frozenset({"pymysql", "aiomysql"})


def _is_driver_error(exc: BaseException) -> bool:
    """The export channel uses aiomysql cursors directly, so driver errors bypass SQLAlchemy wrapping."""
    return type(exc).__module__.split(".", 1)[0] in DRIVER_MODULE_ROOTS


def map_exception(exc: BaseException, request_id: str) -> tuple[str, str]:
    if isinstance(exc, SQLAlchemyError):
        return sanitize_db_error(exc)
    if isinstance(exc, DataSourceConnectionError):
        return "DB_UNAVAILABLE", "数据库连接失败，详情见审计日志"
    if _is_driver_error(exc):
        return sanitize_db_error(exc)
    logger.exception("执行未预期异常 request_id=%s", request_id)
    return "INTERNAL_ERROR", f"网关内部错误，请联系管理员（{request_id}）"


def map_db_error(exc: BaseException, request_id: str = "") -> GatewayError:
    code, message = sanitize_db_error(exc)
    return GatewayError(
        code, message, audit_status="error", request_id=request_id, audited=True
    )


async def resolve_datasource(services, principal: Principal, datasource_name: str):
    """Resolve a datasource by name/description; raises GatewayError (no audit written) on failure."""
    name = (datasource_name or "").strip()
    if not name:
        raise GatewayError("INVALID_ARGUMENT", "datasource 参数不能为空")

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
            raise GatewayError(
                "DATASOURCE_AMBIGUOUS",
                f"数据源名称有歧义：{name}；候选：{candidate_labels(ambiguous)}"
                "。请改用精确名称",
            )
        if datasource is None:
            available = candidate_labels(candidates)
            message = f"数据源不存在：{name}"
            if available:
                message += f"；可用数据源：{available}"
            raise GatewayError("DATASOURCE_NOT_FOUND", message)

    if not datasource.enabled:
        raise GatewayError(
            "DATASOURCE_DISABLED",
            f"数据源已停用：{datasource.name}",
            datasource=datasource,
        )
    if not principal.scope.allows_datasource(datasource.id):
        raise GatewayError(
            "DATASOURCE_DENIED",
            f"当前 Token 无权访问数据源：{datasource.name}",
            datasource=datasource,
        )
    return datasource


async def _database_context(services, datasource) -> tuple[frozenset[str], list]:
    """Exposed databases and their policies; legacy datasources without a stored list fall back to the default database."""
    session_factory = getattr(services, "session_factory", None)
    return await databases.exposed_and_policies(session_factory, datasource)


async def database_options(
    services, datasource, scope
) -> tuple[frozenset[str], list[databases.DatabasePolicy], list[databases.DatabasePolicy]]:
    """(exposed databases, all database policies, usable databases of this token); shared by MCP metadata tools and the pipeline."""
    exposed, policies = await _database_context(services, datasource)
    available = databases.available_databases(
        sorted(exposed), policies, scope, datasource.id
    )
    return exposed, policies, available


async def prepare_read(
    *,
    services,
    principal: Principal,
    client_ip: str,
    datasource_name: str,
    sql: str | None,
    channel: str,
    force_read_only: bool = False,
    credential_account: str | None = None,
    engine_provider: EngineProvider | None = None,
    request_id: str | None = None,
    started_at: float | None = None,
    row_cap: int | None = None,
    database: str | None = None,
) -> PreparedRead:
    """Run the pre-execution orchestration of the read path; on failure the audit is already persisted and the GatewayError maps directly."""
    meta = CHANNEL_META.get(channel)
    if meta is None:
        raise ValueError(f"未知通道：{channel}")
    event_type, tool = meta
    request_id = request_id or str(uuid4())
    started = started_at if started_at is not None else time.perf_counter()

    async def deny(
        code: str,
        message: str,
        datasource=None,
        database_name: str | None = None,
    ) -> None:
        await services.audit.record_denied(
            request_id,
            event_type,
            principal,
            datasource,
            tool,
            sql,
            client_ip,
            code,
            message,
            elapsed_ms(started),
            database_name=database_name,
        )

    if not sql or not sql.strip():
        message = "SQL 不能为空"
        await deny("INVALID_ARGUMENT", message)
        raise GatewayError(
            "INVALID_ARGUMENT", message, request_id=request_id, audited=True
        )

    try:
        datasource = await resolve_datasource(services, principal, datasource_name)
    except GatewayError as exc:
        escape = GatewayError(
            exc.code,
            exc.message,
            exc.audit_status,
            request_id=request_id,
            datasource=exc.datasource,
            audited=True,
        )
        await deny(escape.code, escape.message, escape.datasource)
        raise escape from exc

    exposed, policies, available = await database_options(
        services, datasource, principal.scope
    )
    effective_database = (database or "").strip() or datasource.default_database
    available_names = [item.name for item in available]

    if (
        len(effective_database) > databases.DATABASE_LIMIT
        or "`" in effective_database
        or "\x00" in effective_database
    ):
        message = "库名不合法：长度不能超过 128，且不能包含反引号或空字符"
        await deny("INVALID_ARGUMENT", message, datasource)
        raise GatewayError(
            "INVALID_ARGUMENT",
            message,
            request_id=request_id,
            datasource=datasource,
            audited=True,
        )
    if effective_database not in exposed:
        message = f"库 {effective_database} 未在数据源 {datasource.name} 的可访问库中"
        if available_names:
            message += f"；可用库：{'、'.join(available_names)}"
        await deny(
            "DATABASE_NOT_EXPOSED",
            message,
            datasource,
            database_name=effective_database,
        )
        raise GatewayError(
            "DATABASE_NOT_EXPOSED",
            message,
            request_id=request_id,
            datasource=datasource,
            audited=True,
        )
    if effective_database not in available_names:
        message = f"当前 Token 无权访问库：{effective_database}"
        await deny(
            "DATABASE_DENIED",
            message,
            datasource,
            database_name=effective_database,
        )
        raise GatewayError(
            "DATABASE_DENIED",
            message,
            request_id=request_id,
            datasource=datasource,
            audited=True,
        )

    read_only_for = databases.effective_read_only(policies)
    if force_read_only:
        read_only_for = lambda _database: True  # noqa: E731

    decision = policy.evaluate(
        sql,
        default_database=effective_database,
        read_only_for=read_only_for,
    )
    if not decision.allowed:
        message = decision.message
        denied_database = (
            decision.database
            if decision.code == policy.WRITE_ON_READONLY and decision.database
            else effective_database
        )
        await deny(decision.code, message, datasource, database_name=denied_database)
        raise GatewayError(
            decision.code,
            message,
            request_id=request_id,
            datasource=datasource,
            audited=True,
        )

    try:
        await services.audit.start(
            request_id,
            event_type,
            principal,
            datasource,
            tool,
            sql,
            client_ip,
            credential_account=credential_account,
            database_name=effective_database,
        )
    except AuditUnavailableError as exc:
        raise GatewayError(
            "AUDIT_UNAVAILABLE",
            "审计服务不可用，查询已阻断",
            audit_status="error",
            request_id=request_id,
            datasource=datasource,
            audited=True,
        ) from exc

    prepared = PreparedRead(
        request_id=request_id,
        datasource=datasource,
        database=effective_database,
        decision=decision,
        exec_sql=sql,
        plan=None,
        started_at=started,
        channel=channel,
        credential_account=credential_account,
    )

    provider = engine_provider or services.pool.get
    try:
        engine = await provider(datasource)
    except Exception as exc:  # noqa: BLE001 - treat connection failures as database errors
        code, message = map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), elapsed_ms(started)
        )
        raise GatewayError(
            code,
            message,
            audit_status="error",
            request_id=request_id,
            datasource=datasource,
            audited=True,
        ) from exc

    try:
        rewritten = await scope_policy.enforce(
            decision.ast,
            principal.scope,
            datasource.id,
            effective_database,
            engine,
            exposed_databases=exposed,
            datasource_name=datasource.name,
        )
    except scope_policy.ScopeViolation as exc:
        await conclude_denied(services, prepared, exc.code, exc.message)
        raise GatewayError(
            exc.code,
            exc.message,
            request_id=request_id,
            datasource=datasource,
            audited=True,
        ) from exc
    exec_sql = (
        decision.ast.sql(dialect="mysql")
        if rewritten and decision.ast is not None
        else sql
    )

    plan_ast = decision.ast
    capped = False
    if row_cap and not decision.is_write and isinstance(plan_ast, (exp.Select, exp.Union)):
        has_limit, existing = _existing_limit(plan_ast)
        if not has_limit or (existing is not None and existing > row_cap):
            # Fetch one extra row to detect truncation; downstream trims it.
            plan_ast = plan_ast.limit(row_cap + 1)
            capped = True
        if capped:
            exec_sql = plan_ast.sql(dialect="mysql")

    try:
        plan = masking.build_plan(
            plan_ast,
            datasource.id,
            effective_database,
            await services.masking.list_rules(datasource.id),
        )
    except Exception as exc:  # noqa: BLE001 - a masking-plan failure must block execution
        code, message = map_exception(exc, request_id)
        await services.audit.mark_error_best_effort(
            request_id, code, error_detail(exc), elapsed_ms(started)
        )
        raise GatewayError(
            code,
            message,
            audit_status="error",
            request_id=request_id,
            datasource=datasource,
            audited=True,
        ) from exc

    return replace(prepared, exec_sql=exec_sql, plan=plan, row_cap=row_cap or None)


def _existing_limit(ast) -> tuple[bool, int | None]:
    """Return (whether the statement already has LIMIT, its integer value if parseable).

    A non-integer literal (e.g. `LIMIT ?`) yields None so the caller keeps the user's SQL as-is.
    """
    limit = ast.args.get("limit")
    if limit is None:
        return False, None
    expression = limit.expression
    if isinstance(expression, exp.Literal) and expression.is_int:
        return True, int(expression.this)
    return True, None


async def suggest_databases(
    services,
    principal: Principal,
    datasource,
    table_name: str,
    effective_database: str,
) -> list[str]:
    """1146 candidate databases: look for same-named tables in the other usable databases only; the current database wins, so it is not suggested."""
    if not table_name:
        return []
    try:
        _exposed, _policies, available = await database_options(
            services, datasource, principal.scope
        )
        candidates = [
            item.name for item in available if item.name != effective_database
        ]
        if not candidates:
            return []
        engine = await services.pool.get(datasource)
        placeholders = ", ".join(["%s"] * len(candidates))
        query = (
            "SELECT DISTINCT table_schema FROM information_schema.tables "
            f"WHERE table_name = %s AND table_schema IN ({placeholders})"
        )
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql(query, (table_name, *candidates))
            found = {row[0] for row in result.fetchall()}
        return [name for name in candidates if name in found]
    except Exception:  # noqa: BLE001 - hint degradation must not change the original error
        logger.warning(
            "读取候选库失败 datasource=%s table=%s",
            getattr(datasource, "name", None),
            table_name,
            exc_info=True,
        )
        return []


async def conclude_success(services, prepared: PreparedRead, row_count: int) -> int:
    duration = elapsed_ms(prepared.started_at)
    try:
        await services.audit.mark_success(prepared.request_id, duration, row_count)
    except AuditUnavailableError as exc:
        await services.audit.mark_error_best_effort(
            prepared.request_id, "AUDIT_UNAVAILABLE", "审计结果更新失败", duration
        )
        raise GatewayError(
            "AUDIT_UNAVAILABLE",
            "审计服务不可用，查询已阻断",
            audit_status="error",
            request_id=prepared.request_id,
            datasource=prepared.datasource,
            audited=True,
        ) from exc
    return duration


async def conclude_denied(
    services, prepared: PreparedRead, code: str, message: str
) -> int:
    duration = elapsed_ms(prepared.started_at)
    await services.audit.mark_denied_best_effort(
        prepared.request_id, code, message, duration
    )
    return duration


async def conclude_error(
    services, prepared: PreparedRead, exc: BaseException, code: str | None = None
) -> int:
    duration = elapsed_ms(prepared.started_at)
    if code is None:
        code, _ = map_exception(exc, prepared.request_id)
    await services.audit.mark_error_best_effort(
        prepared.request_id, code, error_detail(exc), duration
    )
    return duration


async def conclude_interrupted(
    services, prepared: PreparedRead, row_count: int, reason: str
) -> int:
    duration = elapsed_ms(prepared.started_at)
    try:
        await services.audit.mark_interrupted(
            prepared.request_id, duration, row_count, reason
        )
    except Exception:  # noqa: BLE001 - best effort, the client may already be gone
        logger.exception(
            "审计 interrupted 更新失败（best effort）request_id=%s", prepared.request_id
        )
    return duration
