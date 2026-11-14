"""Metadata lookup: keyword tokenization, relevance scoring, and information_schema query builders."""

from __future__ import annotations

import re
from typing import Any, Sequence

from app.datasources.databases import exclude_system_schemas

MAX_TERMS = 5
FETCH_LIMIT = 1001
LIKE_ESCAPE = " ESCAPE '!'"


def tokenize(pattern: str | None) -> list[str]:
    if not pattern:
        return []
    terms = [term for term in re.split(r"\s+", pattern.strip()) if term]
    return terms[:MAX_TERMS]


def like_pattern(value: str) -> str:
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def score(
    terms: Sequence[str], name: str, comment: str = "", *, table_name: str = ""
) -> int:
    """Exact name > prefix > substring > comment; tables matched by column name get an extra table-name weight."""
    total = 0
    name_low = (name or "").lower()
    table_low = (table_name or "").lower()
    comment_low = (comment or "").lower()
    for term in terms:
        needle = term.lower()
        if name_low == needle:
            total += 100
        elif name_low.startswith(needle):
            total += 50
        elif needle in name_low:
            total += 20
        if table_low and table_low != name_low:
            if table_low == needle:
                total += 40
            elif needle in table_low:
                total += 10
        if needle in comment_low:
            total += 5
    return total


def resolve_alias(query: str, candidates: Sequence[Any]) -> tuple[Any | None, list[Any]]:
    """Fuzzy match by name/description; return the unique hit or tied candidates (empty list = no match)."""
    needle = (query or "").strip().casefold()
    if not needle:
        return None, []
    scored: list[tuple[int, Any]] = []
    for item in candidates:
        name = (getattr(item, "name", "") or "").casefold()
        remark = (getattr(item, "remark", "") or "").casefold()
        value = 0
        if name == needle:
            value += 1000
        elif needle in name:
            value += 200
        elif name and name in needle:
            value += 120
        if remark:
            if remark == needle:
                value += 800
            elif needle in remark:
                value += 150
            elif remark in needle:
                value += 100
        if value:
            scored.append((value, item))
    if not scored:
        return None, []
    best = max(value for value, _ in scored)
    top = [item for value, item in scored if value == best]
    if len(top) == 1:
        return top[0], []
    return None, top


def _table_scope_clause(
    alias: str,
    allowed_tables: Sequence[tuple[str, str]] | None,
    unrestricted_databases: Sequence[str] = (),
) -> tuple[str, list]:
    """Table-level authorization filter: explicit tables plus whole-database grants; None means every table of the datasource."""
    if allowed_tables is None:
        return "", []
    conditions: list[str] = []
    params: list = []
    if unrestricted_databases:
        placeholders = ",".join(["%s"] * len(unrestricted_databases))
        conditions.append(f"{alias}.table_schema IN ({placeholders})")
        params.extend(unrestricted_databases)
    if allowed_tables:
        placeholders = ",".join(["(%s,%s)"] * len(allowed_tables))
        conditions.append(
            f"({alias}.table_schema, {alias}.table_name) IN ({placeholders})"
        )
        for database, table in allowed_tables:
            params.extend([database, table])
    if not conditions:
        return " AND 1=0", []
    return " AND (" + " OR ".join(conditions) + ")", params


def _column_scope_clause(column_scope) -> tuple[str, list]:
    """Column-level authorization filter: (all-column tables, explicit columns, whole-database grants); None means unrestricted."""
    if column_scope is None:
        return "", []
    all_fields, explicit, unrestricted_databases = column_scope
    conditions: list[str] = []
    params: list = []
    if all_fields:
        conditions.append(
            "(c.table_schema, c.table_name) IN ("
            + ",".join(["(%s,%s)"] * len(all_fields))
            + ")"
        )
        for database, table in all_fields:
            params.extend([database, table])
    if explicit:
        conditions.append(
            "(c.table_schema, c.table_name, c.column_name) IN ("
            + ",".join(["(%s,%s,%s)"] * len(explicit))
            + ")"
        )
        for database, table, column in explicit:
            params.extend([database, table, column])
    if unrestricted_databases:
        placeholders = ",".join(["%s"] * len(unrestricted_databases))
        conditions.append(f"c.table_schema IN ({placeholders})")
        params.extend(unrestricted_databases)
    if not conditions:
        return " AND 1=0", []
    return " AND (" + " OR ".join(conditions) + ")", params


def _term_clause(terms: Sequence[str], columns: Sequence[str]) -> tuple[str, list]:
    sql = ""
    params: list = []
    for term in terms:
        group = " OR ".join(f"{column} LIKE %s{LIKE_ESCAPE}" for column in columns)
        sql += f" AND ({group})"
        params.extend([like_pattern(term)] * len(columns))
    return sql, params


def _database_clause(
    column: str, database: str | Sequence[str] | None
) -> tuple[str, list]:
    """Equality for one database, parameterized IN for several; an empty list is always false so it never degrades into no filter."""
    if database is None:
        return "", []
    if isinstance(database, str):
        if not database:
            return "", []
        return f" AND {column} = %s", [database]
    names = list(database)
    if not names:
        return " AND 1=0", []
    placeholders = ",".join(["%s"] * len(names))
    return f" AND {column} IN ({placeholders})", names


