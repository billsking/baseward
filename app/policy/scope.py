"""Enforce token scope on the SQL AST: table/column checks and wildcard rewriting.

Read information_schema only when the token has column grants; table-level checks do not depend
on metadata. Whenever column ownership cannot be determined, block — never let aliases or
correlated subqueries slip past the column allowlist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from sqlglot import exp
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tokens.scope import TokenScope

logger = logging.getLogger(__name__)

TABLE_DENIED = "TABLE_DENIED"
DATABASE_NOT_EXPOSED = "DATABASE_NOT_EXPOSED"
DATABASE_DENIED = "DATABASE_DENIED"
FIELD_DENIED = "FIELD_DENIED"
FIELD_AMBIGUOUS = "FIELD_AMBIGUOUS"
FIELD_SCOPE_CHECK_FAILED = "FIELD_SCOPE_CHECK_FAILED"
FIELDS_REQUIRED = "FIELDS_REQUIRED"


class ScopeViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Source:
    key: str
    physical: bool
    database: str = ""
    table: str = ""
    allowed: frozenset[str] | None = None
    columns: tuple[str, ...] | None = None

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(name.lower() for name in (self.columns or ()))


@dataclass
class _Frame:
    sources: list[_Source] = field(default_factory=list)
    by_key: dict[str, _Source] = field(default_factory=dict)
    aliases: frozenset[str] = frozenset()
    outer: "_Frame | None" = None

    def find(self, key: str) -> _Source | None:
        frame: _Frame | None = self
        while frame is not None:
            source = frame.by_key.get(key)
            if source is not None:
                return source
            frame = frame.outer
        return None


async def load_columns(
    engine: AsyncEngine, tables: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Load column names of the given tables (definition order); keys are lower-cased."""
    if not tables:
        return {}
    keys = sorted(tables)
    placeholders = ",".join("(%s,%s)" for _ in keys)
    params = [value for key in keys for value in key]
    sql = (
        "SELECT table_schema, table_name, column_name FROM information_schema.columns "
        f"WHERE (table_schema, table_name) IN ({placeholders}) "
        "ORDER BY table_schema, table_name, ordinal_position"
    )
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql(sql, tuple(params))).fetchall()
    collected: dict[tuple[str, str], list[str]] = {}
    for schema, table, column in rows:
        collected.setdefault((schema.lower(), table.lower()), []).append(column)
    return {key: tuple(value) for key, value in collected.items()}


