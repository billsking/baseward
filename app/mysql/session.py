"""SQL entrypoint and metadata exit of the export channel (mysql-mimic Session implementation).

The only metadata exit is `schema()`: SHOW / DESCRIBE / INFORMATION_SCHEMA / COM_FIELD_LIST all
bypass `query()`, so the policy engine cannot see them (D22 / AC23).
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn
from uuid import uuid4

from mysql_mimic import ResultSet, Session
from mysql_mimic.errors import ErrorCode, MysqlError
from mysql_mimic.results import AllowedResult
from mysql_mimic.schema import Column, InfoSchema
from sqlglot.errors import ParseError

from app.credentials.service import CredentialAuthError
from app.datasources.databases import exclude_system_schemas
from app.mysql import streaming
from app.mysql.errors import format_message
from app.mysql.identity import (
    ExportConnectionContext,
    ExportServerContext,
    audit_handshake_failure,
    client_ip_of,
)
from app.query import pipeline as query_pipeline
from app.query.pipeline import GatewayError
from app.sanitize import error_detail

logger = logging.getLogger(__name__)

DATABASE_NOT_EXPOSED = "DATABASE_NOT_EXPOSED"
DATABASE_DENIED = "DATABASE_DENIED"
EXPORT_BUSY = "EXPORT_BUSY"
EXPORT_CONCURRENCY_LIMIT = "EXPORT_CONCURRENCY_LIMIT"

_ERRNO_BY_CODE = {
    "EMPTY_SQL": ErrorCode.EMPTY_QUERY,
    "SQL_PARSE_ERROR": ErrorCode.PARSE_ERROR,
    "MULTI_STATEMENT": ErrorCode.PARSE_ERROR,
    "NO_DATABASE_SELECTED": ErrorCode.NO_DB_ERROR,
    EXPORT_CONCURRENCY_LIMIT: ErrorCode.CON_COUNT_ERROR,
    "EXPORT_ROW_LIMIT": ErrorCode.UNKNOWN_ERROR,
    "EXPORT_TIME_LIMIT": ErrorCode.UNKNOWN_ERROR,
    "AUDIT_UNAVAILABLE": ErrorCode.UNKNOWN_ERROR,
    "DB_UNAVAILABLE": ErrorCode.UNKNOWN_ERROR,
    "DB_ERROR": ErrorCode.UNKNOWN_ERROR,
    "INTERNAL_ERROR": ErrorCode.UNKNOWN_ERROR,
}


class ExportSession(Session):
    holding_quota = False

    def __init__(
        self,
        services: Any,
        settings: Any,
        context: ExportServerContext,
        limiter: Any,
    ) -> None:
        super().__init__()
        self._services = services
        self._settings = settings
        self._context = context
        self._limiter = limiter
        self._ctx: ExportConnectionContext | None = None
        self._peer_ip = ""
        self._connection_id: int | None = None
        self._schema_cache: dict[tuple[int, str | None], InfoSchema] = {}

    # ---------- Lifecycle ----------

    async def init(self, connection: Any) -> None:
        await super().init(connection)
        # The source IP comes from this connection's TCP peer, not from any state shared by username.
        self._peer_ip = client_ip_of(connection)
        self._connection_id = getattr(connection, "connection_id", None)
        if self.username is None:
            # The library still calls init() when authentication fails (D-06): this is where a bad password is observed.
            attempted = str(self.variables.get("external_user") or "")
            await audit_handshake_failure(
                self._services,
                self._context,
                self._connection_id,
                attempted,
                self._peer_ip,
            )
            return
        ctx = self._context.get(self._connection_id)
        if ctx is None:
            logger.warning("channel=export 导出会话缺少握手上下文 account=%s", self.username)
            return
        self._ctx = ctx
        # The library first writes the database from the connection string into database; overwrite it with the credential target database (D67).
        self.database = ctx.credential.default_database

    async def close(self) -> None:
        self.release_quota()
        if self._connection_id is not None:
            self._context.clear(self._connection_id)
        await super().close()

    # ---------- Quota ----------

    def acquire_quota(self) -> bool:
        if not self._limiter.try_acquire():
            return False
        self.holding_quota = True
        return True

    def release_quota(self) -> None:
        if self.holding_quota:
            self.holding_quota = False
            self._limiter.release()

    # ---------- USE ----------

    async def use(self, database: str) -> None:
        ctx = self._ctx
        if ctx is None:
            raise self._access_denied("CREDENTIAL_NOT_FOUND", str(uuid4()))
        request_id = str(uuid4())
        started = time.perf_counter()
        exposed, _policies, available = await query_pipeline.database_options(
            self._services, ctx.datasource, ctx.principal.scope
        )
        # The credential itself is not exempt: the target database may have been removed from the exposed list after issue (E34/AC44).
        if database not in exposed:
            available_names = [item.name for item in available]
            message = (
                f"库 {database} 未在数据源 {ctx.datasource.name} 的可访问库中；"
                f"可用库：{', '.join(available_names) or '无'}"
            )
            await self._audit_denied(
                request_id,
                DATABASE_NOT_EXPOSED,
                message,
                None,
                started,
                database_name=database,
            )
            raise MysqlError(
                format_message(DATABASE_NOT_EXPOSED, message, request_id),
                ErrorCode.NOT_SUPPORTED_YET,
            )
        if database not in {item.name for item in available}:
            message = f"当前 Token 无权访问库：{database}"
            await self._audit_denied(
                request_id,
                DATABASE_DENIED,
                message,
                None,
                started,
                database_name=database,
            )
            raise MysqlError(
                format_message(DATABASE_DENIED, message, request_id),
                ErrorCode.NOT_SUPPORTED_YET,
            )
        self.database = database

    # ---------- SQL ----------

    async def handle_query(self, sql: str, attrs: dict[str, str]) -> AllowedResult:
        try:
            expressions = self._parse(sql)
        except ParseError as exc:
            request_id = str(uuid4())
            await self._audit_denied(
                request_id, "SQL_PARSE_ERROR", "SQL 无法解析", sql, time.perf_counter()
            )
            raise MysqlError(
                format_message("SQL_PARSE_ERROR", "SQL 无法解析", request_id),
                ErrorCode.PARSE_ERROR,
            ) from exc
        if len(expressions) > 1:
            # Must intercept before the middleware chain: neither statement of `SELECT 1; SELECT 2` reaches query().
            request_id = str(uuid4())
            await self._audit_denied(
                request_id, "MULTI_STATEMENT", "导出通道不支持多语句", sql, time.perf_counter()
            )
            raise MysqlError(
                format_message("MULTI_STATEMENT", "导出通道不支持多语句", request_id),
                ErrorCode.PARSE_ERROR,
            )
        return await super().handle_query(sql, attrs)

    async def query(
        self, expression: Any, sql: str, attrs: dict[str, str]
    ) -> AllowedResult:
        request_id = str(uuid4())
        started = time.perf_counter()
        ctx = await self._refresh_identity(request_id, sql, started)
        try:
            prepared = await query_pipeline.prepare_read(
                services=self._services,
                principal=ctx.principal,
                client_ip=self._client_ip,
                datasource_name=ctx.datasource.name,
                sql=sql,
                database=self.database,
                channel="mysql_export",
                force_read_only=True,
                engine_provider=self._services.export_pool.scope_engine,
                request_id=request_id,
                started_at=started,
                credential_account=ctx.credential.account_name,
            )
        except GatewayError as exc:
            await self._raise_gateway(exc, request_id, sql, started)

        if not self.acquire_quota():
            message = "导出通道并发已满，请稍后重试"
            await self._audit_denied(request_id, EXPORT_BUSY, message, sql, started)
            raise MysqlError(
                format_message(EXPORT_CONCURRENCY_LIMIT, message, request_id),
                ErrorCode.CON_COUNT_ERROR,
            )

        cursor_cm = None
        entered = False
        try:
            cursor_cm = self._services.export_pool.stream_cursor(
                prepared.datasource, prepared.exec_sql, prepared.database
            )
            cursor = await cursor_cm.__aenter__()
            entered = True
            names = [str(item[0]) for item in cursor.description or ()]
            columns = streaming.build_result_columns(
                cursor.description, streaming.string_masked_columns(prepared.plan, names)
            )
        except Exception as exc:  # noqa: BLE001 - mapped to 1105
            self.release_quota()
            if entered:
                await streaming.close_cursor(cursor_cm)
            code, message = query_pipeline.map_exception(exc, request_id)
            await self._services.audit.mark_error_best_effort(
                request_id, code, error_detail(exc), query_pipeline.elapsed_ms(started)
            )
            raise MysqlError(
                format_message(code, message, request_id), _ERRNO_BY_CODE.get(code, ErrorCode.UNKNOWN_ERROR)
            ) from exc

        return ResultSet(
            rows=streaming.stream_rows(
                services=self._services,
                settings=self._settings,
                prepared=prepared,
                cursor=cursor,
                cursor_cm=cursor_cm,
                connection=self.connection,
                plan=prepared.plan,
                session=self,
            ),
            columns=columns,
        )

    # ---------- Metadata (the only exit per AC23) ----------

    async def schema(self) -> InfoSchema:
        ctx = self._ctx
        if ctx is None:
            return InfoSchema.from_columns([])
        # USE switches the database and must reload the schema; the cache key includes the database name (AC43).
        key = (ctx.datasource.id, self.database)
        cached = self._schema_cache.get(key)
        if cached is None:
            cached = await self._load_schema(ctx)
            self._schema_cache[key] = cached
        return cached

    async def _load_schema(self, ctx: ExportConnectionContext) -> InfoSchema:
        try:
            rows = await self._fetch_metadata(ctx)
        except Exception:  # noqa: BLE001 - prefer an empty set when the authorization state is unknown
            logger.warning(
                "channel=export 导出通道读取元数据失败 datasource=%s",
                getattr(ctx.datasource, "id", None),
                exc_info=True,
            )
            return InfoSchema.from_columns([])
        scope = ctx.principal.scope
        columns: list[Column] = []
        for schema_name, table_name, column_name, data_type in rows:
            allowed = scope.allowed_columns(ctx.datasource.id, schema_name, table_name)
            if allowed is not None and column_name.lower() not in allowed:
                continue
            columns.append(
                Column(
                    name=column_name,
                    type=data_type,
                    table=table_name,
                    schema=schema_name,
                )
            )
        return InfoSchema.from_columns(columns)

    async def _fetch_metadata(self, ctx: ExportConnectionContext) -> list[tuple]:
        scope = ctx.principal.scope
        database = self.database
        if not database:
            return []
        params: list[Any] = [database]
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns "
            f"WHERE {exclude_system_schemas('table_schema')} "
            "AND table_schema = %s"
        )
        table_names = scope.table_names(ctx.datasource.id, database)
        if table_names is not None:
            if not table_names:
                return []
            tables = sorted(table_names)
            placeholders = ",".join(["(%s,%s)"] * len(tables))
            sql += f" AND (table_schema, table_name) IN ({placeholders})"
            for table in tables:
                params.extend([database, table])
        sql += " ORDER BY table_schema, table_name, ordinal_position"

        pool = await self._services.export_pool.get(ctx.datasource)
        conn = await pool.acquire()
        try:
            cursor = await conn.cursor()
            try:
                await cursor.execute(sql, tuple(params))
                return list(await cursor.fetchall())
            finally:
                await cursor.close()
        finally:
            pool.release(conn)

    # ---------- Internal helpers ----------

    @property
    def _client_ip(self) -> str:
        if self._peer_ip:
            return self._peer_ip
        return self._ctx.client_ip if self._ctx is not None else ""

    async def _refresh_identity(
        self, request_id: str, sql: str, started: float
    ) -> ExportConnectionContext:
        """Takes effect immediately on the same connection: after revoke/expiry/token disable the next statement is rejected (F6/F11)."""
        credential, deny_code = await self._services.credentials.get_for_auth(
            self.username or ""
        )
        if credential is None or deny_code is not None:
            code = deny_code or "CREDENTIAL_NOT_FOUND"
            await self._audit_denied(request_id, code, "认证已失效", sql, started)
            raise self._access_denied(code, request_id)
        try:
            principal, datasource = await self._services.credentials.resolve_principal(
                credential
            )
        except CredentialAuthError as exc:
            await self._audit_denied(request_id, exc.code, "认证已失效", sql, started)
            raise self._access_denied(exc.code, request_id) from exc
        ctx = ExportConnectionContext(
            credential=credential,
            principal=principal,
            datasource=datasource,
            client_ip=self._client_ip,
        )
        self._ctx = ctx
        return ctx

    def _access_denied(self, code: str, request_id: str) -> MysqlError:
        return MysqlError(
            format_message(code, "认证已失效，请重新获取凭证", request_id),
            ErrorCode.ACCESS_DENIED_ERROR,
        )

    async def _raise_gateway(
        self, exc: GatewayError, request_id: str, sql: str, started: float
    ) -> NoReturn:
        request_id = exc.request_id or request_id
        errno = _ERRNO_BY_CODE.get(exc.code, ErrorCode.NOT_SUPPORTED_YET)
        if not exc.audited:
            if exc.audit_status == "denied":
                await self._audit_denied(request_id, exc.code, exc.message, sql, started)
            else:
                await self._services.audit.mark_error_best_effort(
                    request_id,
                    exc.code,
                    exc.message,
                    query_pipeline.elapsed_ms(started),
                )
        raise MysqlError(
            format_message(exc.code, exc.message, request_id), errno
        ) from exc

    async def _audit_denied(
        self,
        request_id: str,
        code: str,
        message: str,
        sql: str | None,
        started: float,
        database_name: str | None = None,
    ) -> None:
        ctx = self._ctx
        await self._services.audit.record_denied(
            request_id,
            "mysql_export",
            ctx.principal if ctx else None,
            ctx.datasource if ctx else None,
            "mysql_query",
            sql,
            self._client_ip,
            code,
            message,
            query_pipeline.elapsed_ms(started),
            credential_account=ctx.credential.account_name if ctx else None,
            database_name=database_name or self.database,
        )
