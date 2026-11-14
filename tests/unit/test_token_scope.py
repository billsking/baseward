"""Token four-level authorization domain model."""

from __future__ import annotations

import json

import pytest

from app.errors import ValidationError
from app.tokens.scope import (
    BROKEN_SCOPE,
    UNRESTRICTED,
    ColumnRef,
    TableRef,
    TokenScope,
    build_scope,
)


def test_unrestricted_allows_everything():
    assert not UNRESTRICTED.restricted
    assert UNRESTRICTED.allows_datasource(1)
    assert UNRESTRICTED.allows_database(1, "db")
    assert UNRESTRICTED.allows_table(1, "db", "users")
    assert UNRESTRICTED.allows_column(1, "db", "users", "phone")
    assert UNRESTRICTED.allowed_columns(1, "db", "users") is None
    assert UNRESTRICTED.database_state(1, "db") == "all"
    assert UNRESTRICTED.summary() == "全部"


def test_datasource_only_allows_all_tables_and_columns():
    scope = build_scope([1], [], [])
    assert scope.restricted
    assert scope.allows_table(1, "any", "table")
    assert scope.allows_column(1, "any", "table", "anything")
    assert not scope.allows_table(2, "any", "table")
    assert scope.allowed_columns(1, "any", "table") is None
    assert scope.allowed_columns(2, "any", "table") == frozenset()


def test_table_selection_limits_tables_but_not_columns():
    scope = build_scope([1], [TableRef(1, "db", "users")], [])
    assert scope.allows_table(1, "db", "users")
    assert not scope.allows_table(1, "db", "orders")
    assert scope.allows_column(1, "db", "users", "email")
    assert scope.allowed_columns(1, "db", "users") is None
    assert scope.allowed_columns(1, "db", "orders") == frozenset()


def test_column_selection_only_affects_its_table():
    scope = build_scope(
        [1],
        [TableRef(1, "db", "users"), TableRef(1, "db", "orders")],
        [ColumnRef(1, "db", "users", "Phone")],
    )
    assert scope.allowed_columns(1, "db", "users") == frozenset({"phone"})
    assert scope.allowed_columns(1, "db", "orders") is None
    assert scope.allows_column(1, "db", "users", "PHONE")
    assert not scope.allows_column(1, "db", "users", "email")


def test_table_without_columns_stays_unrestricted_even_if_other_table_has_columns():
    scope = build_scope(
        [1],
        [TableRef(1, "db", "users"), TableRef(1, "db", "orders")],
        [ColumnRef(1, "db", "users", "phone")],
    )
    assert scope.allowed_columns(1, "db", "orders") is None


def test_hierarchy_validation():
    with pytest.raises(ValidationError):
        build_scope([], [TableRef(1, "db", "users")], [])
    with pytest.raises(ValidationError):
        build_scope([1], [], [ColumnRef(1, "db", "users", "phone")])
    with pytest.raises(ValidationError):
        build_scope([1], [TableRef(2, "db", "users")], [])
    with pytest.raises(ValidationError):
        build_scope(
            [1], [TableRef(1, "db", "users")], [ColumnRef(1, "db", "orders", "id")]
        )


def test_serialization_roundtrip():
    scope = build_scope(
        [2, 1],
        [TableRef(1, "db", "users"), TableRef(2, "db2", "orders")],
        [ColumnRef(1, "db", "users", "phone")],
    )
    restored = TokenScope.from_json(scope.to_json())
    assert restored == scope
    assert restored.to_dict() == {
        "datasources": [1, 2],
        "tables": [[1, "db", "users"], [2, "db2", "orders"]],
        "columns": [[1, "db", "users", "phone"]],
    }


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "null",
        '{"datasources":["1"]}',
        '{"datasources":[true]}',
        '{"datasources":[1],"tables":[[1,"db"]]}',
        '{"datasources":[1],"tables":[[1,"db","users"]],"columns":[[1,"db","users"]]}',
        '{"tables":[[1,"db","users"]]}',
        '{"datasources":[0]}',
    ],
)
def test_invalid_payloads_rejected(raw):
    with pytest.raises((ValueError, ValidationError)):
        TokenScope.from_json(raw)


def test_broken_scope_denies_everything():
    assert BROKEN_SCOPE.restricted
    assert not BROKEN_SCOPE.allows_datasource(1)
    assert not BROKEN_SCOPE.allows_table(1, "db", "users")
    assert not BROKEN_SCOPE.allows_column(1, "db", "users", "id")
    assert BROKEN_SCOPE.summary() == "配置异常（拒绝全部）"


def test_legacy_payload_without_unrestricted_tables_keeps_old_semantics():
    scope = TokenScope.from_json('{"datasources":[1]}')
    assert scope.unrestricted_tables is None
    assert scope.table_pairs(1) is None
    assert scope.allows_table(1, "any", "table")
    assert "unrestricted_tables" not in scope.to_dict()


