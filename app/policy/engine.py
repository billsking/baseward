"""SQL security policy over the sqlglot AST.

Principles: default deny plus a type allowlist. Every decision is AST-based; no keyword regexes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# Error codes (matching spec 6.3)
EMPTY_SQL = "EMPTY_SQL"
SQL_PARSE_ERROR = "SQL_PARSE_ERROR"
MULTI_STATEMENT = "MULTI_STATEMENT"
DDL_BLOCKED = "DDL_BLOCKED"
UNSUPPORTED_STATEMENT = "UNSUPPORTED_STATEMENT"
LOCKING_READ = "LOCKING_READ"
INTO_OUTFILE = "INTO_OUTFILE"
WRITE_ON_READONLY = "WRITE_ON_READONLY"
NO_WHERE = "NO_WHERE"

PARSE_MESSAGE_LIMIT = 200

_FORBIDDEN_NODES: tuple[tuple[type[exp.Expression], str, str], ...] = (
    (exp.Create, DDL_BLOCKED, "禁止执行 DDL 语句（CREATE/ALTER/DROP/TRUNCATE/RENAME）"),
    (exp.Alter, DDL_BLOCKED, "禁止执行 DDL 语句（CREATE/ALTER/DROP/TRUNCATE/RENAME）"),
    (exp.Drop, DDL_BLOCKED, "禁止执行 DDL 语句（CREATE/ALTER/DROP/TRUNCATE/RENAME）"),
    (exp.TruncateTable, DDL_BLOCKED, "禁止执行 DDL 语句（CREATE/ALTER/DROP/TRUNCATE/RENAME）"),
    (exp.Command, UNSUPPORTED_STATEMENT, "不支持的 SQL 语句类型"),
    (exp.Into, INTO_OUTFILE, "禁止 SELECT ... INTO OUTFILE/DUMPFILE"),
    (exp.Lock, LOCKING_READ, "禁止锁定读（FOR UPDATE / LOCK IN SHARE MODE）"),
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str
    message: str
    statement_type: str | None = None
    is_write: bool = False
    ast: exp.Expression | None = None
    database: str | None = None


def _deny(
    code: str,
    message: str,
    statement_type: str | None = None,
    database: str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        code=code,
        message=message,
        statement_type=statement_type,
        database=database,
    )


def _reparse_replace(stmt: exp.Expression) -> tuple[exp.Expression | None, str | None]:
    """Re-parse a REPLACE that degraded into a sqlglot Command as a controlled INSERT.

    Return (statement, failure reason). A non-empty reason means reject.
    """
    expression = stmt.args.get("expression")
    if not isinstance(expression, exp.Literal) or not isinstance(expression.this, str):
        return None, "REPLACE 语句无法安全重解析"
    tail = expression.this.strip()
    if not tail or ";" in tail:
        return None, "REPLACE 语句包含无法校验的分号"
    try:
        reparsed = sqlglot.parse_one("INSERT " + tail, read="mysql")
    except ParseError:
        return None, "REPLACE 语句无法安全重解析"
    if not isinstance(reparsed, exp.Insert):
        return None, "REPLACE 语句无法安全重解析"
    return reparsed, None


def _write_target_databases(stmt: exp.Expression, default_database: str) -> list[str]:
    """Collect write-target databases in AST order, deduplicated, preserving order.

    Insert counts only the written table; for Update/Delete the targets live under the `this`
    subtree (multi-table update/delete ASTs cannot reliably separate "written" from "joined",
    so collect the whole `this` subtree: prefer false rejects over false allows). USING and
    subquery sources are not inside `this`, are treated as reads, and do not enter the set.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(table: exp.Table) -> None:
        database = table.db or default_database
        if database not in seen:
            seen.add(database)
            ordered.append(database)

    for node in stmt.walk():
        if isinstance(node, exp.Insert):
            target = node.this
            if isinstance(target, exp.Schema):
                target = target.this
            if isinstance(target, exp.Table):
                add(target)
        elif isinstance(node, (exp.Update, exp.Delete)):
            if isinstance(node.this, exp.Table):
                for table in node.this.find_all(exp.Table):
                    add(table)

    if ordered:
        return ordered

    # The target cannot be parsed into a physical table (malformed): fall back to every database appearing in the statement.
    for table in stmt.find_all(exp.Table):
        add(table)
    return ordered