def search_tables_query(
    terms: Sequence[str],
    database: str | Sequence[str] | None,
    allowed_tables: Sequence[tuple[str, str]] | None,
    *,
    unrestricted_databases: Sequence[str] = (),
    limit: int = FETCH_LIMIT,
) -> tuple[str, list] | None:
    if allowed_tables is not None and not allowed_tables and not unrestricted_databases:
        return None
    sql = (
        "SELECT t.table_schema, t.table_name, COALESCE(t.table_comment, '') "
        "FROM information_schema.tables t "
        f"WHERE {exclude_system_schemas('t.table_schema')}"
    )
    params: list = []
    database_sql, database_params = _database_clause("t.table_schema", database)
    sql += database_sql
    params.extend(database_params)
    scope_sql, scope_params = _table_scope_clause(
        "t", allowed_tables, unrestricted_databases
    )
    sql += scope_sql
    params.extend(scope_params)
    term_sql, term_params = _term_clause(
        terms, ["t.table_schema", "t.table_name", "t.table_comment"]
    )
    sql += term_sql
    params.extend(term_params)
    sql += " ORDER BY t.table_schema, t.table_name LIMIT %s"
    params.append(limit)
    return sql, params


def search_columns_query(
    terms: Sequence[str],
    database: str | Sequence[str] | None,
    allowed_tables: Sequence[tuple[str, str]] | None,
    column_scope,
    *,
    unrestricted_databases: Sequence[str] = (),
    limit: int = FETCH_LIMIT,
) -> tuple[str, list] | None:
    if allowed_tables is not None and not allowed_tables and not unrestricted_databases:
        return None
    sql = (
        "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, "
        "COALESCE(c.column_comment, ''), c.ordinal_position "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        f"WHERE {exclude_system_schemas('c.table_schema')}"
    )
    params: list = []
    database_sql, database_params = _database_clause("c.table_schema", database)
    sql += database_sql
    params.extend(database_params)
    scope_sql, scope_params = _table_scope_clause(
        "c", allowed_tables, unrestricted_databases
    )
    sql += scope_sql
    params.extend(scope_params)
    column_sql, column_params = _column_scope_clause(column_scope)
    sql += column_sql
    params.extend(column_params)
    term_sql, term_params = _term_clause(
        terms,
        [
            "c.table_schema",
            "c.table_name",
            "c.column_name",
            "c.column_comment",
            "t.table_comment",
        ],
    )
    sql += term_sql
    params.extend(term_params)
    sql += " ORDER BY c.table_schema, c.table_name, c.ordinal_position LIMIT %s"
    params.append(limit)
    return sql, params


def describe_column_tables_query(
    terms: Sequence[str],
    database: str | Sequence[str] | None,
    allowed_tables: Sequence[tuple[str, str]] | None,
    *,
    unrestricted_databases: Sequence[str] = (),
    limit: int = FETCH_LIMIT,
) -> tuple[str, list] | None:
    """Reverse-lookup the owning table when a keyword hits a column name/comment or a table comment."""
    if not terms:
        return None
    if allowed_tables is not None and not allowed_tables and not unrestricted_databases:
        return None
    sql = (
        "SELECT DISTINCT c.table_schema, c.table_name, "
        "COALESCE(t.table_comment, '') "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        f"WHERE {exclude_system_schemas('c.table_schema')}"
    )
    params: list = []
    database_sql, database_params = _database_clause("c.table_schema", database)
    sql += database_sql
    params.extend(database_params)
    scope_sql, scope_params = _table_scope_clause(
        "c", allowed_tables, unrestricted_databases
    )
    sql += scope_sql
    params.extend(scope_params)
    term_sql, term_params = _term_clause(
        terms,
        [
            "c.table_schema",
            "c.table_name",
            "c.column_name",
            "c.column_comment",
            "t.table_comment",
        ],
    )
    sql += term_sql
    params.extend(term_params)
    sql += " ORDER BY c.table_schema, c.table_name LIMIT %s"
    params.append(limit)
    return sql, params


def columns_for_tables_query(
    table_refs: Sequence[tuple[str, str]],
    column_scope,
    *,
    limit: int,
) -> tuple[str, list] | None:
    if not table_refs:
        return None
    refs = list(dict.fromkeys(table_refs))
    ref_set = set(refs)
    placeholders = ",".join(["(%s,%s)"] * len(refs))
    params: list = []
    for database, table in refs:
        params.extend([database, table])
    sql = (
        "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, "
        "c.is_nullable, c.column_key, COALESCE(c.column_comment, ''), "
        "c.ordinal_position "
        "FROM information_schema.columns c "
        f"WHERE (c.table_schema, c.table_name) IN ({placeholders})"
    )
    if column_scope is not None:
        all_fields, explicit, unrestricted_databases = column_scope
        visible_all = [ref for ref in all_fields if ref in ref_set]
        visible_explicit = [
            item for item in explicit if (item[0], item[1]) in ref_set
        ]
        visible_databases = sorted({ref[0] for ref in refs} & set(unrestricted_databases))
        conditions: list[str] = []
        if visible_all:
            conditions.append(
                "(c.table_schema, c.table_name) IN ("
                + ",".join(["(%s,%s)"] * len(visible_all))
                + ")"
            )
            for database, table in visible_all:
                params.extend([database, table])
        if visible_explicit:
            conditions.append(
                "(c.table_schema, c.table_name, c.column_name) IN ("
                + ",".join(["(%s,%s,%s)"] * len(visible_explicit))
                + ")"
            )
            for database, table, column in visible_explicit:
                params.extend([database, table, column])
        if visible_databases:
            conditions.append(
                "c.table_schema IN ("
                + ",".join(["%s"] * len(visible_databases))
                + ")"
            )
            params.extend(visible_databases)
        if not conditions:
            return None
        sql += " AND (" + " OR ".join(conditions) + ")"
    sql += " ORDER BY c.table_schema, c.table_name, c.ordinal_position LIMIT %s"
    params.append(limit)
    return sql, params
