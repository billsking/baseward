"""AC16 and §6.4 error mapping: MCP and the export channel agree cell by cell, errno and SQLSTATE match exactly."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pymysql
import pytest
from mysql_mimic.errors import ErrorCode, get_sqlstate
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.engine import URL

from app.mysql.session import _ERRNO_BY_CODE
from tests.integration.conftest import (
    call_tool,
    connect_export,
    export_server,
    export_settings,
    issue_credential,
    mcp_session,
)
from tests.integration.mysql_wire import WireConnection, WireError

pytestmark = pytest.mark.integration

SELECT_SQL = "SELECT id, phone, email, note, amount FROM users ORDER BY id"
MESSAGE_RE = re.compile(
    r"^\[GATEWAY:(?P<code>[A-Z_]+)\] .+ \(request_id=(?P<request_id>[0-9a-f-]+)\)$"
)

AUTH_CODES = (
    "CREDENTIAL_NOT_FOUND",
    "CREDENTIAL_EXPIRED",
    "CREDENTIAL_REVOKED",
    "CREDENTIAL_PASSWORD_INVALID",
    "TOKEN_DISABLED",
    "DATASOURCE_MISSING",
    "DATASOURCE_DISABLED",
)

POLICY_CODES = (
    "DATASOURCE_DENIED",
    "TABLE_DENIED",
    "FIELD_DENIED",
    "FIELDS_REQUIRED",
    "FIELD_SCOPE_CHECK_FAILED",
    "DDL_BLOCKED",
    "UNSUPPORTED_STATEMENT",
    "LOCKING_READ",
    "INTO_OUTFILE",
    "WRITE_ON_READONLY",
    "KILL_DENIED",
)

LIMIT_CODES = (
    "EXPORT_ROW_LIMIT",
    "EXPORT_TIME_LIMIT",
    "AUDIT_UNAVAILABLE",
    "DB_UNAVAILABLE",
    "DB_ERROR",
    "INTERNAL_ERROR",
)

# EMPTY_SQL and NO_DATABASE_SELECTED are unreachable on the export channel: an empty COM_QUERY is answered OK,
# and the session default database is always the credential's database. They exist only as defense, so a table-level assertion covers them.
MAPPING_TABLE = [
    *((code, 1045, "28000") for code in AUTH_CODES),
    ("EXPORT_CONCURRENCY_LIMIT", 1040, "08004"),
    ("NO_DATABASE_SELECTED", 1046, "3D000"),
    ("EMPTY_SQL", 1065, "42000"),
    ("SQL_PARSE_ERROR", 1064, "42000"),
    ("MULTI_STATEMENT", 1064, "42000"),
    *((code, 1235, "42000") for code in POLICY_CODES),
    *((code, 1105, "HY000") for code in LIMIT_CODES),
]

WIRE_CASES = [
    ("SELECT id FROM", 1064, "42000", "SQL_PARSE_ERROR"),
    ("SELECT 1; SELECT 2", 1064, "42000", "MULTI_STATEMENT"),
    ("DELETE FROM users", 1235, "42000", "WRITE_ON_READONLY"),
    ("SELECT * FROM users FOR UPDATE", 1235, "42000", "LOCKING_READ"),
    ("SELECT id INTO @x FROM users", 1235, "42000", "INTO_OUTFILE"),
    ("SELECT missing_col FROM users", 1105, "HY000", "DB_ERROR"),
]


def _normalize(row) -> list:
    return [str(value) if isinstance(value, Decimal) else value for value in row]


def _pymysql_rows(port: int, account: str, password: str):
    connection = connect_export(port, account, password)
    try:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_SQL)
            return [list(row) for row in cursor.fetchall()], cursor.description
    finally:
        connection.close()


def _sqlalchemy_rows(port: int, account: str, password: str, database: str):
    url = URL.create(
        "mysql+pymysql",
        username=account,
        password=password,
        host="127.0.0.1",
        port=port,
        database=database,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [list(row) for row in conn.execute(sa_text(SELECT_SQL))]
    finally:
        engine.dispose()


def _resolve_error_code(code: str) -> ErrorCode:
    if code in AUTH_CODES:
        return ErrorCode.ACCESS_DENIED_ERROR
    return _ERRNO_BY_CODE.get(code, ErrorCode.NOT_SUPPORTED_YET)


async def test_ac16_mcp_and_two_clients_agree_cell_by_cell(env):
    account, password = await issue_credential(env)

    async with mcp_session(env) as session:
        failed, payload = await call_tool(
            session, "execute_sql", {"datasource": env.ds_name, "sql": SELECT_SQL}
        )
    assert failed is False, payload
    assert payload["masked_columns"] == ["phone", "email"]
    expected_rows = payload["rows"]
    assert expected_rows[0][1] == "138****8000"
    assert "13800138000" not in str(expected_rows)

    async with export_server(env) as (port, _):
        pymysql_rows, description = await asyncio.to_thread(
            _pymysql_rows, port, account, password
        )
        assert [_normalize(row) for row in pymysql_rows] == expected_rows

        # Masked columns are always strings on the export channel (D20); pandas reading them as object is expected.
        types = {column[0]: column[1] for column in description}
        assert types["phone"] == pymysql.constants.FIELD_TYPE.VAR_STRING
        assert types["email"] == pymysql.constants.FIELD_TYPE.VAR_STRING
        assert types["id"] == pymysql.constants.FIELD_TYPE.LONG
        assert types["amount"] == pymysql.constants.FIELD_TYPE.NEWDECIMAL

        alchemy_rows = await asyncio.to_thread(
            _sqlalchemy_rows, port, account, password, env.biz_db
        )
    assert [_normalize(row) for row in alchemy_rows] == expected_rows


@pytest.mark.parametrize(("code", "errno", "sqlstate"), MAPPING_TABLE)
def test_error_code_mapping_table_matches_spec(code, errno, sqlstate):
    resolved = _resolve_error_code(code)
    assert int(resolved) == errno
    assert get_sqlstate(resolved).decode("ascii") == sqlstate


@pytest.mark.parametrize(("sql", "errno", "sqlstate", "gateway_code"), WIRE_CASES)
async def test_wire_errors_carry_errno_sqlstate_and_request_id(
    env, sql, errno, sqlstate, gateway_code
):
    account, password = await issue_credential(env)

    def _run(port: int) -> None:
        connection = WireConnection("127.0.0.1", port, account, password)
        try:
            connection.query(sql)
        finally:
            connection.close()

    async with export_server(env) as (port, _):
        with pytest.raises(WireError) as excinfo:
            await asyncio.to_thread(_run, port)

    error = excinfo.value
    assert error.errno == errno
    assert error.sqlstate == sqlstate
    match = MESSAGE_RE.match(error.message)
    assert match is not None, error.message
    assert match.group("code") == gateway_code
    if gateway_code == "DB_ERROR":
        assert "Unknown column" not in error.message
        assert "missing_col" not in error.message


async def test_auth_failure_on_wire_is_1045_28000(env):
    account, password = await issue_credential(env)

    def _run(port: int) -> None:
        connection = WireConnection("127.0.0.1", port, account, password + "x")
        connection.close()

    async with export_server(env) as (port, _):
        with pytest.raises(WireError) as excinfo:
            await asyncio.to_thread(_run, port)

    assert excinfo.value.errno == 1045
    assert excinfo.value.sqlstate == "28000"
    assert "GATEWAY:CREDENTIAL_PASSWORD_INVALID" not in excinfo.value.message


async def test_row_limit_on_wire_is_1105_hy000(env):
    account, password = await issue_credential(env)
    settings = export_settings(env, mysql_export_max_rows=1)

    def _run(port: int) -> None:
        connection = WireConnection("127.0.0.1", port, account, password)
        try:
            connection.query(SELECT_SQL)
        finally:
            connection.close()

    async with export_server(env, settings) as (port, _):
        with pytest.raises(WireError) as excinfo:
            await asyncio.to_thread(_run, port)

    error = excinfo.value
    assert error.errno == 1105
    assert error.sqlstate == "HY000"
    assert "GATEWAY:EXPORT_ROW_LIMIT" in error.message
    assert re.search(r"request_id=[0-9a-f-]{36}", error.message)
