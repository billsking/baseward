"""Unit tests for MCP large-result handoff (fake services, no MySQL).

Covers v1.5 row-cap pushdown (AC26/AC30), row/byte truncation (AC26), the degradation matrix (AC27), and that sensitive values never leak.
"""

import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.config import Settings
from app.credentials.service import AutoIssuedCredential
from app.errors import ValidationError
from app.mcp.pipeline import (
    EXPORT_UNAVAILABLE,
    HANDOFF_HINT_ISSUED,
    HANDOFF_NOTICE,
    _truncate_by_bytes,
    execute_sql,
    issue_export_credential,
)
from app.query.pipeline import prepare_read
from app.security.principal import Principal

PRINCIPAL = Principal(user_name="alice", token_id=1, token_prefix="gw_1234567")
VALID_KEY = base64.b64encode(b"k" * 32).decode("ascii")


def make_settings(**overrides) -> Settings:
    data = {
        "master_key": VALID_KEY,
        "db_host": "127.0.0.1",
        "db_user": "gateway",
        "db_password": "secret",
        "mysql_export_enabled": True,
        "mysql_export_allow_cidrs": "10.0.0.0/8",
        "mysql_export_advertise_host": "10.0.0.12",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)


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
    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.denied: list[tuple] = []
        self.success: list[tuple] = []
        self.denied_final: list[tuple] = []

    async def record_denied(self, *args, **kwargs) -> None:
        self.denied.append((args, kwargs))

    async def start(self, *args, **kwargs) -> None:
        self.started.append((args, kwargs))

    async def mark_success(self, *args, **kwargs) -> None:
        self.success.append((args, kwargs))

    async def mark_error_best_effort(self, *args, **kwargs) -> None:
        raise AssertionError("mark_error_best_effort 不应被调用")

    async def mark_denied_best_effort(self, *args, **kwargs) -> None:
        self.denied_final.append((args, kwargs))


class FakeDatasources:
    def __init__(self, *datasources) -> None:
        self.datasources = list(datasources)

    async def get_by_name(self, name: str):
        return next((item for item in self.datasources if item.name == name), None)

    async def list_all(self):
        return list(self.datasources)


class FakeMasking:
    async def list_rules(self, datasource_id: int):
        return []


class FakeResult:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows
        self.rowcount = len(rows)

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows


class FakeEngine:
    def __init__(self, result: FakeResult) -> None:
        self._result = result
        self.executed: list[str] = []

    @asynccontextmanager
    async def connect(self):
        yield _FakeConn(self)

    @asynccontextmanager
    async def begin(self):
        yield _FakeConn(self)

    async def exec(self, sql: str) -> FakeResult:
        self.executed.append(sql)
        return self._result


class _FakeConn:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def exec_driver_sql(self, sql: str) -> FakeResult:
        return await self._engine.exec(sql)


class FakeCredentials:
    def __init__(self, issued=None, error: Exception | None = None) -> None:
        self.issued = issued
        self.error = error
        self.calls: list[dict] = []

    async def issue_auto(self, **kwargs) -> AutoIssuedCredential:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.issued


def make_services(
    datasource,
    *,
    settings: Settings | None = None,
    rows: list[tuple] | None = None,
    columns: list[str] | None = None,
    credentials: FakeCredentials | None = None,
):
    settings = settings or make_settings()
    engine = FakeEngine(FakeResult(columns or ["id", "name"], rows or []))
    services = SimpleNamespace(
        audit=FakeAudit(),
        datasources=FakeDatasources(datasource),
        pool=SimpleNamespace(get=_always(engine)),
        masking=FakeMasking(),
        credentials=credentials or FakeCredentials(),
        mask_key=b"k" * 32,
        settings=settings,
    )
    return services, engine


def _always(value):
    async def provider(datasource):
        return value

    return provider


# ---------- Row soft cap (T30, AC26/AC30) ----------