def test_tristate_all_tables_and_explicit_tables_coexist():
    scope = build_scope(
        [1, 2],
        [TableRef(2, "db2", "orders")],
        [
            ColumnRef(2, "db2", "orders", "amount"),
            ColumnRef(2, "db2", "orders", "order_no"),
        ],
        unrestricted_tables=[1],
    )
    assert scope.table_pairs(1) is None
    assert scope.table_names(1, "anything") is None
    assert scope.allows_table(1, "db", "brand_new_table")
    assert scope.allowed_columns(1, "db", "brand_new_table") is None

    assert scope.table_pairs(2) == frozenset({("db2", "orders")})
    assert not scope.allows_table(2, "db2", "users")
    assert scope.allowed_columns(2, "db2", "orders") == frozenset(
        {"amount", "order_no"}
    )

    restored = TokenScope.from_json(scope.to_json())
    assert restored == scope
    assert restored.to_dict()["unrestricted_tables"] == [1]


def test_tristate_without_all_tables_means_no_table_access():
    scope = build_scope(
        [1], [TableRef(1, "db", "users")], [], unrestricted_tables=[]
    )
    assert scope.allows_datasource(2) is False
    assert scope.table_pairs(1) == frozenset({("db", "users")})
    assert scope.table_pairs(2) == frozenset()

    only_unrestricted = build_scope([1, 2], [], [], unrestricted_tables=[])
    assert only_unrestricted.table_pairs(1) == frozenset()
    assert not only_unrestricted.allows_table(1, "db", "users")
    assert only_unrestricted.allowed_columns(1, "db", "users") == frozenset()

    restored = TokenScope.from_json(only_unrestricted.to_json())
    assert restored == only_unrestricted
    assert restored.unrestricted_tables == frozenset()


def test_allows_database_four_level_order():
    scoped = build_scope([1], [TableRef(1, "db", "users")], [])
    assert scoped.allows_database(1, "db")
    assert not scoped.allows_database(1, "other_db")
    assert not scoped.allows_database(2, "db")

    datasource_only = build_scope([1], [], [])
    assert datasource_only.allows_database(1, "any_db")
    assert not datasource_only.allows_database(2, "any_db")

    unrestricted = build_scope([1], [], [], unrestricted_tables=[1])
    assert unrestricted.allows_table(1, "db", "users")
    assert unrestricted.allows_database(1, "db")
    assert not unrestricted.allows_database(2, "db")

    assert UNRESTRICTED.allows_database(1, "db")


def test_tristate_summary():
    scope = build_scope([1, 2], [TableRef(2, "db2", "orders")], [], unrestricted_tables=[1])
    assert scope.summary() == "数据源 2 · 表 全部(1 个数据源) + 指定 1 · 字段 全部"
    assert build_scope([1], [], [], unrestricted_tables=[1]).summary() == (
        "数据源 1 · 表 全部(1 个数据源) · 字段 全部"
    )


def test_tristate_validation():
    with pytest.raises(ValidationError):
        build_scope([], [], [], unrestricted_tables=[1])
    with pytest.raises(ValidationError):
        build_scope([1], [], [], unrestricted_tables=[2])
    with pytest.raises(ValidationError):
        build_scope(
            [1], [TableRef(1, "db", "users")], [], unrestricted_tables=[1]
        )
    with pytest.raises(ValidationError):
        build_scope(
            [1],
            [TableRef(1, "db", "users")],
            [ColumnRef(1, "db", "users", "phone")],
            unrestricted_tables=[1],
        )
    with pytest.raises(ValueError):
        TokenScope.from_json('{"datasources":[1],"unrestricted_tables":"1"}')
    with pytest.raises(ValueError):
        TokenScope.from_json('{"datasources":[1],"unrestricted_tables":[0]}')


# ---------- v1.8: database-level authorization dimension ----------


def test_database_level_roundtrip_is_byte_stable():
    scope = build_scope(
        [2, 1],
        [TableRef(1, "archive", "orders")],
        [ColumnRef(1, "archive", "orders", "id")],
        unrestricted_databases=[(2, "trade"), (1, "demo")],
    )
    restored = TokenScope.from_json(scope.to_json())
    assert restored == scope
    assert restored.to_json() == scope.to_json()
    payload = json.loads(scope.to_json())
    assert payload["unrestricted_databases"] == [[1, "demo"], [2, "trade"]]


def test_legacy_payload_has_no_database_dimension():
    scope = TokenScope.from_json('{"datasources":[1],"tables":[[1,"db","users"]]}')
    assert scope.unrestricted_databases is None
    assert "unrestricted_databases" not in scope.to_dict()
    assert scope.allows_table(1, "db", "users")
    assert not scope.allows_table(1, "db", "orders")
    assert scope.allows_database(1, "db")
    assert not scope.allows_database(1, "other_db")


