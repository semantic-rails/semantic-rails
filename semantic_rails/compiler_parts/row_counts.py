"""Row-count columns: the name each sum's matching-row count takes in a leaf, so the guard can
tell a group with no rows from one whose values are NULL.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..expressions import (
    collect_column_refs,
)
from ..ir import (
    LogicalPlan,
    MeasurePlan,
)
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlCall,
    SqlField,
)
from .bind import (
    _row_marker,
    aggregation_overrides,
)
from .empty_groups import (
    counts_rows,
)
from .lowering_common import _metric_filter_alias, _query_key_aliases

_row_count_columns: ContextVar[Mapping[str, str] | None] = ContextVar(
    "row_count_columns", default=None
)


# The leaves that count their rows for the answer: the measures the call re-aggregates.
_counted_overrides: ContextVar[Collection[str]] = ContextVar("counted_overrides", default=())


@contextmanager
def naming_row_counts(names: Mapping[str, str], overrides: Collection[str] = ()) -> Iterator[None]:
    """Name the row count of each leaf lowered in this block (``_row_count_names``); a nested
    compile names its own. A leaf in ``overrides`` counts its rows whatever its statistic."""
    token = _row_count_columns.set(names)
    counted = _counted_overrides.set(overrides)
    try:
        yield
    finally:
        _counted_overrides.reset(counted)
        _row_count_columns.reset(token)


def _names_in_use(plan: LogicalPlan, config: PackageConfig) -> set[str]:
    """Every name a hidden row count must not take, case-folded as SQL may compare them: the
    query's outputs, grouping keys and internal aliases, every column the package's models
    declare, and every column the query's measures read."""
    measures = {row.bound_measure.measure_id for row in plan.measure_plans}
    filters = len(list(plan.query.get("metric_filters", []) or []))
    names = [
        *_query_key_aliases(plan),
        *plan.post_aggregation_exprs,
        *(row.bound_measure.alias for row in plan.measure_plans),
        *(_metric_filter_alias(index) for index in range(filters)),
        *(
            ref.column
            for row in config.measures
            if row.id in measures
            for ref in collect_column_refs(row.expr)
        ),
        *(
            column
            for row in config.entities
            for column in [row.primary_key, *row.key, *row.identifiers]
            + [column for columns in row.foreign_keys.values() for column in columns]
        ),
        *(row.column for row in config.dimensions),
        *(
            column
            for row in config.relationships
            for column in (
                row.source_column,
                row.target_column,
                *row.source_columns,
                *row.target_columns,
            )
        ),
        *(
            column
            for row in config.aggregate_relations
            for column in [
                row.time_column,
                *row.measure_columns.values(),
                *row.dimension_columns.values(),
            ]
        ),
        *(column for row in config.relations for column in row.columns),
    ]
    return {str(name).casefold() for name in names if name}


def _row_count_names(plan: LogicalPlan, taken: Collection[str]) -> dict[str, str]:
    """Each measure's row-count column, by its alias: ``m<i>_rows`` beside the ``m<i>`` the
    alias registry gives the measure, renamed until it takes no name in ``taken``. The registry
    rewrites every identifier it names, so it never names these, nor any physical column."""
    taken = set(taken)
    names: dict[str, str] = {}
    aliases = dict.fromkeys(row.bound_measure.alias for row in plan.measure_plans)
    for index, alias in enumerate(aliases, start=1):
        name, suffix = f"m{index}_rows", 2
        while name.casefold() in taken:
            name, suffix = f"m{index}_rows_{suffix}", suffix + 1
        taken.add(name.casefold())
        names[alias] = name
    return names


def _row_markers(measure_plan: MeasurePlan, measure: Any, value_expr: Any) -> list[tuple[Any, str]]:
    """The per-row marker a zero-settled sum's leaf counts, and the column the count goes in.

    The guard reads 0 only where that count is 0 (see ``empty_groups``); counts need none. A
    leaf lowered outside ``naming_row_counts`` counts nothing, and the guard refuses its sum.
    A measure the call re-aggregates counts its source rows too, for the answer to state; a
    rollup holds no source rows to count.
    """
    bound = measure_plan.bound_measure
    name = (_row_count_columns.get() or {}).get(bound.alias)
    stated = bound.alias in _counted_overrides.get() and not measure_plan.aggregate_relation_id
    if name is None or not (counts_rows(bound.aggregation, measure) or stated):
        return []
    return [(_row_marker(value_expr), name)]


def override_row_counts(plan: LogicalPlan, config: PackageConfig) -> dict[str, str]:
    """The column counting the source rows of each measure the call re-aggregates (a statistic
    other than its default), by leaf alias."""
    names = _row_count_names(plan, _names_in_use(plan, config))
    overridden = aggregation_overrides(plan.query.get("select"), config)
    return {alias: names[alias] for alias in overridden if alias in names}


def share_group_counts(plan: LogicalPlan, config: PackageConfig) -> dict[str, str]:
    """The column counting the rows each selected share's total spans, by output alias."""
    taken = _names_in_use(plan, config)
    taken |= {name.casefold() for name in _row_count_names(plan, taken).values()}
    names: dict[str, str] = {}
    for index, (alias, payload) in enumerate(plan.post_aggregation_exprs.items(), start=1):
        if payload.get("kind") != "share":
            continue
        name, suffix = f"s{index}_groups", 2
        while name.casefold() in taken:
            name, suffix = f"s{index}_groups_{suffix}", suffix + 1
        taken.add(name.casefold())
        names[alias] = name
    return names


def _rows_fields(measure_plan: MeasurePlan, measure: Any, value_expr: Any) -> list[SqlField]:
    """Beside a sum's value: how many of the group's rows it read."""
    return [
        SqlField(SqlCall("COUNT", [marker]), alias)
        for marker, alias in _row_markers(measure_plan, measure, value_expr)
    ]