def evaluate(
    sql: str,
    *,
    default_database: str,
    read_only_for: Callable[[str], bool],
) -> PolicyDecision:
    """Decide whether a SQL statement may run; write permission is decided per write-target database."""
    if not sql or not sql.strip():
        return _deny(EMPTY_SQL, "SQL 不能为空")

    try:
        statements = [stmt for stmt in sqlglot.parse(sql, read="mysql") if stmt is not None]
    except ParseError as exc:
        message = str(exc)[:PARSE_MESSAGE_LIMIT]
        return _deny(SQL_PARSE_ERROR, f"SQL 解析失败：{message}")

    if len(statements) != 1:
        return _deny(MULTI_STATEMENT, "仅允许单条 SQL 语句")

    stmt = statements[0]
    statement_type = type(stmt).__name__

    if isinstance(stmt, exp.Command) and str(stmt.args.get("this", "")).upper() == "REPLACE":
        reparsed, failure = _reparse_replace(stmt)
        if failure or reparsed is None:
            return _deny(UNSUPPORTED_STATEMENT, failure or "REPLACE 语句无法安全重解析", "REPLACE")
        stmt = reparsed
        statement_type = "REPLACE"

    for node in stmt.walk():
        for node_type, code, message in _FORBIDDEN_NODES:
            if isinstance(node, node_type):
                return _deny(code, message, statement_type)

    is_read = isinstance(stmt, (exp.Select, exp.Union))
    is_describe_table = isinstance(stmt, exp.Describe) and isinstance(stmt.args.get("this"), exp.Table)
    is_write = isinstance(stmt, (exp.Insert, exp.Update, exp.Delete))

    if not (is_read or is_describe_table or is_write):
        return _deny(UNSUPPORTED_STATEMENT, "不支持的 SQL 语句类型", statement_type)

    if is_read:
        if stmt.args.get("locks"):
            return _deny(LOCKING_READ, "禁止锁定读（FOR UPDATE / LOCK IN SHARE MODE）", statement_type)
        if stmt.args.get("into"):
            return _deny(INTO_OUTFILE, "禁止 SELECT ... INTO OUTFILE/DUMPFILE", statement_type)
        return PolicyDecision(
            allowed=True,
            code="",
            message="",
            statement_type=statement_type,
            is_write=False,
            ast=stmt,
        )

    if is_describe_table:
        return PolicyDecision(
            allowed=True,
            code="",
            message="",
            statement_type=statement_type,
            is_write=False,
            ast=stmt,
        )

    for database in _write_target_databases(stmt, default_database):
        if read_only_for(database):
            return _deny(
                WRITE_ON_READONLY,
                f"库 {database} 为只读，禁止写操作；跨库写语句的任一目标库只读即拒绝",
                statement_type,
                database,
            )
    if isinstance(stmt, (exp.Update, exp.Delete)):
        where = stmt.args.get("where")
        if where is None:
            return _deny(NO_WHERE, "禁止执行无 WHERE 条件的 UPDATE/DELETE", statement_type)
        if not where.find(exp.Column):
            # WHERE 1=1 / WHERE TRUE and similar constant predicates cannot limit the write.
            return _deny(
                NO_WHERE,
                "禁止执行条件恒真的 UPDATE/DELETE：WHERE 必须引用至少一个列",
                statement_type,
            )
    return PolicyDecision(
        allowed=True,
        code="",
        message="",
        statement_type=statement_type,
        is_write=True,
        ast=stmt,
    )