class _Processor:
    def __init__(
        self,
        scope: TokenScope,
        datasource_id: int,
        default_database: str,
        metadata: Mapping[tuple[str, str], tuple[str, ...]],
        collect_only: bool,
        exposed_databases: frozenset[str] | None = None,
        datasource_name: str = "",
    ) -> None:
        self.scope = scope
        self.datasource_id = datasource_id
        self.default_database = default_database
        self.metadata = metadata
        self.collect_only = collect_only
        self.exposed_databases = exposed_databases
        self.datasource_name = datasource_name
        self.tables: set[tuple[str, str]] = set()
        self.rewritten = False

    def process(self, stmt: exp.Expression) -> None:
        self._process_statement(stmt)

    # ---------- Statement dispatch ----------

    def _process_statement(self, stmt: exp.Expression) -> None:
        if isinstance(stmt, (exp.Select, exp.Union)):
            self._process_query(stmt, {}, None)
        elif isinstance(stmt, exp.Update):
            # Write statements can also carry a top-level WITH: CTE bodies must be validated as queries;
            # otherwise they pass the allowlist as same-named physical tables and their SQL is unmanaged (C1).
            ctes = self._apply_with(stmt, {})
            frame = self._build_sources(stmt.this, ctes)
            if not self.collect_only:
                self._validate_node_columns(stmt, frame)
            self._process_statement_subqueries(
                stmt, frame, ("where", "order", "limit", "expressions"), ctes
            )
        elif isinstance(stmt, exp.Delete):
            ctes = self._apply_with(stmt, {})
            raw: list[tuple[exp.Expression | None, _Source]] = []
            if stmt.this is not None:
                self._add_source(stmt.this, ctes, raw)
            using = stmt.args.get("using")
            if isinstance(using, list):
                for item in using:
                    if isinstance(item, exp.Expression):
                        self._add_source(item, ctes, raw)
            elif isinstance(using, exp.Expression):
                self._add_source(using, ctes, raw)
            frame = self._finish_sources(raw, ctes)
            if not self.collect_only:
                self._validate_node_columns(stmt, frame)
            self._process_statement_subqueries(
                stmt, frame, ("where", "order", "limit"), ctes
            )
        elif isinstance(stmt, exp.Insert):
            self._process_insert(stmt)
        elif isinstance(stmt, exp.Describe):
            self._process_describe(stmt)

    # ---------- Common structures ----------

    def _process_query(
        self,
        node: exp.Expression,
        ctes: dict[str, tuple[str, ...] | None],
        outer: _Frame | None,
    ) -> tuple[str, ...] | None:
        if isinstance(node, exp.Subquery):
            return self._process_query(node.this, ctes, outer)
        if isinstance(node, exp.Union):
            local = self._apply_with(node, ctes)
            outputs = self._process_query(node.this, local, outer)
            self._process_query(node.expression, local, outer)
            return outputs
        if isinstance(node, exp.Select):
            return self._process_select(node, ctes, outer)
        return None

    def _apply_with(
        self, node: exp.Expression, ctes: dict[str, tuple[str, ...] | None]
    ) -> dict[str, tuple[str, ...] | None]:
        with_ = node.args.get("with_")
        if with_ is None:
            return ctes
        local = dict(ctes)
        recursive = bool(with_.args.get("recursive"))
        for cte in with_.expressions:
            name = cte.alias.lower()
            alias = cte.args.get("alias")
            declared = tuple(
                column.name for column in (alias.args.get("columns") or []) if column.name
            )
            body_ctes = dict(local)
            if recursive:
                body_ctes[name] = declared or None
            outputs = self._process_query(cte.this, body_ctes, None)
            local[name] = declared or outputs
        return local

    def _process_select(
        self,
        select: exp.Select,
        ctes: dict[str, tuple[str, ...] | None],
        outer: _Frame | None,
    ) -> tuple[str, ...] | None:
        local_ctes = self._apply_with(select, ctes)
        raw: list[tuple[exp.Expression | None, _Source]] = []
        from_ = select.args.get("from_")
        if from_ is not None:
            self._add_source(from_.this, local_ctes, raw)
        for join in select.args.get("joins") or []:
            self._add_source(join.this, local_ctes, raw)

        frame = _Frame(outer=outer, aliases=self._projection_aliases(select))
        for node, source in raw:
            frame.sources.append(source)
            if source.key:
                frame.by_key[source.key] = source
        for node, source in raw:
            if node is not None:
                source.columns = self._process_query(node, local_ctes, outer=frame)

        if not self.collect_only:
            self._rewrite_select(select, frame)
            self._validate_select_columns(select, frame)

        for key in ("expressions", "where", "group", "having", "order", "qualify", "limit", "offset"):
            value = select.args.get(key)
            if value is not None:
                self._walk_for_queries(value, local_ctes, frame)
        for join in select.args.get("joins") or []:
            for key in ("on", "using"):
                value = join.args.get(key)
                if value is not None:
                    self._walk_for_queries(value, local_ctes, frame)

        return self._output_columns(select)

    def _add_source(
        self,
        node: exp.Expression | None,
        ctes: dict[str, tuple[str, ...] | None],
        raw: list[tuple[exp.Expression | None, _Source]],
    ) -> None:
        if isinstance(node, exp.Table):
            key = (node.alias_or_name or node.name).lower()
            if not isinstance(node.this, exp.Identifier):
                raw.append((None, _Source(key=key, physical=False)))
                for join in node.args.get("joins") or []:
                    self._add_source(join.this, ctes, raw)
                return
            if not node.db and node.name.lower() in ctes:
                raw.append((None, _Source(key=key, physical=False, columns=ctes[node.name.lower()])))
            else:
                database = node.db or self.default_database
                self._require_table(database, node.name)
                self.tables.add((database, node.name))
                allowed = self.scope.allowed_columns(self.datasource_id, database, node.name)
                columns = self.metadata.get((database.lower(), node.name.lower()))
                raw.append(
                    (
                        None,
                        _Source(
                            key=key,
                            physical=True,
                            database=database,
                            table=node.name,
                            allowed=allowed,
                            columns=columns,
                        ),
                    )
                )
            for join in node.args.get("joins") or []:
                self._add_source(join.this, ctes, raw)
            return
        if isinstance(node, exp.Lateral):
            self._add_source(node.this, ctes, raw)
            return
        if isinstance(node, exp.Paren):
            self._add_source(node.this, ctes, raw)
            return
        if isinstance(node, exp.Subquery):
            key = (node.alias or "").lower()
            raw.append((node.this, _Source(key=key, physical=False)))
            return
        key = (getattr(node, "alias", "") or "").lower()
        raw.append((None, _Source(key=key, physical=False)))

    def _build_sources(
        self, root: exp.Expression | None, ctes: dict[str, tuple[str, ...] | None]
    ) -> _Frame:
        raw: list[tuple[exp.Expression | None, _Source]] = []
        if root is not None:
            self._add_source(root, ctes, raw)
        return self._finish_sources(raw, ctes)

    def _finish_sources(
        self,
        raw: list[tuple[exp.Expression | None, _Source]],
        ctes: dict[str, tuple[str, ...] | None],
    ) -> _Frame:
        frame = _Frame()
        for node, source in raw:
            frame.sources.append(source)
            if source.key:
                frame.by_key[source.key] = source
        for node, source in raw:
            if node is not None:
                source.columns = self._process_query(node, ctes, outer=frame)
        return frame

    def _require_table(self, database: str, table: str) -> None:
        if (
            self.exposed_databases is not None
            and database not in self.exposed_databases
        ):
            if self.datasource_name:
                message = (
                    f"数据库 {database} 未在数据源 {self.datasource_name} "
                    "的可访问范围内"
                )
            else:
                message = f"数据库 {database} 未在可访问范围内"
            raise ScopeViolation(DATABASE_NOT_EXPOSED, message)
        if not self.scope.allows_database(self.datasource_id, database):
            raise ScopeViolation(DATABASE_DENIED, f"当前 Token 无权访问库：{database}")
        if not self.scope.allows_table(self.datasource_id, database, table):
            qualified = f"{database}.{table}" if database else table
            raise ScopeViolation(TABLE_DENIED, f"当前 Token 无权访问表：{qualified}")

    # ---------- Wildcard rewriting ----------

    def _rewrite_select(self, select: exp.Select, frame: _Frame) -> None:
        projections: list[exp.Expression] = []
        changed = False
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                expanded = self._expand_bare_star(frame)
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                expanded = self._expand_qualified_star(projection, frame)
            else:
                expanded = None
            if expanded is None:
                projections.append(projection)
            else:
                projections.extend(expanded)
                changed = True
        if changed:
            select.set("expressions", projections)
            self.rewritten = True

    def _expand_bare_star(self, frame: _Frame) -> list[exp.Expression] | None:
        restricted = [source for source in frame.sources if source.physical and source.allowed is not None]
        if not restricted:
            return None
        if not frame.sources:
            return None
        expanded: list[exp.Expression] = []
        for source in frame.sources:
            if source.physical and source.allowed is not None:
                expanded.extend(self._allowed_columns(source))
            elif source.key:
                expanded.append(_qualified_star(source.key))
            else:
                raise ScopeViolation(
                    FIELD_SCOPE_CHECK_FAILED, "无法确定子查询的输出字段，查询已阻断"
                )
        return expanded

    def _expand_qualified_star(
        self, column: exp.Column, frame: _Frame
    ) -> list[exp.Expression] | None:
        qualifier = (column.table or "").lower()
        source = frame.find(qualifier) if qualifier else None
        if source is None:
            raise ScopeViolation(
                FIELD_SCOPE_CHECK_FAILED, f"无法确定字段所属表：{column.sql(dialect='mysql')}"
            )
        if not source.physical or source.allowed is None:
            return None
        return self._allowed_columns(source)

    def _allowed_columns(self, source: _Source) -> list[exp.Expression]:
        allowed = source.allowed or frozenset()
        names: list[str] = []
        if source.columns:
            known = {name.lower() for name in source.columns}
            names.extend(name for name in source.columns if name.lower() in allowed)
            names.extend(sorted(allowed - known))
        else:
            names.extend(sorted(allowed))
        return [
            exp.column(name, table=source.key or None, quoted=True) for name in names
        ]

    # ---------- Column validation ----------

    def _validate_select_columns(self, select: exp.Select, frame: _Frame) -> None:
        if not self.scope.columns:
            return
        for column in select.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            if column.find_ancestor(exp.Select) is not select:
                continue
            if (
                frame.aliases
                and column.name
                and column.name.lower() in frame.aliases
                and self._in_alias_context(column, select)
            ):
                continue
            self._validate_column(column, frame)

    def _validate_node_columns(self, node: exp.Expression, frame: _Frame) -> None:
        if not self.scope.columns:
            return
        for column in node.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                raise ScopeViolation(FIELD_SCOPE_CHECK_FAILED, "该语句位置禁止使用通配符")
            if column.find_ancestor(exp.Select) is not None:
                continue
            self._validate_column(column, frame)

    def _validate_column(self, column: exp.Column, frame: _Frame) -> None:
        name = column.name
        if not name:
            return
        qualifier = (column.table or "").lower()
        if qualifier:
            source = frame.find(qualifier)
            if source is None:
                raise ScopeViolation(
                    FIELD_SCOPE_CHECK_FAILED, f"无法确定字段所属表：{column.sql(dialect='mysql')}"
                )
            self._check_source_field(source, name)
            return
        source = self._resolve_unqualified(name, frame)
        if source is not None:
            self._check_source_field(source, name)

    def _resolve_unqualified(self, name: str, frame: _Frame) -> _Source | None:
        lowered = name.lower()
        current: _Frame | None = frame
        while current is not None:
            if current.sources:
                matches = [
                    source
                    for source in current.sources
                    if source.columns is not None and lowered in source.column_names
                ]
                if matches:
                    for source in matches:
                        self._check_source_field(source, name)
                    return matches[0]
                if any(source.physical and source.columns is None for source in current.sources):
                    raise ScopeViolation(
                        FIELD_SCOPE_CHECK_FAILED,
                        f"无法读取表结构，字段 {name} 的授权无法校验",
                    )
            current = current.outer
        return None

    def _check_source_field(self, source: _Source, name: str) -> None:
        if not source.physical or source.allowed is None:
            return
        if name.lower() not in source.allowed:
            qualified = f"{source.database}.{source.table}" if source.database else source.table
            raise ScopeViolation(FIELD_DENIED, f"当前 Token 无权访问字段：{qualified}.{name}")

    @staticmethod
    def _in_alias_context(column: exp.Column, select: exp.Select) -> bool:
        node = column.parent
        while node is not None and node is not select:
            if isinstance(node, (exp.Order, exp.Group, exp.Having)) and node.parent is select:
                return True
            node = node.parent
        return False

    # ---------- INSERT / DESCRIBE ----------

    def _process_insert(self, stmt: exp.Insert) -> None:
        ctes = self._apply_with(stmt, {})
        target = stmt.this
        schema = target if isinstance(target, exp.Schema) else None
        table = schema.this if schema is not None else target
        if not isinstance(table, exp.Table):
            raise ScopeViolation(FIELD_SCOPE_CHECK_FAILED, "无法解析 INSERT 目标表")
        database = table.db or self.default_database
        self._require_table(database, table.name)
        self.tables.add((database, table.name))
        allowed = self.scope.allowed_columns(self.datasource_id, database, table.name)
        if not self.collect_only and allowed is not None:
            declared = [item.name for item in schema.expressions] if schema is not None else None
            if declared is None:
                actual = self.metadata.get((database.lower(), table.name.lower()))
                # A column-less INSERT writes every real column: all real columns must be inside the granted set.
                # `allowed < actual` only misjudges as allowed when `allowed` contains dropped columns (W11).
                if actual is None or not {name.lower() for name in actual} <= set(allowed):
                    raise ScopeViolation(
                        FIELDS_REQUIRED,
                        f"表 {table.name} 的字段授权不完整，INSERT 必须显式指定允许字段",
                    )
            else:
                for name in declared:
                    if name.lower() not in allowed:
                        raise ScopeViolation(
                            FIELD_DENIED,
                            f"当前 Token 无权写入字段：{database}.{table.name}.{name}",
                        )

        frame = _Frame(
            sources=[
                _Source(
                    key=(table.alias_or_name or table.name).lower(),
                    physical=True,
                    database=database,
                    table=table.name,
                    allowed=allowed,
                    columns=self.metadata.get((database.lower(), table.name.lower())),
                )
            ]
        )
        frame.by_key[frame.sources[0].key] = frame.sources[0]

        conflict = stmt.args.get("conflict")
        if not self.collect_only and conflict is not None:
            self._validate_node_columns(conflict, frame)
            self._walk_for_queries(conflict, ctes, frame)

        expression = stmt.args.get("expression")
        if isinstance(expression, exp.Expression):
            self._walk_for_queries(expression, ctes, frame)

    def _process_describe(self, stmt: exp.Describe) -> None:
        table = stmt.this
        if not isinstance(table, exp.Table):
            return
        database = table.db or self.default_database
        self._require_table(database, table.name)
        self.tables.add((database, table.name))
        if self.collect_only:
            return
        allowed = self.scope.allowed_columns(self.datasource_id, database, table.name)
        if allowed is None:
            return
        actual = self.metadata.get((database.lower(), table.name.lower()))
        if actual is None or not {name.lower() for name in actual} <= set(allowed):
            raise ScopeViolation(
                FIELDS_REQUIRED, f"DESCRIBE 需要表 {table.name} 的全部字段权限"
            )

    # ---------- Subquery traversal ----------

    def _process_statement_subqueries(
        self,
        stmt: exp.Expression,
        frame: _Frame,
        keys: tuple[str, ...],
        ctes: dict[str, tuple[str, ...] | None] | None = None,
    ) -> None:
        scope_ctes = ctes if ctes is not None else {}
        for key in keys:
            value = stmt.args.get(key)
            if value is not None:
                self._walk_for_queries(value, scope_ctes, frame)

    def _walk_for_queries(
        self,
        node: exp.Expression | list,
        ctes: dict[str, tuple[str, ...] | None],
        frame: _Frame | None,
    ) -> None:
        if isinstance(node, list):
            for item in node:
                if isinstance(item, exp.Expression):
                    self._walk_for_queries(item, ctes, frame)
            return
        if not isinstance(node, exp.Expression):
            return
        if isinstance(node, (exp.Select, exp.Union)):
            self._process_query(node, ctes, outer=frame)
            return
        if isinstance(node, exp.Subquery):
            self._process_query(node.this, ctes, outer=frame)
            return
        for value in node.args.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, exp.Expression):
                        self._walk_for_queries(item, ctes, frame)
            elif isinstance(value, exp.Expression):
                self._walk_for_queries(value, ctes, frame)

    # ---------- Output columns ----------

    @staticmethod
    def _projection_aliases(select: exp.Select) -> frozenset[str]:
        aliases: set[str] = set()
        for projection in select.expressions:
            if isinstance(projection, exp.Alias) and projection.alias:
                aliases.add(projection.alias.lower())
        return frozenset(aliases)

    @staticmethod
    def _output_columns(select: exp.Select | exp.Union) -> tuple[str, ...] | None:
        if isinstance(select, exp.Union):
            return _Processor._output_columns(select.this)
        names: list[str] = []
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                return None
            if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                return None
            name = projection.alias_or_name
            if not name:
                return None
            names.append(name)
        return tuple(names)


def _qualified_star(key: str) -> exp.Expression:
    return exp.Column(this=exp.Star(), table=exp.to_identifier(key, quoted=True))


def _table_set(processor: _Processor) -> set[tuple[str, str]]:
    return processor.tables


async def enforce(
    stmt: exp.Expression,
    scope: TokenScope,
    datasource_id: int,
    default_database: str,
    engine: AsyncEngine,
    *,
    exposed_databases: frozenset[str] | None = None,
    datasource_name: str = "",
) -> bool:
    """Validate and rewrite the SQL in place; return whether a rewrite happened. Violations raise ScopeViolation.

    Visibility (exposure) applies to unrestricted tokens too, so this no longer exits early on ``scope.restricted``.
    """
    collector = _Processor(
        scope,
        datasource_id,
        default_database,
        {},
        collect_only=True,
        exposed_databases=exposed_databases,
        datasource_name=datasource_name,
    )
    collector.process(stmt)
    if not scope.columns:
        return False

    tables = _table_set(collector)
    try:
        metadata = await load_columns(engine, tables)
    except Exception as exc:  # noqa: BLE001 - must block when metadata is unavailable
        logger.warning("读取字段元数据失败，按字段授权阻断：%s", exc)
        raise ScopeViolation(
            FIELD_SCOPE_CHECK_FAILED, "无法读取表结构，查询已按字段授权阻断"
        ) from exc

    processor = _Processor(
        scope,
        datasource_id,
        default_database,
        metadata,
        collect_only=False,
        exposed_databases=exposed_databases,
        datasource_name=datasource_name,
    )
    processor.process(stmt)
    return processor.rewritten
