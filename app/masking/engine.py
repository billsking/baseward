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


@dataclass(frozen=True)
class MaskPlan:
    position_rules: dict[int, Rule] = field(default_factory=dict)
    wildcard: bool = False
    is_union: bool = False
    tables: tuple[tuple[str, str], ...] = ()
    manual_rules: tuple[ManualRule, ...] = ()
    datasource_id: int | None = None

    @property
    def has_rules(self) -> bool:
        return bool(self.position_rules) or self.wildcard or self.is_union


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


def _plan_select(
    select: exp.Select,
    datasource_id: int,
    default_database: str,
    manual_rules: list[ManualRule],
) -> MaskPlan:
    table_map = _collect_tables(select, default_database)
    query_tables = tuple(sorted(set(table_map.values())))
    position_rules: dict[int, Rule] = {}
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

    return MaskPlan(
        position_rules=position_rules,
        wildcard=wildcard,
        is_union=False,
        tables=query_tables,
        manual_rules=tuple(manual_rules),
        datasource_id=datasource_id,
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
        return MaskPlan(
            position_rules=position_rules,
            wildcard=wildcard,
            is_union=True,
            tables=tuple(sorted(tables)),
            manual_rules=tuple(manual_rules),
            datasource_id=datasource_id,
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
        if plan.wildcard:
            rules.append(_rule_for_output_name(plan, column_name))
            continue
        rule = plan.position_rules.get(index)
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
