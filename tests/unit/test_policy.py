"""Table-driven SQL policy engine tests."""

import pytest

from app.policy.engine import (
    DDL_BLOCKED,
    EMPTY_SQL,
    LOCKING_READ,
    MULTI_STATEMENT,
    NO_WHERE,
    SQL_PARSE_ERROR,
    UNSUPPORTED_STATEMENT,
    WRITE_ON_READONLY,
    evaluate,
)

ALLOWED_READONLY = [
    "SELECT * FROM users",
    "SELECT u.id, o.amount FROM users u JOIN orders o ON o.user_id = u.id",
    "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)",
    "WITH recent AS (SELECT * FROM orders WHERE created_at > '2026-01-01') SELECT * FROM recent",
    "SELECT id FROM users UNION SELECT id FROM admins",
    "DESCRIBE users",
    "SELECt id\nFROM users\nWHERE id = 1",
    "SELECT /* comment */ id FROM users -- trailing",
]

ALLOWED_WRITE = [
    "INSERT INTO users (name) VALUES ('a')",
    "INSERT INTO users (id, name) VALUES (1, 'a') ON DUPLICATE KEY UPDATE name = 'a'",
    "REPLACE INTO users (id, name) VALUES (1, 'a')",
    "UPDATE users SET name = 'b' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
    "DELETE FROM users USING users JOIN orders ON orders.user_id = users.id WHERE orders.id = 1",
]

# (SQL, expected error code, datasource read-only?)
DENIED = [
    ("CREATE TABLE t (id INT)", DDL_BLOCKED, True),
    ("ALTER TABLE users ADD COLUMN x INT", DDL_BLOCKED, True),
    ("DROP TABLE users", DDL_BLOCKED, True),
    ("TRUNCATE TABLE users", DDL_BLOCKED, True),
    ("RENAME TABLE users TO users_old", UNSUPPORTED_STATEMENT, True),
    ("GRANT ALL ON *.* TO 'x'@'%'", UNSUPPORTED_STATEMENT, True),
    ("REVOKE ALL ON *.* FROM 'x'@'%'", UNSUPPORTED_STATEMENT, True),
    ("CALL do_something()", UNSUPPORTED_STATEMENT, True),
    ("PREPARE stmt FROM 'SELECT 1'", UNSUPPORTED_STATEMENT, True),
    ("EXECUTE stmt", UNSUPPORTED_STATEMENT, True),
    ("LOCK TABLES users WRITE", UNSUPPORTED_STATEMENT, True),
    ("LOAD DATA INFILE '/tmp/x' INTO TABLE users", SQL_PARSE_ERROR, True),
    ("SET GLOBAL max_connections = 1", UNSUPPORTED_STATEMENT, True),
    ("SET PERSIST max_connections = 1", UNSUPPORTED_STATEMENT, True),
    ("SHOW TABLES", UNSUPPORTED_STATEMENT, True),
    ("EXPLAIN SELECT 1", UNSUPPORTED_STATEMENT, True),
    ("SELECT * FROM users FOR UPDATE", LOCKING_READ, True),
    ("SELECT * FROM users LOCK IN SHARE MODE", LOCKING_READ, True),
    ("SELECT * FROM users INTO OUTFILE '/tmp/users.txt'", SQL_PARSE_ERROR, True),
    ("SELECT 1; SELECT 2", MULTI_STATEMENT, True),
    ("", EMPTY_SQL, True),
    ("   \n ", EMPTY_SQL, True),
    ("UPDATE users SET name = 'x'", NO_WHERE, False),
    ("DELETE FROM users", NO_WHERE, False),
    ("DELETE FROM users USING users JOIN orders ON orders.user_id = users.id", NO_WHERE, False),
    ("UPDATE users SET name = 'x' WHERE 1=1", NO_WHERE, False),
    ("DELETE FROM users WHERE TRUE", NO_WHERE, False),
    ("REPLACE INTO users (id, note) VALUES (1, 'a;b')", UNSUPPORTED_STATEMENT, False),
    ("dRoP /* hidden */ TABLE users", DDL_BLOCKED, True),
    ("select * from users for\nupdate", LOCKING_READ, True),
    ("SELECT * FROM users WHERE id = ", SQL_PARSE_ERROR, True),
]


def _readonly(value: bool):
    return lambda _database: value


@pytest.mark.parametrize("sql", ALLOWED_READONLY)
def test_allowed_read_statements_on_readonly_source(sql: str) -> None:
    decision = evaluate(sql, default_database="main", read_only_for=_readonly(True))
    assert decision.allowed, f"{sql} -> {decision.code} {decision.message}"
    assert decision.is_write is False
    assert decision.ast is not None
    assert decision.statement_type in {"Select", "Union", "Describe"}


