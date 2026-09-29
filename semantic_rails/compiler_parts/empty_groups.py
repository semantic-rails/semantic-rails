"""Empty groups: NULL when there is no data, 0 when there is data of nothing.

The rule for a sum, a count or a distinct count: a group with no rows reads 0 when the
measure has data somewhere in the query's scope, and NULL when it has none at all. A
measure counts as observed when at least one group holds a value: a non-NULL sum, or a
count above zero. Averages, minimums, maximums, stocks and distinct populations have no
value for nothing and stay NULL, as do measures of already-aggregated values.

Invariant: every such measure the projection reads comes from ``guarded_base``, and
nothing else turns a measure NULL into 0. :func:`resolves_to_zero` is the only predicate
and :func:`guard_empty_groups` the only place that builds the guard. Lowering checks its own
projection with :func:`refuse_unsettled`, so a path that skips the guard is refused with a
stable code instead of answering with a silent NULL.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from typing import Any

from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    MeasureRefExpr,
    MetricRecipeRefExpr,
    SemanticExpr,
)
from ..ir import LogicalPlan, MeasurePlan
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
from .bind import _parse_public_expr
from .indexes import _measure_index, _recipe_index

GUARDED_BASE = "guarded_base"

_ZERO_AGGREGATIONS = {"sum", "count", "count_distinct"}
# A count never reads NULL, so a group's count is observed once it is above zero.
_COUNTING = {"count", "count_distinct"}
# Semi-additive stocks and distinct populations have no value for nothing.
ZERO_MEASURE_CLASSES = frozenset({"additive", "event_count", "entity_count"})


def resolves_to_zero(
    aggregation: str,
    measure: MeasureConfig | None,
    classes: Collection[str] = ZERO_MEASURE_CLASSES,
) -> bool:
    """Whether ``aggregation`` of ``measure`` over no rows is 0 rather than undefined.

    ``classes`` are the measure classes that count: a threshold on a population also counts a
    distinct population, whose count of no entities is 0.
    """
    return (
        measure is not None
        and measure.additive
        and measure.measure_class in classes
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


def expr_resolves_to_zero(
    expr: SemanticExpr, config: PackageConfig, classes: Collection[str] = ZERO_MEASURE_CLASSES
) -> bool:
    """Whether a whole expression is 0 over no rows: sums and differences of such measures."""
    if isinstance(expr, MeasureRefExpr | AggregateExpr):
        measure = _measure_index(config).get(expr.measure)
        return resolves_to_zero(expr.aggregation, measure, classes)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return recipe is not None and expr_resolves_to_zero(recipe.expression, config, classes)
    if isinstance(expr, ArithmeticExpr) and expr.op in {"add", "subtract"}:
        return expr_resolves_to_zero(expr.left, config, classes) and expr_resolves_to_zero(
            expr.right, config, classes
        )
    return False


def zero_outputs(plan: LogicalPlan, config: PackageConfig) -> dict[str, str]:
    """The outputs of a branch-combined plan that are 0 over no rows, each as a plain value."""
    return {
        alias: "sum"
        for alias, payload in plan.post_aggregation_exprs.items()
        if expr_resolves_to_zero(_parse_public_expr(payload), config)
    }


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


def refuse_unsettled(
    projection: SqlSelect, plan: LogicalPlan, config: PackageConfig, *, combined: bool
) -> None:
    """Refuse a projection that reads an empty-group measure from anywhere but ``guarded_base``.

    What must be guarded is worked out again from the plan (``combined`` for the outputs of
    branches joined together), never taken from lowering, so a path that never built the guard
    or read past it is refused here instead of answering with a silent NULL.
    """
    expected = zero_outputs(plan, config) if combined else zero_aliases(plan.measure_plans, config)
    unsettled = sorted(base_reads(projection.select) & expected.keys())
    source = projection.from_table
    if unsettled and not (isinstance(source, SqlTableRef) and source.name == GUARDED_BASE):
        raise SemanticLayerError(
            "EMPTY_GROUPS_UNSETTLED",
            "The query reads a sum or count without settling its empty groups, so a group with "
            "no rows would read NULL instead of 0. This is an engine defect, not a query error.",
            details={"measures": unsettled},
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