async def prepare(sql: str, row_cap, datasource=None):
    datasource = datasource or make_datasource()
    services, _ = make_services(datasource)
    return await prepare_read(
        services=services,
        principal=PRINCIPAL,
        client_ip="127.0.0.1",
        datasource_name=datasource.name,
        sql=sql,
        channel="mcp",
        row_cap=row_cap,
    )


async def test_select_without_limit_is_capped() -> None:
    prepared = await prepare("SELECT id, name FROM users", row_cap=2000)
    assert "LIMIT 2001" in prepared.exec_sql
    assert prepared.row_cap == 2000


async def test_smaller_user_limit_wins() -> None:
    sql = "SELECT id FROM users LIMIT 10"
    prepared = await prepare(sql, row_cap=2000)
    assert prepared.exec_sql == sql
    assert prepared.row_cap == 2000


async def test_larger_user_limit_is_lowered() -> None:
    prepared = await prepare("SELECT id FROM users LIMIT 5000", row_cap=2000)
    assert "LIMIT 2001" in prepared.exec_sql
    assert "5000" not in prepared.exec_sql


async def test_limit_offset_form_keeps_offset() -> None:
    prepared = await prepare("SELECT id FROM users LIMIT 10, 5000", row_cap=2000)
    assert "LIMIT 2001" in prepared.exec_sql
    assert "OFFSET 10" in prepared.exec_sql


async def test_union_is_capped() -> None:
    prepared = await prepare(
        "SELECT id FROM a UNION ALL SELECT id FROM b", row_cap=2000
    )
    assert "LIMIT 2001" in prepared.exec_sql


async def test_write_statement_is_not_capped() -> None:
    sql = "UPDATE users SET name = 'x' WHERE id = 1"
    prepared = await prepare(sql, row_cap=2000, datasource=make_datasource(is_read_only=0))
    assert prepared.exec_sql == sql
    assert "LIMIT" not in prepared.exec_sql


async def test_row_cap_none_or_zero_does_not_rewrite() -> None:
    for row_cap in (None, 0):
        prepared = await prepare("SELECT id FROM users", row_cap=row_cap)
        assert prepared.exec_sql == "SELECT id FROM users"
        assert prepared.row_cap is None


# ---------- Byte truncation (_truncate_by_bytes) ----------


def test_truncate_by_bytes_passthrough_when_cap_disabled() -> None:
    rows = [[datetime(2026, 10, 1, 8, 0, 0)]]
    assert _truncate_by_bytes(rows, 0) == ([["2026-10-01T08:00:00"]], False)
    assert _truncate_by_bytes(rows, -1) == ([["2026-10-01T08:00:00"]], False)


def test_truncate_by_bytes_counts_utf8_not_characters() -> None:
    rows = [["中"]]
    assert len('["中"]'.encode("utf-8")) == 7
    assert _truncate_by_bytes(rows, 7) == ([["中"]], False)
    # Counting characters (5) would misjudge it as under the limit; this must return 0 rows.
    assert _truncate_by_bytes(rows, 6) == ([], True)


def test_truncate_by_bytes_stops_before_overrun() -> None:
    rows = [["aa"], ["bb"], ["cc"]]  # 6 bytes of JSON per row
    kept, truncated = _truncate_by_bytes(rows, 12)
    assert kept == [["aa"], ["bb"]]
    assert truncated is True


# ---------- Read-path truncation and handoff (AC26/AC27) ----------


def issued_credential() -> AutoIssuedCredential:
    return AutoIssuedCredential(
        account_name="exp_9f3a1c7b2d85",
        password="S3cret-Export-Password",
        expires_at=datetime(2026, 10, 28, 7, 30, 0),
        datasource_id=1,
        source_ip="10.0.0.5",
    )


async def run_query(
    *,
    settings: Settings,
    rows: list[tuple],
    sql: str = "SELECT id, name FROM users",
    credentials: FakeCredentials | None = None,
):
    datasource = make_datasource()
    services, engine = make_services(
        datasource, settings=settings, rows=rows, credentials=credentials
    )
    result = await execute_sql(services, PRINCIPAL, "10.0.0.5", datasource.name, sql)
    return result, services, engine


