"""Token scope: four-level allowlist over datasource, database, table, and column.

Selecting a lower level must also include its parents:
- selecting a database requires selecting its datasource;
- selecting a table requires selecting its database;
- selecting a column requires selecting its table.
A table with no columns selected means all columns of that table are allowed.

``unrestricted_tables`` (all tables of a datasource) and ``unrestricted_databases`` (all tables of a
database) are optional explicit states:
- legacy records lack ``unrestricted_databases`` (``None``): keep historical semantics;
- new records carry the field (possibly an empty set): database scope is decided only by
  "whole-database grant / table entries / datasource-wide grant", and an empty ``tables`` no longer
  means the whole database.
This does not change the allowlist-only (never blocklist) security model; it turns "all tables"
from an implicit convention into an explicit state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.errors import ValidationError
from app.i18n import t, translate_label

NAME_LIMIT = 128
MAX_DATASOURCES = 1000
MAX_TABLES = 10000
MAX_COLUMNS = 50000
MAX_UNRESTRICTED_DATABASES = 512


@dataclass(frozen=True, order=True)
class TableRef:
    datasource_id: int
    database: str
    table: str


@dataclass(frozen=True, order=True)
class ColumnRef:
    datasource_id: int
    database: str
    table: str
    column: str


@dataclass(frozen=True)
class TokenScope:
    datasources: frozenset[int] = frozenset()
    tables: frozenset[TableRef] = frozenset()
    columns: frozenset[ColumnRef] = frozenset()
    unrestricted_tables: frozenset[int] | None = None
    unrestricted_databases: frozenset[tuple[int, str]] | None = None
    _datasource_ids: frozenset[int] = field(init=False, repr=False, compare=False)
    _table_keys: frozenset[tuple[int, str, str]] = field(init=False, repr=False, compare=False)
    _column_keys: frozenset[tuple[int, str, str, str]] = field(
        init=False, repr=False, compare=False
    )
    _unrestricted_database_keys: frozenset[tuple[int, str]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_datasource_ids", frozenset(self.datasources))
        object.__setattr__(
            self,
            "_unrestricted_database_keys",
            frozenset(self.unrestricted_databases or ()),
        )
        object.__setattr__(
            self,
            "_table_keys",
            frozenset(
                (item.datasource_id, item.database, item.table) for item in self.tables
            ),
        )
        object.__setattr__(
            self,
            "_column_keys",
            frozenset(
                (
                    item.datasource_id,
                    item.database,
                    item.table,
                    item.column.lower(),
                )
                for item in self.columns
            ),
        )

    @property
    def restricted(self) -> bool:
        return bool(
            self.datasources
            or self.tables
            or self.columns
            or self.unrestricted_tables
            or self.unrestricted_databases
        )

    @property
    def effective_datasource_ids(self) -> frozenset[int]:
        if self.datasources:
            return self._datasource_ids
        owners = {item.datasource_id for item in self.tables}
        owners.update(item.datasource_id for item in self.columns)
        owners.update(self.unrestricted_tables or ())
        owners.update(datasource_id for datasource_id, _ in self.unrestricted_databases or ())
        return frozenset(owners)

    def allows_datasource(self, datasource_id: int) -> bool:
        if self.datasources:
            return datasource_id in self._datasource_ids
        if self.tables or self.columns or self.unrestricted_tables:
            return datasource_id in self.effective_datasource_ids
        return True

    def table_scope(
        self, datasource_id: int
    ) -> tuple[frozenset[str], frozenset[tuple[str, str]]] | None:
        """Return (whole-database grants, explicit tables) for this datasource; None means all tables.

        Both empty means the datasource has no table permission at all. Whole-database grants must
        be returned separately; otherwise a caller filtering only by explicit tables would treat a
        fully granted database as unauthorized.
        """
        if not self.allows_datasource(datasource_id):
            return frozenset(), frozenset()
        pairs = frozenset(
            (item.database, item.table)
            for item in self.tables
            if item.datasource_id == datasource_id
        )
        # Without a datasource allowlist, keep historical semantics; legacy records use unrestricted_tables=None.
        if self.unrestricted_tables is None or not self.datasources:
            if not self.tables:
                return None
            return frozenset(), pairs
        if datasource_id in self.unrestricted_tables:
            return None
        databases = frozenset(
            database
            for owner_id, database in self._unrestricted_database_keys
            if owner_id == datasource_id
        )
        return databases, pairs

    def table_pairs(self, datasource_id: int) -> frozenset[tuple[str, str]] | None:
        """Return the allowed (database, table) set for this datasource; None means all tables.

        Whole-database grants are not included; use `table_scope` when the database dimension matters.
        """
        scope = self.table_scope(datasource_id)
        return None if scope is None else scope[1]

    def table_names(self, datasource_id: int, database: str) -> frozenset[str] | None:
        """Return the allowed table names in the given database; None means all tables there, an empty set means none."""
        if not self.allows_database(datasource_id, database):
            return frozenset()
        if (
            (datasource_id, database) in self._unrestricted_database_keys
            or datasource_id in (self.unrestricted_tables or ())
        ):
            return None
        pairs = self.table_pairs(datasource_id)
        if pairs is None:
            return None
        return frozenset(table for db, table in pairs if db == database)

    def _has_entries(self, datasource_id: int, database: str) -> bool:
        return any(
            item.datasource_id == datasource_id and item.database == database
            for item in self.tables
        ) or any(
            item.datasource_id == datasource_id and item.database == database
            for item in self.columns
        )

    def allows_database(self, datasource_id: int, database: str) -> bool:
        """Four-level check of whether this database is authorized; the caller validates exposure first.

        Order: whole-database grant -> table/column entries under this database -> datasource-wide
        grant (every exposed database of the datasource) -> legacy payload compatibility.
        """
        if not self.allows_datasource(datasource_id):
            return False
        if (datasource_id, database) in self._unrestricted_database_keys:
            return True
        if self._has_entries(datasource_id, database):
            return True
        if datasource_id in (self.unrestricted_tables or ()):
            return True
        if self.unrestricted_databases is not None:
            return False
        # Legacy payloads have no database level: keep "empty means all" only when no table entries exist at all.
        return self.unrestricted_tables is None and not self.tables

    def database_state(self, datasource_id: int, database: str) -> str:
        """Return this database's authorization state: all / partial / none."""
        if not self.allows_datasource(datasource_id):
            return "none"
        if (datasource_id, database) in self._unrestricted_database_keys:
            return "all"
        if datasource_id in (self.unrestricted_tables or ()):
            return "all"
        if (
            self.unrestricted_databases is None
            and self.unrestricted_tables is None
            and not self.tables
        ):
            return "all"
        return "partial" if self._has_entries(datasource_id, database) else "none"

    def allows_table(self, datasource_id: int, database: str, table: str) -> bool:
        if not self.allows_datasource(datasource_id):
            return False
        if not self.allows_database(datasource_id, database):
            return False
        if (
            (datasource_id, database) in self._unrestricted_database_keys
            or datasource_id in (self.unrestricted_tables or ())
        ):
            return True
        if self.unrestricted_databases is not None:
            return (datasource_id, database, table) in self._table_keys
        if self.unrestricted_tables is None:
            return not self.tables or (datasource_id, database, table) in self._table_keys
        return (datasource_id, database, table) in self._table_keys

    def _selected_columns(
        self, datasource_id: int, database: str, table: str
    ) -> frozenset[str]:
        prefix = (datasource_id, database, table)
        return frozenset(key[3] for key in self._column_keys if key[:3] == prefix)

    def allows_column(
        self, datasource_id: int, database: str, table: str, column: str
    ) -> bool:
        if not self.allows_table(datasource_id, database, table):
            return False
        if not self.columns:
            return True
        selected = self._selected_columns(datasource_id, database, table)
        # Column grants apply per table: a table with no column records means all its columns.
        if not selected:
            return True
        return column.lower() in selected

    def allowed_columns(
        self, datasource_id: int, database: str, table: str
    ) -> frozenset[str] | None:
        """Return allowed lower-case column names for this table; None means the table's columns are unrestricted."""
        if not self.allows_table(datasource_id, database, table):
            return frozenset()
        if not self.columns:
            return None
        selected = self._selected_columns(datasource_id, database, table)
        return selected or None

    def summary(self) -> str:
        if 0 in self.datasources:
            return t("scope.summary.broken")
        if not self.restricted:
            return t("scope.summary.all")
        parts: list[str] = []
        parts.append(
            t("scope.summary.datasources", n=len(self.datasources))
            if self.datasources
            else t("scope.summary.datasources_all")
        )
        if self.unrestricted_tables is not None:
            all_count = len(self.unrestricted_tables)
            explicit_count = len(self.tables)
            if all_count and explicit_count:
                parts.append(
                    t(
                        "scope.summary.tables_all_and_explicit",
                        n=all_count,
                        m=explicit_count,
                    )
                )
            elif all_count:
                parts.append(t("scope.summary.tables_all_datasources", n=all_count))
            else:
                parts.append(t("scope.summary.tables", n=explicit_count))
        else:
            parts.append(
                t("scope.summary.tables", n=len(self.tables))
                if self.tables
                else t("scope.summary.tables_all")
            )
        if self.unrestricted_databases:
            parts.append(
                t("scope.summary.whole_databases", n=len(self.unrestricted_databases))
            )
        parts.append(
            t("scope.summary.columns", n=len(self.columns))
            if self.columns
            else t("scope.summary.columns_all")
        )
        return " · ".join(parts)

    def to_dict(self) -> dict:
        data = {
            "datasources": sorted(self.datasources),
            "tables": [
                [item.datasource_id, item.database, item.table]
                for item in sorted(self.tables)
            ],
            "columns": [
                [item.datasource_id, item.database, item.table, item.column]
                for item in sorted(self.columns)
            ],
        }
        if self.unrestricted_tables is not None:
            data["unrestricted_tables"] = sorted(self.unrestricted_tables)
        if self.unrestricted_databases is not None:
            data["unrestricted_databases"] = [
                [datasource_id, database]
                for datasource_id, database in sorted(self.unrestricted_databases)
            ]
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: object) -> "TokenScope":
        if not isinstance(data, dict):
            raise ValueError("授权范围格式错误")
        raw_datasources = data.get("datasources") or []
        raw_tables = data.get("tables") or []
        raw_columns = data.get("columns") or []
        raw_unrestricted = data.get("unrestricted_tables")
        raw_unrestricted_databases = data.get("unrestricted_databases")
        if not isinstance(raw_datasources, list):
            raise ValueError("数据源授权格式错误")
        if not isinstance(raw_tables, list) or not isinstance(raw_columns, list):
            raise ValueError("表或字段授权格式错误")
        if raw_unrestricted is not None and not isinstance(raw_unrestricted, list):
            raise ValueError("全部表授权格式错误")
        if raw_unrestricted_databases is not None and not isinstance(
            raw_unrestricted_databases, list
        ):
            raise ValueError("库级全部表授权格式错误")

        datasources: set[int] = set()
        for value in raw_datasources:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("数据源授权格式错误")
            datasources.add(value)

        unrestricted: set[int] | None = None
        if raw_unrestricted is not None:
            unrestricted = set()
            for value in raw_unrestricted:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError("全部表授权格式错误")
                unrestricted.add(value)

        unrestricted_databases: set[tuple[int, str]] | None = None
        if raw_unrestricted_databases is not None:
            unrestricted_databases = set()
            for value in raw_unrestricted_databases:
                if (
                    not isinstance(value, Sequence)
                    or isinstance(value, str)
                    or len(value) != 2
                ):
                    raise ValueError("库级全部表授权格式错误")
                ds_id, database = value
                if isinstance(ds_id, bool) or not isinstance(ds_id, int) or ds_id <= 0:
                    raise ValueError("库级全部表授权格式错误")
                if not isinstance(database, str):
                    raise ValueError("库级全部表授权格式错误")
                name = database.strip()
                if not name or len(name) > NAME_LIMIT:
                    raise ValueError("库级全部表授权格式错误")
                unrestricted_databases.add((ds_id, name))

        tables: set[TableRef] = set()
        for value in raw_tables:
            if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 3:
                raise ValueError("表授权格式错误")
            ds_id, database, table = value
            if isinstance(ds_id, bool) or not isinstance(ds_id, int) or ds_id <= 0:
                raise ValueError("表授权格式错误")
            if not isinstance(database, str) or not isinstance(table, str):
                raise ValueError("表授权格式错误")
            tables.add(TableRef(ds_id, database, table))

        columns: set[ColumnRef] = set()
        for value in raw_columns:
            if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
                raise ValueError("字段授权格式错误")
            ds_id, database, table, column = value
            if isinstance(ds_id, bool) or not isinstance(ds_id, int) or ds_id <= 0:
                raise ValueError("字段授权格式错误")
            if not all(isinstance(part, str) for part in (database, table, column)):
                raise ValueError("字段授权格式错误")
            columns.add(ColumnRef(ds_id, database, table, column))

        return build_scope(
            datasources, tables, columns, unrestricted, unrestricted_databases
        )

    @classmethod
    def from_json(cls, raw: str) -> "TokenScope":
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("授权范围 JSON 无法解析") from exc
        return cls.from_dict(data)