def test_library_level_all_tables_allows_new_tables_but_not_other_libraries():
    scope = build_scope([1], [], [], unrestricted_databases=[(1, "A")])
    assert scope.allows_database(1, "A")
    assert scope.allows_table(1, "A", "brand_new_table")
    assert scope.allowed_columns(1, "A", "brand_new_table") is None
    assert scope.database_state(1, "A") == "all"

    assert not scope.allows_database(1, "C")
    assert not scope.allows_table(1, "C", "users")
    assert scope.allowed_columns(1, "C", "users") == frozenset()
    assert scope.database_state(1, "C") == "none"


def test_database_state_all_partial_none():
    scope = build_scope(
        [1],
        [TableRef(1, "B", "orders"), TableRef(1, "B", "users")],
        [],
        unrestricted_databases=[(1, "A")],
    )
    assert scope.database_state(1, "A") == "all"
    assert scope.database_state(1, "B") == "partial"
    assert scope.database_state(1, "C") == "none"
    assert scope.database_state(2, "A") == "none"


def test_new_payload_with_empty_layers_does_not_open_all_databases():
    scope = build_scope([1], [], [], unrestricted_databases=[])
    assert not scope.allows_database(1, "db")
    assert not scope.allows_table(1, "db", "users")
    assert scope.database_state(1, "db") == "none"


def test_database_level_hierarchy_validation():
    with pytest.raises(ValidationError):
        build_scope([], [], [], unrestricted_databases=[(1, "A")])
    with pytest.raises(ValidationError):
        build_scope([1], [], [], unrestricted_databases=[(2, "A")])
    with pytest.raises(ValidationError):
        build_scope(
            [1], [], [], unrestricted_tables=[1], unrestricted_databases=[(1, "A")]
        )
    with pytest.raises(ValidationError):
        build_scope(
            [1], [TableRef(1, "A", "t")], [], unrestricted_databases=[(1, "A")]
        )
    with pytest.raises(ValidationError):
        build_scope(
            [1],
            [TableRef(1, "A", "t")],
            [ColumnRef(1, "A", "t", "c")],
            unrestricted_databases=[(1, "A")],
        )
    with pytest.raises((ValueError, ValidationError)):
        TokenScope.from_json('{"datasources":[1],"unrestricted_databases":[[1]]}')
    with pytest.raises((ValueError, ValidationError)):
        TokenScope.from_json('{"datasources":[1],"unrestricted_databases":[[0,"A"]]}')


def test_database_level_limit_is_512():
    pairs = [(1, f"db{index}") for index in range(512)]
    scope = build_scope([1], [], [], unrestricted_databases=pairs)
    assert scope.unrestricted_databases is not None
    assert len(scope.unrestricted_databases) == 512
    with pytest.raises(ValidationError, match="512"):
        build_scope([1], [], [], unrestricted_databases=[*pairs, (1, "db512")])


def test_table_scope_splits_database_grants_from_explicit_tables():
    """Whole-database grants must be exposed as a separate dimension, otherwise MCP lookup only sees explicit tables."""
    scope = build_scope(
        [1],
        [TableRef(1, "archive", "orders")],
        [],
        unrestricted_tables=[],
        unrestricted_databases=[(1, "trade")],
    )
    assert scope.table_scope(1) == (
        frozenset({"trade"}),
        frozenset({("archive", "orders")}),
    )
    assert scope.table_pairs(1) == frozenset({("archive", "orders")})
    assert scope.table_scope(2) == (frozenset(), frozenset())

    assert UNRESTRICTED.table_scope(1) is None
    assert build_scope([1], [], [], unrestricted_tables=[1]).table_scope(1) is None
    assert TokenScope.from_json('{"datasources":[1]}').table_scope(1) is None
    assert build_scope([1], [], [], unrestricted_tables=[]).table_scope(1) == (
        frozenset(),
        frozenset(),
    )


def test_mcp_scope_allowed_tables_keeps_database_dimension():
    from app.mcp.pipeline import _scope_allowed_tables

    scope = build_scope(
        [1],
        [TableRef(1, "archive", "orders")],
        [],
        unrestricted_tables=[],
        unrestricted_databases=[(1, "trade")],
    )
    assert _scope_allowed_tables(scope, 1) == (
        [("archive", "orders")],
        ["trade"],
    )
    assert _scope_allowed_tables(UNRESTRICTED, 1) == (None, [])
    assert _scope_allowed_tables(build_scope([1], [], [], unrestricted_tables=[]), 1) == (
        [],
        [],
    )


def test_mcp_scope_allowed_columns_keeps_database_dimension():
    from app.mcp.pipeline import _scope_allowed_columns

    scope = build_scope(
        [1],
        [TableRef(1, "archive", "orders")],
        [ColumnRef(1, "archive", "orders", "amount")],
        unrestricted_tables=[],
        unrestricted_databases=[(1, "trade")],
    )
    assert _scope_allowed_columns(scope, 1) == (
        [],
        [("archive", "orders", "amount")],
        ["trade"],
    )
    assert _scope_allowed_columns(UNRESTRICTED, 1) is None