@pytest.mark.parametrize("sql", ALLOWED_WRITE)
def test_allowed_write_statements_on_writable_source(sql: str) -> None:
    decision = evaluate(sql, default_database="main", read_only_for=_readonly(False))
    assert decision.allowed, f"{sql} -> {decision.code} {decision.message}"
    assert decision.is_write is True
    assert decision.ast is not None


@pytest.mark.parametrize("sql", ALLOWED_WRITE)
def test_write_statements_rejected_on_readonly_source(sql: str) -> None:
    decision = evaluate(sql, default_database="main", read_only_for=_readonly(True))
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert decision.ast is None


@pytest.mark.parametrize(("sql", "code", "is_read_only"), DENIED)
def test_denied_statements(sql: str, code: str, is_read_only: bool) -> None:
    decision = evaluate(sql, default_database="main", read_only_for=_readonly(is_read_only))
    assert not decision.allowed, sql
    assert decision.code == code, f"{sql} -> {decision.code} {decision.message}"
    assert decision.ast is None


def test_replace_statement_type_is_recorded() -> None:
    decision = evaluate(
        "REPLACE INTO users (id) VALUES (1)",
        default_database="main",
        read_only_for=_readonly(False),
    )
    assert decision.allowed
    assert decision.statement_type == "REPLACE"


# ---------- v1.8: write permission decided per database (D65 / AC48) ----------

REPORT_READ_ONLY = lambda database: database == "report"  # noqa: E731


def test_unqualified_write_uses_default_database() -> None:
    decision = evaluate(
        "UPDATE users SET name = 'x' WHERE id = 1",
        default_database="report",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert "report" in decision.message


def test_qualified_write_to_readonly_database_is_denied() -> None:
    decision = evaluate(
        "INSERT INTO report.t (id) VALUES (1)",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert "report" in decision.message


def test_insert_select_from_readonly_database_is_allowed() -> None:
    decision = evaluate(
        "INSERT INTO main.t SELECT * FROM report.v",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert decision.allowed, decision.message
    assert decision.is_write is True


def test_insert_select_into_readonly_database_is_denied() -> None:
    decision = evaluate(
        "INSERT INTO report.t SELECT * FROM main.v",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert "report" in decision.message


def test_cross_database_write_names_first_readonly_target_in_ast_order() -> None:
    # AST order is z, a; lexicographic order is a, z. Must name z.
    decision = evaluate(
        "UPDATE z.t, a.u SET z.t.x = 1, a.u.y = 2 WHERE z.t.id = a.u.id",
        default_database="main",
        read_only_for=lambda database: database in {"z", "a"},
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert "库 z 为只读" in decision.message
    assert "库 a 为只读" not in decision.message


def test_replace_into_readonly_database_is_denied() -> None:
    decision = evaluate(
        "REPLACE INTO report.t (id) VALUES (1)",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY
    assert "report" in decision.message


def test_delete_using_readonly_table_is_a_read_not_a_write_target() -> None:
    decision = evaluate(
        "DELETE FROM main.t USING main.t "
        "JOIN report.v ON main.t.id = report.v.id WHERE report.v.id = 1",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert decision.allowed, decision.message


def test_write_subquery_source_is_not_a_write_target() -> None:
    decision = evaluate(
        "UPDATE main.t SET x = (SELECT MAX(y) FROM report.v) WHERE id = 1",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert decision.allowed, decision.message


def test_missing_default_database_falls_back_to_callback() -> None:
    seen: list[str] = []

    def read_only_for(database: str) -> bool:
        seen.append(database)
        return False

    decision = evaluate(
        "INSERT INTO t (id) VALUES (1)",
        default_database="",
        read_only_for=read_only_for,
    )
    assert decision.allowed, decision.message
    assert seen == [""]


def test_ddl_is_rejected_without_consulting_database_policy() -> None:
    seen: list[str] = []

    def read_only_for(database: str) -> bool:
        seen.append(database)
        return False

    for sql in ("CREATE TABLE t (id INT)", "DROP TABLE report.t"):
        decision = evaluate(sql, default_database="main", read_only_for=read_only_for)
        assert not decision.allowed
        assert decision.code == DDL_BLOCKED
    assert seen == []


def test_no_where_is_checked_after_readonly_policy() -> None:
    decision = evaluate(
        "UPDATE report.t SET x = 1",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == WRITE_ON_READONLY

    decision = evaluate(
        "UPDATE main.t SET x = 1",
        default_database="main",
        read_only_for=REPORT_READ_ONLY,
    )
    assert not decision.allowed
    assert decision.code == NO_WHERE
