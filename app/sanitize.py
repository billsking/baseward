"""JSON-safe result conversion and database error sanitization."""

from __future__ import annotations

import base64
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.exc import InterfaceError, OperationalError

from app.errors import ValidationError

LONG_MESSAGE_LIMIT = 4000


def json_safe(value):
    """Convert common database types into JSON-serializable values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def json_safe_rows(rows: list[tuple]) -> list[list]:
    return [[json_safe(value) for value in row] for row in rows]


def _extract_mysql_errno(exc: BaseException) -> int | None:
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def map_db_error(exc: BaseException) -> tuple[str, str]:
    """Map driver errors to (code, generic message for the AI) without leaking data values."""
    errno = _extract_mysql_errno(exc)
    if isinstance(exc, (OperationalError, InterfaceError)):
        if errno is not None:
            return "DB_UNAVAILABLE", f"数据库执行失败（错误码 {errno}），详情见审计日志"
        return "DB_UNAVAILABLE", "数据库连接失败，详情见审计日志"
    if errno is not None:
        return "DB_ERROR", f"数据库执行失败（错误码 {errno}），详情见审计日志"
    return "DB_ERROR", "数据库执行失败，详情见审计日志"


def error_detail(exc: BaseException) -> str:
    """Raw error for the audit record, truncated to the column limit."""
    return str(exc)[:LONG_MESSAGE_LIMIT]


def validate_retention_days(days: int) -> int:
    if not 1 <= days <= 3650:
        raise ValidationError("审计保留天数必须在 1-3650 之间")
    return days
