"""Streaming column types (D19/D20): direct type-code mapping, unknown-code and NULL fallback, masked columns become strings."""

from __future__ import annotations

import pymysql.constants.FIELD_TYPE as FIELD_TYPE
import pytest
from mysql_mimic import ColumnType

from app.masking.engine import MaskPlan
from app.masking.rules import STRATEGY_HASH, STRATEGY_MASK, STRATEGY_NULL, STRATEGY_OFF, Rule
from app.mysql.streaming import build_result_columns, string_masked_columns

TYPE_CODES = sorted(
    {
        value
        for name, value in vars(FIELD_TYPE).items()
        if name.isupper() and isinstance(value, int)
    }
)


def _description(*items: tuple[str, object]) -> list[tuple]:
    return [(name, type_code) for name, type_code in items]


def test_pymysql_field_types_are_exhaustively_covered():
    # pymysql defines 30 type constants (CHAR/TINY and ENUM/INTERVAL share values), i.e. 28 distinct type codes.
    assert len(TYPE_CODES) == 28


@pytest.mark.parametrize("type_code", TYPE_CODES)
def test_known_type_codes_map_to_same_column_type(type_code):
    columns = build_result_columns(_description(("c", type_code)), set())
    expected = (
        ColumnType.VAR_STRING if type_code == FIELD_TYPE.NULL else ColumnType(type_code)
    )
    assert columns[0].name == "c"
    assert columns[0].type is expected


def test_unknown_type_code_falls_back_to_var_string():
    columns = build_result_columns(_description(("c", 200)), set())
    assert columns[0].type is ColumnType.VAR_STRING


def test_missing_type_code_falls_back_to_var_string():
    columns = build_result_columns([("c", None)], set())
    assert columns[0].type is ColumnType.VAR_STRING


def test_empty_description_yields_no_columns():
    assert build_result_columns(None, set()) == []
    assert build_result_columns([], set()) == []


def test_string_masked_columns_only_take_mask_and_hash():
    plan = MaskPlan(
        position_rules={
            0: Rule(STRATEGY_MASK),
            1: Rule(STRATEGY_HASH),
            2: Rule(STRATEGY_NULL),
            3: Rule(STRATEGY_OFF),
        }
    )
    names = ["phone", "email", "password", "note"]
    assert string_masked_columns(plan, names) == {"phone", "email"}


def test_masked_columns_become_var_string_and_null_rule_keeps_type():
    plan = MaskPlan(
        position_rules={0: Rule(STRATEGY_MASK), 1: Rule(STRATEGY_NULL)},
    )
    names = ["phone", "password"]
    columns = build_result_columns(
        _description(("phone", FIELD_TYPE.LONGLONG), ("password", FIELD_TYPE.LONGLONG)),
        string_masked_columns(plan, names),
    )
    assert columns[0].type is ColumnType.VAR_STRING
    assert columns[1].type is ColumnType.LONGLONG


def test_wildcard_plan_marks_masked_columns_by_output_name():
    plan = MaskPlan(wildcard=True)
    names = ["Phone", "password", "name"]
    # Without manual rules, auto-detection by column name: phone matches mask, password matches null (keeps its type).
    marked = string_masked_columns(plan, names)
    assert marked == {"Phone"}