async def test_row_truncation_returns_exactly_cap_rows() -> None:
    settings = make_settings(mcp_result_max_rows=3)
    rows = [(1, "a"), (2, "b"), (3, "c"), (4, "d")]  # The extra row fetched by LIMIT 4
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, engine = await run_query(
        settings=settings, rows=rows, credentials=credentials
    )

    # Upstream runs LIMIT cap+1, not fetchall then slice
    assert "LIMIT 4" in engine.executed[-1]
    assert result["truncated"] is True
    assert result["truncated_reason"] == "rows"
    assert result["row_count"] == 3
    assert len(result["rows"]) == 3
    assert result["rows"][-1] == [3, "c"]


async def test_export_payload_shape() -> None:
    settings = make_settings(mcp_result_max_rows=3)
    sql = "SELECT id, name FROM users"
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, _engine = await run_query(
        settings=settings, rows=[(1, "a"), (2, "b"), (3, "c"), (4, "d")],
        sql=sql, credentials=credentials,
    )

    export = result["export"]
    assert export is not None
    assert export["protocol"] == "mysql"
    assert export["host"] == "10.0.0.12"
    assert export["port"] == 3307
    assert export["user"] == "exp_9f3a1c7b2d85"
    assert export["password"] == "S3cret-Export-Password"
    assert export["database"] == "demo"
    assert export["expires_at"] == "2026-10-28T07:30:00Z"
    # Must be the user's original SQL, not the exec_sql with LIMIT
    assert export["sql"] == sql
    assert "SSCursor" in export["sample_code"]
    assert "os.environ['GATEWAY_EXPORT_PASSWORD']" in export["sample_code"]
    assert "S3cret-Export-Password" not in export["sample_code"]
    assert result["hint"] == HANDOFF_HINT_ISSUED
    assert credentials.calls[0]["trigger_request_id"] == result["request_id"]


async def test_byte_truncation_reports_bytes() -> None:
    settings = make_settings(mcp_result_max_rows=0, mcp_result_max_bytes=40)
    rows = [(1, "x" * 40), (2, "y" * 40)]
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, _engine = await run_query(
        settings=settings, rows=rows, credentials=credentials
    )

    assert result["truncated"] is True
    assert result["truncated_reason"] == "bytes"
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["export"] is not None


async def test_both_limits_report_rows() -> None:
    settings = make_settings(mcp_result_max_rows=2, mcp_result_max_bytes=5)
    rows = [(1, "aaaaaaaa"), (2, "bbbbbbbb"), (3, "cccccccc")]

    result, _services, _engine = await run_query(
        settings=settings, rows=rows, credentials=FakeCredentials(issued=issued_credential())
    )

    assert result["truncated"] is True
    assert result["truncated_reason"] == "rows"


async def test_untruncated_keeps_legacy_shape() -> None:
    settings = make_settings(mcp_result_max_rows=0, mcp_result_max_bytes=0)
    credentials = FakeCredentials()

    result, _services, _engine = await run_query(
        settings=settings, rows=[(1, "a"), (2, "b")], credentials=credentials
    )

    assert result["truncated"] is False
    assert result["truncated_reason"] is None
    assert result["hint"] is None
    assert result["export"] is None
    assert result["row_count"] == 2
    assert result["rows"] == [[1, "a"], [2, "b"]]
    assert credentials.calls == []


async def test_auto_issue_disabled_gives_hint_only() -> None:
    settings = make_settings(mcp_result_max_rows=2, mcp_export_auto_issue=False)
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, _engine = await run_query(
        settings=settings, rows=[(1, "a"), (2, "b"), (3, "c")], credentials=credentials
    )

    assert result["truncated"] is True
    assert result["export"] is None
    assert result["hint"].startswith("结果已截断（2 行）")
    assert "缩小查询范围" in result["hint"]
    assert credentials.calls == []


