"""Execution-pipeline orchestration unit tests (fake services, no MySQL)."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.datasources import databases as databases_module
from app.datasources.databases import DatabasePolicy
from app.errors import AuditUnavailableError
from app.mcp import pipeline as mcp_pipeline
from app.mcp.pipeline import execute_sql
from app.query import pipeline as query_pipeline
from app.query.pipeline import prepare_read, suggest_databases
from app.security.principal import Principal
from app.tokens.scope import TokenScope

PRINCIPAL = Principal(user_name="alice", token_id=1, token_prefix="gw_1234567")


def make_datasource(**overrides):
    data = {
        "id": 1,
        "name": "demo",
        "host": "127.0.0.1",
        "port": 3306,
        "username": "reader",
        "password_cipher": "x",
        "default_database": "demo",
        "is_read_only": 1,
        "enabled": 1,
        "remark": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeAudit:
    def __init__(self, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.denied: list[tuple] = []
        self.start_calls: list[tuple] = []
        self.started = 0

    async def record_denied(self, *args, **kwargs) -> None:
        self.denied.append((args, kwargs))

    async def start(self, *args, **kwargs) -> None:
        self.started += 1
        self.start_calls.append((args, kwargs))
        if self.fail_start:
            raise AuditUnavailableError("down")

    async def mark_success(self, *args, **kwargs) -> None:
        raise AssertionError("mark_success 不应被调用")

    async def mark_error_best_effort(self, *args, **kwargs) -> None:
        raise AssertionError("mark_error_best_effort 不应被调用")


class FakeDatasources:
    def __init__(self, *datasources) -> None:
        self.datasources = list(datasources)

    async def get_by_name(self, name: str):
        return next((item for item in self.datasources if item.name == name), None)

    async def list_all(self):
        return list(self.datasources)


class FakePool:
    def __init__(self) -> None:
        self.called = False

    async def get(self, datasource):
        self.called = True
        raise AssertionError("不应触碰业务库")


def make_services(datasource=None, fail_start: bool = False):
    return make_services_multi([datasource or make_datasource()], fail_start)


def make_services_multi(datasources, fail_start: bool = False):
    audit = FakeAudit(fail_start=fail_start)
    pool = FakePool()
    services = SimpleNamespace(
        audit=audit,
        datasources=FakeDatasources(*datasources),
        pool=pool,
        masking=SimpleNamespace(),
        mask_key=b"k" * 32,
        settings=SimpleNamespace(
            mcp_result_max_rows=0,
            mcp_result_max_bytes=0,
            mcp_export_auto_issue=False,
            mysql_export_enabled=False,
        ),
    )
    return services, audit, pool


def error_code(exc: ToolError) -> str:
    import json

    return json.loads(str(exc))["error"]["code"]


@pytest.mark.asyncio
async def test_audit_start_failure_blocks_before_execution() -> None:
    services, audit, pool = make_services(fail_start=True)
    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "demo", "SELECT 1")
    assert error_code(exc_info.value) == "AUDIT_UNAVAILABLE"
    assert audit.started == 1
    assert pool.called is False


@pytest.mark.asyncio
async def test_ddl_is_denied_and_audited_without_execution() -> None:
    services, audit, pool = make_services()
    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "demo", "DROP TABLE users")
    assert error_code(exc_info.value) == "DDL_BLOCKED"
    assert len(audit.denied) == 1
    assert pool.called is False


@pytest.mark.asyncio
async def test_write_on_readonly_is_denied() -> None:
    services, audit, pool = make_services()
    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "demo", "UPDATE users SET name='x' WHERE id=1")
    assert error_code(exc_info.value) == "WRITE_ON_READONLY"
    assert pool.called is False


@pytest.mark.asyncio
async def test_empty_sql_and_unknown_datasource_are_denied() -> None:
    services, audit, pool = make_services()
    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "demo", "   ")
    assert error_code(exc_info.value) == "INVALID_ARGUMENT"
    assert pool.called is False

    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "missing", "SELECT 1")
    assert error_code(exc_info.value) == "DATASOURCE_NOT_FOUND"
    assert pool.called is False


@pytest.mark.asyncio
async def test_ambiguous_alias_is_denied_before_connect() -> None:
    first = make_datasource(id=1, name="demo", remark="示例库")
    second = make_datasource(id=2, name="demo-bak", remark="示例库备份")
    services, audit, pool = make_services_multi([first, second])

    with pytest.raises(ToolError) as exc_info:
        await execute_sql(services, PRINCIPAL, "127.0.0.1", "示例", "SELECT 1")
    assert error_code(exc_info.value) == "DATASOURCE_AMBIGUOUS"
    assert pool.called is False
    assert audit.denied[-1][0][7] == "DATASOURCE_AMBIGUOUS"


# ---------- v1.8: effective default database propagation (T44 / AC39 / AC40 / AC47 / D66 / D72) ----------


class FakeMasking:
    async def list_rules(self, datasource_id: int):
        return []


def capture_plan(monkeypatch) -> list[tuple[int, str]]:
    built: list[tuple[int, str]] = []

    def _build(ast, datasource_id: int, database: str, rules):
        built.append((datasource_id, database))
        return SimpleNamespace()

    monkeypatch.setattr(query_pipeline.masking, "build_plan", _build)
    return built


async def _unused_engine(datasource):
    return SimpleNamespace()


def make_database_services(datasource=None, *, pool=None):
    datasource = datasource or make_datasource()
    audit = FakeAudit()
    masking = FakeMasking()
    services = SimpleNamespace(
        audit=audit,
        datasources=FakeDatasources(datasource),
        pool=pool if pool is not None else SimpleNamespace(get=_unused_engine),
        masking=masking,
        session_factory=object(),
        mask_key=b"k" * 32,
    )
    return services, audit, masking


def install_db_config(monkeypatch, exposed, policies=None):
    calls = {"list_exposed": 0}

    async def _list_exposed(session_factory, datasource_id):
        calls["list_exposed"] += 1
        return list(exposed)

    async def _load_policies(session_factory, datasource_id):
        if policies is not None:
            return list(policies)
        return [DatabasePolicy(name, False) for name in exposed]

    monkeypatch.setattr(databases_module, "list_exposed", _list_exposed)
    monkeypatch.setattr(databases_module, "load_policies", _load_policies)
    return calls


async def _prepare(services, *, database=None, sql="SELECT id FROM users", **kwargs):
    return await prepare_read(
        services=services,
        principal=kwargs.pop("principal", PRINCIPAL),
        client_ip="127.0.0.1",
        datasource_name="demo",
        sql=sql,
        channel="mcp",
        database=database,
        engine_provider=_unused_engine,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_default_database_and_masking_receive_effective_database(
    monkeypatch,
) -> None:
    install_db_config(monkeypatch, ["demo", "report"])
    built = capture_plan(monkeypatch)
    services, audit, _ = make_database_services()

    prepared = await _prepare(services)
    assert prepared.database == "demo"
    assert built[-1] == (1, "demo")
    assert audit.start_calls[-1][1]["database_name"] == "demo"

    blank = await _prepare(services, database="   ")
    assert blank.database == "demo"

    explicit = await _prepare(services, database="report")
    assert explicit.database == "report"
    assert built[-1] == (1, "report")
    assert audit.start_calls[-1][1]["database_name"] == "report"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["a" * 129, "bad`name", "bad\x00name"])
async def test_invalid_database_name_is_denied_before_audit_intent(
    monkeypatch, bad
) -> None:
    install_db_config(monkeypatch, ["demo"])
    services, audit, _ = make_database_services()

    with pytest.raises(query_pipeline.GatewayError) as exc_info:
        await _prepare(services, database=bad)
    assert exc_info.value.code == "INVALID_ARGUMENT"
    assert audit.started == 0


@pytest.mark.asyncio
async def test_unexposed_database_is_denied_with_attempted_database(
    monkeypatch,
) -> None:
    install_db_config(monkeypatch, ["demo"])
    built = capture_plan(monkeypatch)
    services, audit, _ = make_database_services()

    with pytest.raises(query_pipeline.GatewayError) as exc_info:
        await _prepare(services, database="ods")
    assert exc_info.value.code == "DATABASE_NOT_EXPOSED"
    assert "ods" in exc_info.value.message
    assert "demo" in exc_info.value.message
    assert audit.started == 0
    assert audit.denied[-1][1]["database_name"] == "ods"
    assert built == []


@pytest.mark.asyncio
async def test_exposed_but_unauthorized_database_is_denied(monkeypatch) -> None:
    install_db_config(monkeypatch, ["main", "report"])
    services, audit, _ = make_database_services(
        datasource=make_datasource(default_database="main")
    )
    scope = TokenScope(
        datasources=frozenset({1}),
        unrestricted_databases=frozenset({(1, "main")}),
    )
    principal = Principal(
        user_name="alice", token_id=1, token_prefix="gw_1234567", scope=scope
    )

    with pytest.raises(query_pipeline.GatewayError) as exc_info:
        await _prepare(services, database="report", principal=principal)
    assert exc_info.value.code == "DATABASE_DENIED"
    assert "report" in exc_info.value.message
    assert audit.started == 0
    assert audit.denied[-1][1]["database_name"] == "report"


@pytest.mark.asyncio
async def test_visibility_is_checked_even_for_unrestricted_token(monkeypatch) -> None:
    calls = install_db_config(monkeypatch, ["demo"])
    services, audit, _ = make_database_services()

    await _prepare(services)
    assert calls["list_exposed"] == 1
    assert audit.started == 1


@pytest.mark.asyncio
async def test_force_read_only_rejects_writable_library_policy(monkeypatch) -> None:
    install_db_config(
        monkeypatch,
        ["main"],
        policies=[DatabasePolicy("main", False)],
    )
    services, audit, _ = make_database_services(
        datasource=make_datasource(default_database="main", is_read_only=0)
    )

    with pytest.raises(query_pipeline.GatewayError) as exc_info:
        await _prepare(
            services,
            sql="UPDATE users SET name='x' WHERE id=1",
            force_read_only=True,
        )
    assert exc_info.value.code == "WRITE_ON_READONLY"
    assert audit.started == 0
    assert audit.denied[-1][1]["database_name"] == "main"


class _FakeCursorResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, rows, calls) -> None:
        self._rows = rows
        self._calls = calls

    async def exec_driver_sql(self, sql, params=None):
        self._calls.append((sql, params))
        return _FakeCursorResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeMetadataEngine:
    def __init__(self, rows=(), error: Exception | None = None) -> None:
        self._rows = rows
        self._error = error
        self.calls: list[tuple] = []

    def connect(self):
        if self._error is not None:
            raise self._error
        return _FakeConnection(self._rows, self.calls)


def metadata_services(engine: _FakeMetadataEngine):
    async def _get(datasource):
        return engine

    datasource = make_datasource(default_database="main")
    services = SimpleNamespace(
        datasources=FakeDatasources(datasource),
        pool=SimpleNamespace(get=_get),
        session_factory=object(),
    )
    return services, datasource


@pytest.mark.asyncio
async def test_suggest_databases_lists_other_authorized_libraries(monkeypatch) -> None:
    install_db_config(monkeypatch, ["main", "report"])
    engine = _FakeMetadataEngine(rows=[("report",), ("main",)])
    services, datasource = metadata_services(engine)

    candidates = await suggest_databases(
        services, PRINCIPAL, datasource, "orders", "main"
    )
    assert candidates == ["report"]
    sql, params = engine.calls[0]
    assert "main" not in params
    assert params == ("orders", "report")


@pytest.mark.asyncio
async def test_suggest_databases_skips_current_library_hit(monkeypatch) -> None:
    install_db_config(monkeypatch, ["main", "report"])
    engine = _FakeMetadataEngine(rows=[("main",)])
    services, datasource = metadata_services(engine)

    assert await suggest_databases(
        services, PRINCIPAL, datasource, "orders", "main"
    ) == []


@pytest.mark.asyncio
async def test_suggest_databases_degrades_to_empty_on_metadata_failure(
    monkeypatch,
) -> None:
    install_db_config(monkeypatch, ["main", "report"])
    engine = _FakeMetadataEngine(error=RuntimeError("down"))
    services, datasource = metadata_services(engine)

    assert await suggest_databases(
        services, PRINCIPAL, datasource, "orders", "main"
    ) == []


# ---------- MCP execution guardrails (query timeout and write row cap) ----------


class _ExecResult:
    def __init__(self, rowcount: int = 0, scalar=None) -> None:
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar(self):
        return self._scalar


class _ExecConnection:
    def __init__(self, *, rowcount: int = 0, delay: float = 0.0) -> None:
        self.rowcount = rowcount
        self.delay = delay
        self.sql: list[str] = []
        self.exited: list[type[BaseException] | None] = []
        self.invalidated = False

    async def exec_driver_sql(self, sql, params=None):
        self.sql.append(sql)
        upper = sql.strip().upper()
        if upper.startswith("SELECT CONNECTION_ID"):
            return _ExecResult(scalar=42)
        if self.delay and upper.startswith(("UPDATE", "DELETE", "SELECT *")):
            await asyncio.sleep(self.delay)
        return _ExecResult(rowcount=self.rowcount)

    async def invalidate(self) -> None:
        self.invalidated = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited.append(exc_type)
        return False


class _ExecEngine:
    def __init__(self, conn: _ExecConnection) -> None:
        self.conn = conn

    def begin(self):
        return self.conn

    def connect(self):
        return self.conn


class _ExecAudit:
    def __init__(self) -> None:
        self.success: list[tuple] = []
        self.errors: list[tuple] = []

    async def mark_success(self, *args) -> None:
        self.success.append(args)

    async def mark_error_best_effort(self, *args) -> None:
        self.errors.append(args)


def exec_services(engine: _ExecEngine, *, timeout: float = 30.0, limit: int = 1000):
    async def _get(datasource):
        return engine

    audit = _ExecAudit()
    services = SimpleNamespace(
        pool=SimpleNamespace(get=_get),
        audit=audit,
        settings=SimpleNamespace(
            mcp_query_timeout_seconds=timeout,
            mcp_write_max_affected_rows=limit,
        ),
    )
    return services, audit


def prepared_write() -> SimpleNamespace:
    return SimpleNamespace(
        request_id="req-write",
        datasource=make_datasource(is_read_only=0),
        database="demo",
        exec_sql="UPDATE users SET name='x' WHERE id=1",
        decision=SimpleNamespace(statement_type="Update", is_write=True),
    )


@pytest.mark.asyncio
async def test_write_above_row_limit_is_rolled_back() -> None:
    conn = _ExecConnection(rowcount=1001)
    engine = _ExecEngine(conn)
    services, audit = exec_services(engine, limit=1000)

    with pytest.raises(ToolError) as exc_info:
        await mcp_pipeline._execute_write(services, prepared_write())

    assert error_code(exc_info.value) == "WRITE_LIMIT_EXCEEDED"
    assert audit.success == []
    assert audit.errors[-1][1] == "WRITE_LIMIT_EXCEEDED"
    assert conn.exited[-1] is mcp_pipeline._WriteLimitExceeded


@pytest.mark.asyncio
async def test_write_within_row_limit_succeeds() -> None:
    conn = _ExecConnection(rowcount=10)
    engine = _ExecEngine(conn)
    services, audit = exec_services(engine, limit=1000)

    result = await mcp_pipeline._execute_write(services, prepared_write())

    assert result["row_count"] == 10
    assert audit.success and audit.errors == []
    assert conn.exited[-1] is None


@pytest.mark.asyncio
async def test_write_timeout_kills_query_and_discards_connection() -> None:
    conn = _ExecConnection(rowcount=1, delay=0.2)
    engine = _ExecEngine(conn)
    services, audit = exec_services(engine, timeout=0.01)

    with pytest.raises(ToolError) as exc_info:
        await mcp_pipeline._execute_write(services, prepared_write())

    assert error_code(exc_info.value) == "QUERY_TIMEOUT"
    assert audit.success == []
    assert conn.invalidated is True
    assert "KILL QUERY 42" in conn.sql


@pytest.mark.asyncio
async def test_read_timeout_kills_query_and_discards_connection(monkeypatch) -> None:
    concluded: list[str] = []

    async def _conclude_error(services, prepared, exc, code: str) -> None:
        concluded.append(code)

    monkeypatch.setattr(query_pipeline, "conclude_error", _conclude_error)
    conn = _ExecConnection(delay=0.2)
    engine = _ExecEngine(conn)
    services, _ = exec_services(engine, timeout=0.01)
    prepared = SimpleNamespace(
        request_id="req-read",
        datasource=make_datasource(is_read_only=1),
        database="demo",
        exec_sql="SELECT * FROM users",
        row_cap=None,
        plan=SimpleNamespace(),
        decision=SimpleNamespace(statement_type="Select", is_write=False),
    )

    with pytest.raises(ToolError) as exc_info:
        await mcp_pipeline._execute_read(services, PRINCIPAL, "127.0.0.1", prepared, "SELECT * FROM users")

    assert error_code(exc_info.value) == "QUERY_TIMEOUT"
    assert concluded == ["QUERY_TIMEOUT"]
    assert conn.invalidated is True
    assert "KILL QUERY 42" in conn.sql