UNRESTRICTED = TokenScope()

# Datasource IDs start at 1; 0 is a "corrupt config" sentinel matching no real datasource.
BROKEN_SCOPE = TokenScope(datasources=frozenset({0}))


def _clean_name(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(t("error.field.invalid", field=translate_label(label)))
    text = value.strip()
    if not text or len(text) > NAME_LIMIT:
        raise ValidationError(
            t("error.field.required", field=translate_label(label), limit=NAME_LIMIT)
        )
    return text


def build_scope(
    datasources: Iterable[int],
    tables: Iterable[TableRef],
    columns: Iterable[ColumnRef],
    unrestricted_tables: Iterable[int] | None = None,
    unrestricted_databases: Iterable[Sequence] | None = None,
) -> TokenScope:
    """Deduplicate and validate the four-level hierarchy; an empty set means that level is unrestricted (legacy payload only).

    ``unrestricted_databases`` = None keeps historical semantics; any other value enables the
    three states "whole database / explicit tables / no table permission".
    """
    ds_ids = {item for item in datasources}
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ds_ids):
        raise ValidationError("数据源授权格式错误")

    all_table_ids: set[int] | None = None
    if unrestricted_tables is not None:
        all_table_ids = {item for item in unrestricted_tables}
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in all_table_ids
        ):
            raise ValidationError("全部表授权格式错误")
        if all_table_ids and not ds_ids:
            raise ValidationError("授权全部表之前必须先选择数据源")
        if not all_table_ids.issubset(ds_ids):
            raise ValidationError("全部表授权的数据源不在已选择的数据源中")

    all_database_keys: set[tuple[int, str]] | None = None
    if unrestricted_databases is not None:
        all_database_keys = set()
        for item in unrestricted_databases:
            if (
                not isinstance(item, Sequence)
                or isinstance(item, str)
                or len(item) != 2
            ):
                raise ValidationError("库级全部表授权格式错误")
            datasource_id, database = item
            if (
                isinstance(datasource_id, bool)
                or not isinstance(datasource_id, int)
                or datasource_id <= 0
            ):
                raise ValidationError("库级全部表授权格式错误")
            all_database_keys.add((datasource_id, _clean_name(database, "库名")))
        if len(all_database_keys) > MAX_UNRESTRICTED_DATABASES:
            raise ValidationError(
                t("error.scope.whole_database_limit", limit=MAX_UNRESTRICTED_DATABASES)
            )
        if all_database_keys and not ds_ids:
            raise ValidationError("授权库级全部表之前必须先选择数据源")
        for datasource_id, _ in all_database_keys:
            if datasource_id not in ds_ids:
                raise ValidationError("库级全部表的数据源不在已选择的数据源中")
            if all_table_ids is not None and datasource_id in all_table_ids:
                raise ValidationError("该数据源已授权全部表，不能再指定库")

    table_refs = {
        TableRef(item.datasource_id, _clean_name(item.database, "库名"), _clean_name(item.table, "表名"))
        for item in tables
    }
    column_refs = {
        ColumnRef(
            item.datasource_id,
            _clean_name(item.database, "库名"),
            _clean_name(item.table, "表名"),
            _clean_name(item.column, "字段名"),
        )
        for item in columns
    }

    if len(ds_ids) > MAX_DATASOURCES:
        raise ValidationError(t("error.scope.datasource_limit", limit=MAX_DATASOURCES))
    if len(table_refs) > MAX_TABLES:
        raise ValidationError(t("error.scope.table_limit", limit=MAX_TABLES))
    if len(column_refs) > MAX_COLUMNS:
        raise ValidationError(t("error.scope.column_limit", limit=MAX_COLUMNS))

    if table_refs and not ds_ids:
        raise ValidationError("选择表之前必须先选择数据源")
    if column_refs and not table_refs:
        raise ValidationError("选择字段之前必须先选择表")

    table_keys = {(item.datasource_id, item.database, item.table) for item in table_refs}
    for table in table_refs:
        if table.datasource_id not in ds_ids:
            raise ValidationError("表不属于已选择的数据源")
        if all_table_ids is not None and table.datasource_id in all_table_ids:
            raise ValidationError("该数据源已授权全部表，不能再指定具体表")
        if (
            all_database_keys is not None
            and (table.datasource_id, table.database) in all_database_keys
        ):
            raise ValidationError("该库已授权全部表，不能再指定具体表")
    for column in column_refs:
        if (column.datasource_id, column.database, column.table) not in table_keys:
            raise ValidationError("字段不属于已选择的表")
        if all_table_ids is not None and column.datasource_id in all_table_ids:
            raise ValidationError("该数据源已授权全部表，不能再指定字段")
        if (
            all_database_keys is not None
            and (column.datasource_id, column.database) in all_database_keys
        ):
            raise ValidationError("该库已授权全部表，不能再指定字段")

    return TokenScope(
        datasources=frozenset(ds_ids),
        tables=frozenset(table_refs),
        columns=frozenset(column_refs),
        unrestricted_tables=(
            frozenset(all_table_ids) if all_table_ids is not None else None
        ),
        unrestricted_databases=(
            frozenset(all_database_keys) if all_database_keys is not None else None
        ),
    )