async def test_export_disabled_gives_hint_only() -> None:
    settings = make_settings(mcp_result_max_rows=2, mysql_export_enabled=False)
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, _engine = await run_query(
        settings=settings, rows=[(1, "a"), (2, "b"), (3, "c")], credentials=credentials
    )

    assert result["export"] is None
    assert result["hint"] == "结果已截断。导出通道未启用，请缩小查询范围后重试，或联系管理员。"
    assert credentials.calls == []


async def test_missing_advertise_host_gives_hint_only() -> None:
    settings = make_settings(
        mcp_result_max_rows=2, mysql_export_host="0.0.0.0", mysql_export_advertise_host=""
    )
    credentials = FakeCredentials(issued=issued_credential())

    result, _services, _engine = await run_query(
        settings=settings, rows=[(1, "a"), (2, "b"), (3, "c")], credentials=credentials
    )

    assert result["export"] is None
    assert result["hint"] == "结果已截断。导出通道地址未配置，请缩小查询范围后重试，或联系管理员。"
    assert credentials.calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValidationError("Token 不存在或已停用"), "CREDENTIAL_ISSUE_DENIED"),
        (RuntimeError("db write failed"), "INTERNAL_ERROR"),
    ],
)
async def test_issue_failure_does_not_break_result(error, code) -> None:
    settings = make_settings(mcp_result_max_rows=2)

    result, _services, _engine = await run_query(
        settings=settings,
        rows=[(1, "a"), (2, "b"), (3, "c")],
        credentials=FakeCredentials(error=error),
    )

    assert result["truncated"] is True
    assert result["row_count"] == 2
    assert result["export"] is None
    assert f"自动签发导出凭证失败（{code}）" in result["hint"]


async def test_export_password_never_logged(caplog) -> None:
    settings = make_settings(mcp_result_max_rows=2)
    credentials = FakeCredentials(issued=issued_credential())

    with caplog.at_level(logging.DEBUG):
        result, services, _engine = await run_query(
            settings=settings,
            rows=[(1, "a"), (2, "b"), (3, "c")],
            credentials=credentials,
        )

    password = result["export"]["password"]
    assert password not in caplog.text
    assert all(password not in str(call) for call in services.audit.started)
    assert all(password not in str(call) for call in services.audit.success)


# ---------- Explicit issue tool (issue_export_credential) ----------


def error_payload(exc: ToolError) -> dict:
    return json.loads(exc.args[0])


async def run_issue(
    *,
    settings: Settings | None = None,
    credentials: FakeCredentials | None = None,
    datasource=None,
    sql: str = "SELECT id, name FROM users",
    reason: str = "导出商户订单",
    client_ip: str = "10.0.0.5",
):
    datasource = datasource or make_datasource()
    services, engine = make_services(
        datasource, settings=settings, credentials=credentials
    )
    result = await issue_export_credential(
        services, PRINCIPAL, client_ip, datasource.name, sql, reason
    )
    return result, services, engine


async def test_explicit_issue_returns_runnable_payload() -> None:
    credentials = FakeCredentials(issued=issued_credential())
    result, services, _engine = await run_issue(credentials=credentials)

    assert result["protocol"] == "mysql"
    assert result["host"] == "10.0.0.12"
    assert result["port"] == 3307
    assert result["user"] == "exp_9f3a1c7b2d85"
    assert result["password"] == "S3cret-Export-Password"
    assert result["database"] == "demo"
    assert result["expires_at"] == "2026-10-28T07:30:00Z"
    assert result["sql"] == "SELECT id, name FROM users"
    assert result["notice"] == HANDOFF_NOTICE
    assert "SSCursor" in result["sample_code"]
    assert result["password"] not in result["sample_code"]

    call = credentials.calls[0]
    assert call["token_id"] == PRINCIPAL.token_id
    assert call["datasource_id"] == 1
    assert call["trigger_request_id"] == result["request_id"]
    assert call["reason"] == "导出商户订单"

    # The precheck and the issue each write one audit record; the precheck row ends with row_count=0, leaving no pending.
    assert len(services.audit.started) == 1
    assert services.audit.success[0][0][2] == 0


