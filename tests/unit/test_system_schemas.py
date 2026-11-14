"""System-schema exclusion: one lowercase list, matched case-insensitively at every read."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.datasources import databases
from app.mcp import introspect

APP_DIR = Path(__file__).resolve().parents[2] / "app"
CANONICAL = APP_DIR / "datasources" / "databases.py"


def test_system_databases_are_lowercase_and_cover_engine_schemas() -> None:
    assert databases.SYSTEM_DATABASES == {
        "mysql",
        "information_schema",
        "performance_schema",
        "sys",
        "metrics_schema",
        "oceanbase",
    }


def test_exclusion_predicate_lowercases_the_column() -> None:
    sql = databases.exclude_system_schemas("t.table_schema")
    assert sql.startswith("LOWER(t.table_schema) NOT IN (")
    for name in databases.SYSTEM_DATABASES:
        assert f"'{name}'" in sql


def test_introspect_builders_use_the_shared_predicate() -> None:
    for query in (
        introspect.search_tables_query(["user"], None, None),
        introspect.search_columns_query(["user"], None, None, None),
        introspect.describe_column_tables_query(["user"], None, None),
    ):
        assert query is not None
        sql, _ = query
        assert "LOWER(" in sql
        assert databases.SYSTEM_DATABASES_SQL in sql


class FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class FakeConnection:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement):
        return FakeResult([(name,) for name in self._names])


class FakeEngine:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def connect(self) -> FakeConnection:
        return FakeConnection(self._names)

    async def dispose(self) -> None:
        pass


async def test_list_upstream_databases_filters_engine_and_uppercase_names(
    monkeypatch,
) -> None:
    """TiDB reports uppercase system schemas and OceanBase adds its own; a user `test` database stays."""
    names = [
        "INFORMATION_SCHEMA",
        "PERFORMANCE_SCHEMA",
        "METRICS_SCHEMA",
        "oceanbase",
        "SYS",
        "test",
        "shop",
    ]
    monkeypatch.setattr(
        databases, "create_async_engine", lambda *a, **k: FakeEngine(names)
    )
    datasource = SimpleNamespace(
        password="pw", username="user", host="db", port=3306, default_database=None
    )

    assert await databases.list_upstream_databases(datasource) == ["shop", "test"]


def test_only_the_canonical_module_hardcodes_the_list() -> None:
    offenders = [
        path.relative_to(APP_DIR).as_posix()
        for path in sorted(APP_DIR.rglob("*.py"))
        if path != CANONICAL
        and "performance_schema" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []