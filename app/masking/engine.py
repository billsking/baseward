"""Map a query AST to a result-column masking plan and apply it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlglot import exp

from app.masking.rules import (
    STRATEGY_OFF,
    ManualRule,
    Rule,
    detect_auto,
    mask_value,
    strictest,
)

logger = logging.getLogger(__name__)

# (database, table, column)
_Ref = tuple[str, str, str]


@dataclass(frozen=True)
class MaskPlan:
    position_rules: dict[int, Rule] = field(default_factory=dict)
    wildcard: bool = False
    is_union: bool = False
    tables: tuple[tuple[str, str], ...] = ()
    manual_rules: tuple[ManualRule, ...] = ()
    datasource_id: int | None = None
    # Output name (lower-cased) -> rule derived from the underlying real columns. Covers
    # projections expanded from a derived table or CTE, whose output name may be an alias.
    name_rules: dict[str, Rule] = field(default_factory=dict)
    # Column lineage reached through derived sources: output name -> (database, table, column).
    wildcard_rules: dict[str, Rule] = field(default_factory=dict)

    @property
    def has_rules(self) -> bool:
        return (
            bool(self.position_rules)
            or bool(self.name_rules)
            or bool(self.wildcard_rules)
            or self.wildcard
            or self.is_union
        )


EMPTY_PLAN = MaskPlan()


def _collect_tables(select: exp.Expression, default_database: str) -> dict[str, tuple[str, str]]:
    table_map: dict[str, tuple[str, str]] = {}
    for table in select.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            continue
        database = (table.db or default_database or "").lower()
        key = (database, name)
        alias = table.alias_or_name.lower()
        table_map[alias] = key
        table_map[name] = key
    return table_map


def _is_star(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Star):
        return True
    return isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)


def _manual_matches(
    manual_rules: list[ManualRule],
    datasource_id: int,
    column_name: str,
    table: tuple[str, str] | None,
    query_tables: tuple[tuple[str, str], ...],
) -> list[Rule]:
    lowered = column_name.lower()
    matches: list[Rule] = []
    for rule in manual_rules:
        if rule.datasource_id != datasource_id or rule.column.lower() != lowered:
            continue
        rule_table = (rule.database.lower(), rule.table.lower())
        if table is not None and rule_table != table:
            continue
        if table is None and rule_table not in query_tables:
            continue
        matches.append(rule.as_rule())
    return matches


def _column_rule(
    column: exp.Column,
    manual_rules: list[ManualRule],
    datasource_id: int,
    table_map: dict[str, tuple[str, str]],
    query_tables: tuple[tuple[str, str], ...],
) -> Rule | None:
    qualifier = column.table.lower()
    if qualifier:
        table = table_map.get(qualifier)
    elif len(query_tables) == 1:
        table = query_tables[0]
    else:
        table = None
    matches = _manual_matches(manual_rules, datasource_id, column.name, table, query_tables)
    if matches:
        return strictest(matches)
    return detect_auto(column.name)


@dataclass
class _LineageSource:
    """A FROM/JOIN source: either a real table or a derived relation with known columns."""

    physical: tuple[str, str] | None = None
    refs: dict[str, tuple[_Ref, ...]] | None = None


def _declared_names(alias: exp.Expression | None) -> list[str]:
    if alias is None:
        return []
    names: list[str] = []
    for column in alias.args.get("columns") or []:
        name = getattr(column, "name", "")
        if name:
            names.append(name.lower())
    return names


def _apply_declared_columns(
    refs: dict[str, tuple[_Ref, ...]], alias: exp.Expression | None
) -> dict[str, tuple[_Ref, ...]]:
    """Honour an explicit column list such as ``WITH c(a, b) AS (...)``."""
    declared = _declared_names(alias)
    if not declared or len(declared) != len(refs):
        return refs
    return dict(zip(declared, refs.values()))


def _lineage_query(
    node: exp.Expression,
    default_database: str,
    ctes: dict[str, dict[str, tuple[_Ref, ...]]],
) -> dict[str, tuple[_Ref, ...]]:
    if isinstance(node, exp.Subquery):
        return _lineage_query(node.this, default_database, ctes)
    if isinstance(node, exp.Union):
        local_ctes = dict(ctes)
        with_ = node.args.get("with_")
        if with_ is not None:
            for cte in with_.expressions:
                refs = _lineage_query(cte.this, default_database, local_ctes)
                local_ctes[cte.alias.lower()] = _apply_declared_columns(
                    refs, cte.args.get("alias")
                )
        merged: dict[str, tuple[_Ref, ...]] = {}
        for branch in (node.this, node.expression):
            for name, refs in _lineage_query(branch, default_database, local_ctes).items():
                merged[name] = tuple(dict.fromkeys(merged.get(name, ()) + refs))
        return merged
    if isinstance(node, exp.Select):
        return _lineage_select(node, default_database, ctes)
    return {}


def _add_lineage_source(
    node: exp.Expression,
    default_database: str,
    ctes: dict[str, dict[str, tuple[_Ref, ...]]],
    sources: dict[str, _LineageSource],
) -> None:
    if isinstance(node, (exp.Paren, exp.Lateral)):
        _add_lineage_source(node.this, default_database, ctes, sources)
        return
    if isinstance(node, exp.Subquery):
        key = (node.alias or "").lower()
        refs = _lineage_query(node.this, default_database, ctes)
        sources[key] = _LineageSource(refs=_apply_declared_columns(refs, node.args.get("alias")))
        return
    if isinstance(node, exp.Table):
        key = (node.alias_or_name or node.name or "").lower()
        name = node.name.lower()
        if not node.db and name in ctes:
            sources[key] = _LineageSource(refs=ctes[name])
            return
        sources[key] = _LineageSource(physical=(node.db or default_database, node.name))
        return
    sources[(getattr(node, "alias", "") or "").lower()] = _LineageSource()


def _resolve_refs(
    column: exp.Column,
    sources: dict[str, _LineageSource],
) -> tuple[_Ref, ...]:
    name = column.name.lower()
    if not name:
        return ()
    qualifier = column.table.lower()
    if qualifier:
        source = sources.get(qualifier)
        if source is None:
            return ()
        if source.physical is not None:
            database, table = source.physical
            return ((database, table, name),)
        return (source.refs or {}).get(name, ())
    found: list[_Ref] = []
    for source in sources.values():
        if source.physical is not None:
            database, table = source.physical
            found.append((database, table, name))
        else:
            found.extend((source.refs or {}).get(name, ()))
    return tuple(dict.fromkeys(found))


def _expression_refs(
    expression: exp.Expression,
    sources: dict[str, _LineageSource],
) -> tuple[_Ref, ...]:
    if isinstance(expression, (exp.Count, exp.Sum, exp.Avg)):
        return ()
    refs: list[_Ref] = []
    for column in expression.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        refs.extend(_resolve_refs(column, sources))
    return tuple(dict.fromkeys(refs))


def _lineage_select(
    select: exp.Select,
    default_database: str,
    ctes: dict[str, dict[str, tuple[_Ref, ...]]],
) -> dict[str, tuple[_Ref, ...]]:
    """Map each output column to the real columns it derives from.

    Derived tables and CTEs are resolved recursively so that ``SELECT * FROM
    (SELECT phone AS p FROM users) x`` still knows that ``p`` carries ``users.phone``.
    """
    local_ctes = dict(ctes)
    with_ = select.args.get("with_")
    if with_ is not None:
        for cte in with_.expressions:
            refs = _lineage_query(cte.this, default_database, local_ctes)
            local_ctes[cte.alias.lower()] = _apply_declared_columns(refs, cte.args.get("alias"))

    sources: dict[str, _LineageSource] = {}
    from_ = select.args.get("from_")
    if from_ is not None:
        _add_lineage_source(from_.this, default_database, local_ctes, sources)
    for join in select.args.get("joins") or []:
        _add_lineage_source(join.this, default_database, local_ctes, sources)

    lineage: dict[str, tuple[_Ref, ...]] = {}
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            for source in sources.values():
                for name, refs in (source.refs or {}).items():
                    lineage[name] = tuple(dict.fromkeys(lineage.get(name, ()) + refs))
            continue
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            source = sources.get(projection.table.lower())
            if source is not None:
                for name, refs in (source.refs or {}).items():
                    lineage[name] = tuple(dict.fromkeys(lineage.get(name, ()) + refs))
            continue
        name = (projection.alias_or_name or "").lower()
        if not name:
            continue
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        refs = _expression_refs(expression, sources)
        if refs:
            lineage[name] = tuple(dict.fromkeys(lineage.get(name, ()) + refs))
    return lineage


def _rule_for_ref(
    ref: _Ref,
    manual_rules: list[ManualRule],
    datasource_id: int,
    query_tables: tuple[tuple[str, str], ...],
) -> Rule | None:
    database, table, column = ref
    matches = _manual_matches(manual_rules, datasource_id, column, (database, table), query_tables)
    if matches:
        return strictest(matches)
    return detect_auto(column)


def _rules_for_lineage(
    lineage: dict[str, tuple[_Ref, ...]],
    manual_rules: list[ManualRule],
    datasource_id: int,
    query_tables: tuple[tuple[str, str], ...],
) -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    for name, refs in lineage.items():
        resolved = strictest(
            [
                rule
                for rule in (
                    _rule_for_ref(ref, manual_rules, datasource_id, query_tables) for ref in refs
                )
                if rule is not None
            ]
        )
        if resolved is not None:
            rules[name] = resolved
    return rules


def _plan_select(
    select: exp.Select,
    datasource_id: int,
    default_database: str,
    manual_rules: list[ManualRule],
) -> MaskPlan:
    table_map = _collect_tables(select, default_database)
    query_tables = tuple(sorted(set(table_map.values())))
    position_rules: dict[int, Rule] = {}
    name_rules: dict[str, Rule] = {}
    wildcard = False

    for index, projection in enumerate(select.expressions):
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if _is_star(expression):
            wildcard = True
            continue
        if isinstance(expression, (exp.Count, exp.Sum, exp.Avg)):
            continue
        candidates: list[Rule] = []
        for column in expression.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                wildcard = True
                continue
            rule = _column_rule(column, manual_rules, datasource_id, table_map, query_tables)
            if rule is not None:
                candidates.append(rule)
        rule = strictest(candidates)
        if rule is not None:
            position_rules[index] = rule
            output_name = (projection.alias_or_name or "").lower()
            if output_name:
                existing = name_rules.get(output_name)
                name_rules[output_name] = (
                    rule if existing is None else strictest([existing, rule]) or rule
                )

    return MaskPlan(
        position_rules=position_rules,
        wildcard=wildcard,
        is_union=False,
        tables=query_tables,
        manual_rules=tuple(manual_rules),
        datasource_id=datasource_id,
        name_rules=name_rules,
        wildcard_rules=_rules_for_lineage(
            _lineage_select(select, default_database, {}),
            manual_rules,
            datasource_id,
            query_tables,
        ),
    )


def build_plan(
    ast: exp.Expression | None,
    datasource_id: int,
    default_database: str,
    manual_rules: list[ManualRule],
) -> MaskPlan:
    """Build the masking plan for Select/Union; other statements yield an empty plan."""
    if ast is None:
        return EMPTY_PLAN
    if isinstance(ast, exp.Union):
        position_rules: dict[int, Rule] = {}
        name_rules: dict[str, Rule] = {}
        wildcard = False
        tables: set[tuple[str, str]] = set()
        for branch in ast.find_all(exp.Select):
            branch_plan = _plan_select(branch, datasource_id, default_database, manual_rules)
            wildcard = wildcard or branch_plan.wildcard
            tables.update(branch_plan.tables)
            for index in sorted(branch_plan.position_rules):
                merged = strictest(
                    [rule for rule in (position_rules.get(index), branch_plan.position_rules[index]) if rule]
                )
                if merged is not None:
                    position_rules[index] = merged
            for name, rule in branch_plan.name_rules.items():
                existing = name_rules.get(name)
                name_rules[name] = rule if existing is None else strictest([existing, rule]) or rule
        query_tables = tuple(sorted(tables))
        return MaskPlan(
            position_rules=position_rules,
            wildcard=wildcard,
            is_union=True,
            tables=query_tables,
            manual_rules=tuple(manual_rules),
            datasource_id=datasource_id,
            name_rules=name_rules,
            wildcard_rules=_rules_for_lineage(
                _lineage_query(ast, default_database, {}),
                manual_rules,
                datasource_id,
                query_tables,
            ),
        )
    if isinstance(ast, exp.Select):
        return _plan_select(ast, datasource_id, default_database, manual_rules)
    return EMPTY_PLAN


def _rule_for_output_name(plan: MaskPlan, column_name: str) -> Rule | None:
    if plan.datasource_id is None:
        return detect_auto(column_name)
    matches = _manual_matches(
        list(plan.manual_rules),
        plan.datasource_id,
        column_name,
        None,
        plan.tables,
    )
    if matches:
        return strictest(matches)
    return detect_auto(column_name)


def column_rules(plan: MaskPlan, columns: list[str]) -> list[Rule | None]:
    """Resolve the masking rule by result-column position; None means the column is not masked."""
    rules: list[Rule | None] = []
    for index, column_name in enumerate(columns):
        key = (column_name or "").lower()
        if plan.wildcard:
            rule = plan.name_rules.get(key) or plan.wildcard_rules.get(key)
            if rule is None:
                rule = _rule_for_output_name(plan, column_name)
            rules.append(rule)
            continue
        rule = plan.position_rules.get(index)
        if rule is None:
            rule = plan.name_rules.get(key) or plan.wildcard_rules.get(key)
        if rule is None and plan.is_union:
            rule = _rule_for_output_name(plan, column_name)
        rules.append(rule)
    return rules


def apply(
    plan: MaskPlan,
    columns: list[str],
    rows: list[tuple],
    mask_key: bytes,
) -> tuple[list[list], list[str]]:
    """Mask the result set by plan, returning (masked rows, masked column names)."""
    rules = column_rules(plan, columns)

    masked_columns = [
        columns[index]
        for index, rule in enumerate(rules)
        if rule is not None and rule.strategy != STRATEGY_OFF
    ]

    masked_rows: list[list] = []
    for row_index, row in enumerate(rows):
        output_row: list = []
        for index, value in enumerate(row):
            rule = rules[index] if index < len(rules) else None
            if rule is None or rule.strategy == STRATEGY_OFF:
                output_row.append(value)
                continue
            try:
                output_row.append(mask_value(value, rule, mask_key))
            except Exception:  # noqa: BLE001 - a failing cell must not break the whole row
                column_name = columns[index] if index < len(columns) else str(index)
                logger.warning("脱敏失败 column=%s row=%s", column_name, row_index)
                output_row.append(None)
        masked_rows.append(output_row)
    return masked_rows, masked_columns
