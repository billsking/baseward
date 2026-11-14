"""AST result mapping and masking execution unit tests."""

from datetime import date

from sqlglot import parse_one

from app.masking.engine import apply, build_plan
from app.masking.rules import ManualRule
from app.security.crypto import mask_hmac

KEY = b"k" * 32
DS_ID = 1


def rule(table: str, column: str, strategy: str, pattern: str | None = None, database: str = "db") -> ManualRule:
    return ManualRule(
        id=None,
        datasource_id=DS_ID,
        database=database,
        table=table,
        column=column,
        strategy=strategy,
        pattern=pattern,
    )


def plan_for(sql: str, rules: list[ManualRule] | None = None, default_database: str = "db"):
    return build_plan(parse_one(sql, read="mysql"), DS_ID, default_database, rules or [])


def test_simple_column_phone() -> None:
    plan = plan_for("SELECT phone FROM users")
    rows, masked = apply(plan, ["phone"], [("13812341234",), (None,)], KEY)
    assert rows == [["138****1234"], [None]]
    assert masked == ["phone"]


def test_alias_and_qualified_column() -> None:
    plan = plan_for("SELECT u.phone AS p FROM users u")
    rows, masked = apply(plan, ["p"], [("13812341234",)], KEY)
    assert rows == [["138****1234"]]
    assert masked == ["p"]


def test_wildcard_uses_result_column_names() -> None:
    plan = plan_for("SELECT * FROM users")
    rows, masked = apply(
        plan,
        ["id", "phone", "email"],
        [(1, "13812341234", "alice@example.com")],
        KEY,
    )
    assert rows == [[1, "138****1234", "a***@example.com"]]
    assert masked == ["phone", "email"]


def test_qualified_star() -> None:
    plan = plan_for("SELECT users.* FROM users")
    rows, masked = apply(plan, ["phone"], [("13812341234",)], KEY)
    assert rows == [["138****1234"]]
    assert masked == ["phone"]


def test_join_same_column_uses_each_table_rule() -> None:
    rules = [rule("users", "phone", "off"), rule("orders", "phone", "mask", "phone")]
    plan = plan_for(
        "SELECT u.phone, o.phone FROM users u JOIN orders o ON o.user_id = u.id",
        rules,
    )
    rows, masked = apply(plan, ["phone", "phone"], [("13812341234", "13900001111")], KEY)
    assert rows == [["13812341234", "139****1111"]]
    assert masked == ["phone"]


def test_count_is_not_masked_but_max_and_expressions_are() -> None:
    count_plan = plan_for("SELECT COUNT(phone) FROM users")
    rows, masked = apply(count_plan, ["count"], [(10,)], KEY)
    assert rows == [[10]]
    assert masked == []

    max_plan = plan_for("SELECT MAX(phone) FROM users")
    rows, masked = apply(max_plan, ["max"], [("13900001111",)], KEY)
    assert rows == [["139****1111"]]
    assert masked == ["max"]

    concat_plan = plan_for("SELECT CONCAT(phone, 'x') FROM users")
    rows, masked = apply(concat_plan, ["concat"], [("13900001111x",)], KEY)
    assert rows == [["139*****111x"]]
    assert masked == ["concat"]

    bool_plan = plan_for("SELECT phone = '13900001111' FROM users")
    rows, masked = apply(bool_plan, ["expr"], [(1,)], KEY)
    assert rows == [["*"]]
    assert masked == ["expr"]


def test_manual_off_overrides_auto() -> None:
    plan = plan_for("SELECT phone FROM users", [rule("users", "phone", "off")])
    rows, masked = apply(plan, ["phone"], [("13812341234",)], KEY)
    assert rows == [["13812341234"]]
    assert masked == []


def test_manual_hash_and_null() -> None:
    hash_plan = plan_for("SELECT phone FROM users", [rule("users", "phone", "hash")])
    rows, _ = apply(hash_plan, ["phone"], [("13812341234",)], KEY)
    assert rows == [[mask_hmac("13812341234", KEY)]]

    null_plan = plan_for("SELECT phone FROM users", [rule("users", "phone", "null")])
    rows, masked = apply(null_plan, ["phone"], [("13812341234",)], KEY)
    assert rows == [[None]]
    assert masked == ["phone"]


def test_unqualified_multi_table_takes_strictest() -> None:
    rules = [
        rule("users", "phone", "mask", "phone"),
        rule("orders", "phone", "null"),
    ]
    plan = plan_for(
        "SELECT phone FROM users u JOIN orders o ON o.user_id = u.id",
        rules,
    )
    rows, masked = apply(plan, ["phone"], [("13812341234",)], KEY)
    assert rows == [[None]]
    assert masked == ["phone"]


def test_case_insensitive_rules() -> None:
    rules = [rule("USERS", "PHONE", "mask", "phone")]
    plan = plan_for("SELECT PHONE FROM USERS", rules)
    rows, masked = apply(plan, ["PHONE"], [("13812341234",)], KEY)
    assert rows == [["138****1234"]]
    assert masked == ["PHONE"]


def test_no_rule_keeps_values() -> None:
    plan = plan_for("SELECT id, created_at FROM users")
    rows, masked = apply(plan, ["id", "created_at"], [(1, date(2026, 1, 2))], KEY)
    assert rows == [[1, date(2026, 1, 2)]]
    assert masked == []


def test_values_of_other_types_do_not_break_rows() -> None:
    plan = plan_for("SELECT phone FROM users")
    rows, masked = apply(plan, ["phone"], [(None,), (123,), (b"abc",)], KEY)
    assert rows[0] == [None]
    assert isinstance(rows[1][0], str)
    assert isinstance(rows[2][0], str)
    assert masked == ["phone"]


def test_union_is_conservatively_masked() -> None:
    plan = plan_for("SELECT phone FROM users UNION SELECT email FROM admins")
    rows, masked = apply(plan, ["phone"], [("13812341234",)], KEY)
    assert masked == ["phone"]
    assert rows[0][0] != "13812341234"