async def test_explicit_issue_normalizes_reason() -> None:
    credentials = FakeCredentials(issued=issued_credential())
    _result, _services, _engine = await run_issue(
        credentials=credentials, reason="  导出\n商户   订单  "
    )
    assert credentials.calls[0]["reason"] == "导出 商户 订单"


async def test_explicit_issue_requires_enabled_channel() -> None:
    settings = make_settings(mysql_export_enabled=False)
    with pytest.raises(ToolError) as exc_info:
        await run_issue(settings=settings)

    assert error_payload(exc_info.value)["error"]["code"] == EXPORT_UNAVAILABLE


async def test_explicit_issue_requires_advertise_host() -> None:
    settings = make_settings(
        mysql_export_host="0.0.0.0", mysql_export_advertise_host=""
    )
    with pytest.raises(ToolError) as exc_info:
        await run_issue(settings=settings)

    assert error_payload(exc_info.value)["error"]["code"] == EXPORT_UNAVAILABLE


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201])
async def test_explicit_issue_rejects_bad_reason(reason: str) -> None:
    credentials = FakeCredentials(issued=issued_credential())
    with pytest.raises(ToolError) as exc_info:
        await run_issue(credentials=credentials, reason=reason)

    assert error_payload(exc_info.value)["error"]["code"] == "INVALID_ARGUMENT"
    assert credentials.calls == []


async def test_explicit_issue_rejects_write_sql() -> None:
    credentials = FakeCredentials(issued=issued_credential())
    with pytest.raises(ToolError) as exc_info:
        await run_issue(
            credentials=credentials,
            datasource=make_datasource(is_read_only=0),
            sql="UPDATE users SET name = 'x' WHERE id = 1",
        )

    assert error_payload(exc_info.value)["error"]["code"] == "WRITE_ON_READONLY"
    assert credentials.calls == []


async def test_explicit_issue_denied_keeps_audit_row_final() -> None:
    datasource = make_datasource()
    credentials = FakeCredentials(error=ValidationError("数据源不在授权范围内"))
    services, _engine = make_services(datasource, credentials=credentials)

    with pytest.raises(ToolError) as exc_info:
        await issue_export_credential(
            services,
            PRINCIPAL,
            "10.0.0.5",
            datasource.name,
            "SELECT id, name FROM users",
            "导出商户订单",
        )

    assert error_payload(exc_info.value)["error"]["code"] == "CREDENTIAL_ISSUE_DENIED"
    assert len(services.audit.started) == 1
    assert len(services.audit.denied_final) == 1


async def test_explicit_issue_password_not_audited() -> None:
    credentials = FakeCredentials(issued=issued_credential())
    result, services, _engine = await run_issue(credentials=credentials)

    password = result["password"]
    assert all(password not in str(call) for call in services.audit.started)
    assert all(password not in str(call) for call in services.audit.success)
    assert all(password not in str(call) for call in services.audit.denied)


# ---------- Literal percent signs in user SQL (regression) ----------


async def test_read_path_doubles_literal_percent_signs() -> None:
    sql = (
        "SELECT DATE_FORMAT(FROM_UNIXTIME(ts), '%Y-%m-%d') AS ins_day, COUNT(*) AS cnt "
        "FROM refund_order_now GROUP BY ins_day"
    )

    result, _services, engine = await run_query(
        settings=make_settings(), rows=[("2026-08-15", 3)], sql=sql
    )

    assert "%%Y-%%m-%%d" in engine.executed[-1]
    assert result["rows"] == [["2026-08-15", 3]]


async def test_write_path_doubles_literal_percent_signs() -> None:
    datasource = make_datasource(is_read_only=0)
    services, engine = make_services(datasource)
    sql = "UPDATE users SET pct = pct % 100 WHERE DATE_FORMAT(ts, '%Y-%m') = '2026-08'"

    await execute_sql(services, PRINCIPAL, "10.0.0.5", datasource.name, sql)

    assert "%%Y-%%m" in engine.executed[-1]
