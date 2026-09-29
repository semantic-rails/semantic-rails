"""Empty groups: NULL when there is no data, 0 when there is data of nothing.

The rule for a sum, a count or a distinct count: a group with no rows reads 0 when the
measure has data somewhere in the query's scope, and NULL when it has none at all. A
measure counts as observed when at least one group holds a value: a non-NULL sum, or a
count above zero. Averages, minimums, maximums, stocks and distinct populations have no
value for nothing and stay NULL, as do measures of already-aggregated values.

Invariant: every such measure the projection reads comes from ``guarded_base``, and
nothing else turns a measure NULL into 0. :func:`resolves_to_zero` is the only predicate
and :func:`guard_empty_groups` the only place that builds the guard, so a query that skips
the guard shows up as a measure read from anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from typing import Any

from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    MeasureRefExpr,
    MetricRecipeRefExpr,
    SemanticExpr,
)
from ..ir import MeasurePlan
from ..schema import MeasureConfig, PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from .indexes import _measure_index, _recipe_index

GUARDED_BASE = "guarded_base"

_ZERO_AGGREGATIONS = {"sum", "count", "count_distinct"}
# A count never reads NULL, so a group's count is observed once it is above zero.
_COUNTING = {"count", "count_distinct"}
# Semi-additive stocks and distinct populations have no value for nothing.
_ZERO_MEASURE_CLASSES = {"additive", "event_count", "entity_count"}


def resolves_to_zero(aggregation: str, measure: MeasureConfig | None) -> bool:
    """Whether ``aggregation`` of ``measure`` over no rows is 0 rather than undefined."""
    return (
        measure is not None
        and measure.additive
        and measure.measure_class in _ZERO_MEASURE_CLASSES
        and (aggregation or measure.default_aggregation or "").lower() in _ZERO_AGGREGATIONS
    )


def zero_aliases(rows: Iterable[MeasurePlan], config: PackageConfig) -> dict[str, str]:
    """The measure aliases of ``rows`` that resolve to zero, each with its aggregation."""
    measures = _measure_index(config)
    zero: dict[str, str] = {}
    for row in rows:
        bound = row.bound_measure
        measure = measures.get(bound.measure_id)
        if measure is not None and resolves_to_zero(bound.aggregation, measure):
            zero[bound.alias] = (bound.aggregation or measure.default_aggregation).lower()
    return zero


def expr_resolves_to_zero(expr: SemanticExpr, config: PackageConfig) -> bool:
    """Whether a whole expression is 0 over no rows: sums and differences of such measures."""
    if isinstance(expr, MeasureRefExpr | AggregateExpr):
        measure = _measure_index(config).get(expr.measure)
        return resolves_to_zero(expr.aggregation, measure)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return recipe is not None and expr_resolves_to_zero(recipe.expression, config)
    if isinstance(expr, ArithmeticExpr) and expr.op in {"add", "subtract"}:
        return expr_resolves_to_zero(expr.left, config) and expr_resolves_to_zero(
            expr.right, config
        )
    return False


def guard_empty_groups(
    source: str, keys: Iterable[str], measures: Iterable[str], zero: Mapping[str, str]
) -> SqlCte:
    """The ``guarded_base`` CTE: ``source`` with each ``zero`` measure's NULLs settled.

    ``zero`` maps a measure alias to its aggregation. Its NULL reads 0 while some row of the
    whole grouped result holds a value, and stays NULL when none does. The other measures
    pass through. It sits below the projection, so a LIMIT or a metric filter can't change
    which groups it sees.
    """
    fields_: list[SqlField] = [SqlField(SqlIdentifier(parts=["base", key]), key) for key in keys]
    for alias in measures:
        value: Any = SqlIdentifier(parts=["base", alias])
        aggregation = zero.get(alias)
        if aggregation is not None:
            seen = SqlCall("MAX" if aggregation in _COUNTING else "COUNT", [value])
            value = SqlCase(
                whens=[
                    SqlCaseWhen(
                        SqlBinary(SqlWindow(function=seen), ">", SqlLiteral(0)),
                        SqlCall("COALESCE", [value, SqlLiteral(0)]),
                    )
                ],
                else_expr=None,
            )
        fields_.append(SqlField(value, alias))
    return SqlCte(
        name=GUARDED_BASE,
        query=SqlSelect(select=fields_, from_table=SqlTableRef(name=source, alias="base")),
    )


def sql_nodes(node: Any) -> Iterator[Any]:
    """Every node of a SQL AST, found generically so no node type can hide a read."""
    if isinstance(node, list | tuple):
        for item in node:
            yield from sql_nodes(item)
    elif is_dataclass(node) and not isinstance(node, type):
        yield node
        for item in fields(node):
            yield from sql_nodes(getattr(node, item.name))


def base_reads(expr: Any) -> set[str]:
    """The aliases an output expression reads from ``base``."""
    return {
        node.parts[-1]
        for node in sql_nodes(expr)
        if isinstance(node, SqlIdentifier) and len(node.parts) == 2 and node.parts[0] == "base"
    }


_zero_outputs: ContextVar[list[dict[str, Any]] | None] = ContextVar("zero_outputs", default=None)


@contextmanager
def recording_zero_outputs() -> Iterator[list[dict[str, Any]]]:
    """Collect every output that follows the zero-or-NULL rule, nested compiles included.

    The runtime warns about the ones that came back NULL on every row, which needs no
    extra query.
    """
    outputs: list[dict[str, Any]] = []
    token = _zero_outputs.set(outputs)
    try:
        yield outputs
    finally:
        _zero_outputs.reset(token)


def record_zero_output(output: str, measure_ids: Iterable[str]) -> None:
    outputs = _zero_outputs.get()
    if outputs is not None:
        outputs.append({"output": output, "measures": sorted(set(measure_ids))})
