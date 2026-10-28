"""Subtotal and grand-total rows (a call's ``totals``).

``totals`` lists the group_by ids subtotal rows keep, outermost first. Each shorter prefix of
it is one level of total rows, down to the grand total (``[]``); a level that keeps every
group_by column is the call's own grain and adds nothing. A level's rows are the call itself at
that grain (the coarser call a ``total`` expression reads), so every measure is computed from
its own rows there, never added up from the call's rows: a distinct count's subtotal counts
distinct values. With a time grain, every level keeps the period.

The rows come back in one statement, after the call's own rows (``having`` and ``limit`` cut
only those), level by level from the finest to the grand total. A column the level rolls up
reads NULL there, and the ``total_over`` column names the group_by ids the row adds up (NULL on
the call's own rows), so a total row never reads as a NULL group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..ast import TIME_AXIS
from ..ir import LogicalPlan
from ..query_call import TOTAL_OVER
from ..schema import PackageConfig
from ..sql_ast import (
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlOrder,
    SqlSelect,
    SqlSetQuery,
    SqlTableRef,
    sql_nodes,
)
from ..top_n import limit_order
from . import nested
from .empty_groups import recording_outputs_as
from .lowering_common import _query_key_aliases
from .namespacing import _namespace_sql_select
from .paths import display_key
from .predicate_sets import _requested_order
from .totals import _unique_name as _unique
from .totals import coarse_call


def total_levels(plan: LogicalPlan, config: PackageConfig) -> list[list[str]]:
    """The group_by columns each level of total rows rolls up, finest level first, each level
    once. A level keeps its prefix of ``totals`` and the entity key of each name it keeps."""
    totals = plan.query.get("totals")
    if totals is None:
        return []
    levels: list[list[str]] = []
    for size in range(len(totals), -1, -1):
        kept = totals[:size]
        keep = {*kept, *filter(None, (display_key(config, column) for column in kept))}
        rolled = [column for column in plan.group_by if column not in keep]
        if rolled and rolled not in levels:
            levels.append(rolled)
    return levels


def _named(rolled: list[str]) -> str:
    """``total_over``'s value: every column the level rolls up, in group_by order."""
    return ", ".join(rolled)


def with_total_rows(plan: LogicalPlan, config: PackageConfig, detail: SqlSelect) -> SqlSelect:
    """``detail`` (the call's own rows, with its order and limit), then its total rows."""
    levels = total_levels(plan, config)
    keys = [key for key in _query_key_aliases(plan) if key in {f.alias for f in detail.select}]
    detail = limit_order(detail, keys)[0]
    outputs = [field.alias for field in detail.select]
    taken = {
        node.name.casefold()
        for node in sql_nodes(detail)
        if isinstance(node, (SqlCte, SqlTableRef))
    }
    detail_name = _unique("total_detail", taken)
    rows_name = _unique("total_rows", taken)
    # Orders the rows and is never returned: named like none of the call's columns.
    level_column, suffix = f"{rows_name}_level", 2
    while level_column.casefold() in {column.casefold() for column in outputs}:
        level_column, suffix = f"{rows_name}_level_{suffix}", suffix + 1

    def branch(source: str, produced: set[str], level: int, marker: str | None) -> SqlSelect:
        fields = [
            SqlField(
                SqlIdentifier(parts=[source, column]) if column in produced else SqlLiteral(None),
                column,
            )
            for column in outputs
        ]
        return SqlSelect(
            select=[
                *fields,
                SqlField(SqlLiteral(marker), TOTAL_OVER),
                SqlField(SqlLiteral(level), level_column),
            ],
            from_table=SqlTableRef(name=source),
        )

    # The call's own statement stays at the top, its CTEs where the answer's readers (its
    # coverage, NO_DATA_YET) find them.
    ctes = [*detail.ctes, SqlCte(name=detail_name, query=replace(detail, ctes=[]))]
    branches = [branch(detail_name, set(outputs), 0, None)]
    for index, rolled in enumerate(levels, start=1):
        payload = coarse_call(
            plan.query,
            [],
            [dict(row) for row in plan.query.get("select") or []],
            config,
            group_by=[column for column in plan.group_by if column not in rolled],
            time=plan.time,
        )
        name = _unique("total_level", taken)
        # The call's own outputs carry its disclosures; a level's are the same columns again.
        with recording_outputs_as({}):
            level = nested.query_sql(config, payload, standalone=True)
        ctes.append(SqlCte(name=name, query=_namespace_sql_select(level, f"{name}__")))
        produced = {field.alias for field in level.select}
        branches.append(branch(name, produced, index, _named(rolled)))
    ctes.append(SqlCte(name=rows_name, query=SqlSetQuery("UNION ALL", branches)))
    return SqlSelect(
        ctes=ctes,
        select=[SqlField(SqlIdentifier(parts=[rows_name, column]), column) for column in outputs]
        + [SqlField(SqlIdentifier(parts=[rows_name, TOTAL_OVER]), TOTAL_OVER)],
        from_table=SqlTableRef(name=rows_name),
        order_by=[
            SqlOrder(SqlIdentifier(parts=[rows_name, level_column]), "ASC"),
            *_row_order(plan, rows_name, outputs, keys),
        ],
    )


def _row_order(
    plan: LogicalPlan, source: str, outputs: list[str], keys: list[str]
) -> list[SqlOrder]:
    """Within the call's own rows and within each level: the call's order (by default the
    period first), then every group key and the period, ascending."""
    requested = [
        term
        for term in _requested_order(plan, source)
        if isinstance(term.expression, SqlIdentifier) and term.expression.parts[-1] in outputs
    ]
    if not requested and TIME_AXIS in keys:
        requested = [SqlOrder(SqlIdentifier(parts=[source, TIME_AXIS]), "ASC", nulls_last=True)]
    ordered = {
        term.expression.parts[-1]
        for term in requested
        if isinstance(term.expression, SqlIdentifier)
    }
    rest = [
        SqlOrder(SqlIdentifier(parts=[source, key]), "ASC", nulls_last=True)
        for key in keys
        if key not in ordered
    ]
    return [*requested, *rest]


def total_rows_disclosure(plan: LogicalPlan, config: PackageConfig) -> dict[str, Any] | None:
    """``TOTALS``: what the total rows are, when the call asks for them."""
    levels = total_levels(plan, config)
    if plan.query.get("totals") is None:
        return None
    return {
        "code": "TOTALS",
        "levels": [_named(rolled) for rolled in levels],
        "message": (
            f"Rows whose {TOTAL_OVER} names columns add those columns up: each is the call at "
            "that grain, computed from the rows the call keeps before having and limit, never "
            "added up from the returned rows."
        ),
    }


def call_rows(statement: SqlSelect) -> SqlSelect:
    """The statement of the call's own rows: ``statement`` itself, or, when it adds total rows,
    the CTE those rows come from (with the call's order and limit)."""
    ctes = {cte.name: cte.query for cte in statement.ctes}
    source = statement.from_table
    rows = ctes.get(source.name) if isinstance(source, SqlTableRef) else None
    if isinstance(rows, SqlSetQuery) and isinstance(rows.queries[0].from_table, SqlTableRef):
        detail = ctes.get(rows.queries[0].from_table.name)
        if isinstance(detail, SqlSelect):
            return detail
    return statement
