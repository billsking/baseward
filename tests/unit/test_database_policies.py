"""Exposed databases and per-database policy: fake session_factory, no MySQL."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.datasources.databases import (
    DatabasePolicy,
    available_databases,
    effective_read_only,
    list_exposed,
    load_policies,
    load_stored_read_only,
    narrowing_impact,
    read_only_summary,
    sync_on_save,
    use_statement,
)
from app.db.models import DataSourceDatabase
from app.errors import ValidationError
from app.tokens.scope import TableRef, TokenScope

_NOW = datetime(2026, 11, 5, 1, 2, 3)


class FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class FakeSession:
    def __init__(self, db: "FakeFactory") -> None:
        self._db = db

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement):
        self._db.statements.append(statement)
        rows = self._db.row_sets.pop(0) if self._db.row_sets else []
        return FakeResult(rows)

    def add(self, obj) -> None:
        self._db.added.append(obj)

    async def delete(self, obj) -> None:
        self._db.deleted.append(obj)

    async def commit(self) -> None:
        self._db.commits += 1


class FakeFactory:
    def __init__(self) -> None:
        self.row_sets: list[list] = []
        self.statements: list = []
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    def __call__(self) -> FakeSession:
        return FakeSession(self)


def _row(name: str, is_read_only: int) -> DataSourceDatabase:
    return DataSourceDatabase(
        datasource_id=7,
        name=name,
        is_read_only=is_read_only,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_list_exposed_returns_names_in_order() -> None:
    factory = FakeFactory()
    factory.row_sets = [[("main",), ("report",)]]
    assert await list_exposed(factory, 7) == ["main", "report"]
    params = list(factory.statements[0].compile().params.values())
    assert 7 in params


async def test_list_exposed_empty() -> None:
    assert await list_exposed(FakeFactory(), 7) == []


async def test_load_policies_merges_datasource_switch() -> None:
    factory = FakeFactory()
    factory.row_sets = [[("main", 0, 0), ("report", 1, 0)]]
    policies = await load_policies(factory, 7)
    assert policies == [
        DatabasePolicy("main", False),
        DatabasePolicy("report", True),
    ]

    readonly = FakeFactory()
    readonly.row_sets = [[("main", 0, 1), ("report", 0, 1)]]
    assert all(p.is_read_only for p in await load_policies(readonly, 7))
    params = list(readonly.statements[0].compile().params.values())
    assert 7 in params


async def test_load_stored_read_only_keeps_raw_values() -> None:
    factory = FakeFactory()
    factory.row_sets = [[("main", 0), ("report", 1)]]
    assert await load_stored_read_only(factory, 7) == {
        "main": False,
        "report": True,
    }
    params = list(factory.statements[0].compile().params.values())
    assert 7 in params


def test_effective_read_only_is_datasource_and_database() -> None:
    check = effective_read_only(
        [DatabasePolicy("main", False), DatabasePolicy("report", True)]
    )
    assert check("main") is False
    assert check("report") is True
    assert check("unknown") is True


def test_available_databases_intersects_exposed_and_scope() -> None:
    policies = [
        DatabasePolicy("main", True),
        DatabasePolicy("report", False),
        DatabasePolicy("archive", True),
    ]
    scope = TokenScope(tables=frozenset({TableRef(7, "report", "orders")}))
    assert [p.name for p in available_databases(
        ["main", "report", "archive"], policies, scope, 7
    )] == ["report"]
    assert available_databases([], policies, scope, 7) == []


@pytest.mark.parametrize("database", ["", "a`b", "a\x00b", "x" * 129])
def test_use_statement_rejects_illegal_names(database: str) -> None:
    with pytest.raises(ValidationError):
        use_statement(database)


def test_use_statement_quotes_valid_name() -> None:
    assert use_statement("report") == "USE `report`"


async def test_narrowing_impact_counts_three_dimensions() -> None:
    factory = FakeFactory()
    factory.row_sets = [
        [
            (1, '{"datasources":[7],"tables":[[7,"report","orders"]],"columns":[]}'),
            (2, '{"datasources":[7],"tables":[[7,"main","orders"]],"columns":[]}'),
        ],
        [(11,), (12,)],
    ]
    assert await narrowing_impact(factory, 7, ["report"]) == {
        "tokens": 1,
        "credentials": 2,
        "databases": 1,
    }


async def test_narrowing_impact_without_removed_databases_skips_queries() -> None:
    factory = FakeFactory()
    assert await narrowing_impact(factory, 7, []) == {
        "tokens": 0,
        "credentials": 0,
        "databases": 0,
    }
    assert factory.statements == []


def test_read_only_summary_texts() -> None:
    assert read_only_summary([DatabasePolicy("main", True)]) == "全部只读"
    assert (
        read_only_summary(
            [DatabasePolicy("main", False), DatabasePolicy("report", True)]
        )
        == "可写 1 个库"
    )


async def test_sync_on_save_adds_keeps_and_deletes() -> None:
    factory = FakeFactory()
    factory.row_sets = [[_row("main", 0), _row("legacy", 1)]]

    await sync_on_save(factory, 7, ["main", "report"], "main")

    assert factory.commits == 1
    assert [row.name for row in factory.deleted] == ["legacy"]
    assert len(factory.added) == 1
    added = factory.added[0]
    assert added.name == "report"
    assert added.is_read_only == 1
    assert added.datasource_id == 7


async def test_sync_on_save_rejects_default_database_outside_selection() -> None:
    with pytest.raises(ValidationError):
        await sync_on_save(FakeFactory(), 7, ["report"], "main")
