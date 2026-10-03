"""Settle additive empty groups once, below projection: observed and loaded means 0.

A sum reads 0 only in a group where it read no rows; rows whose values are all NULL are
unknown and stay NULL. A query with a distribution branch, and a metric predicate's source
over several measures under a threshold that 0 passes, keep the earlier settlement, which
reads them as 0 (``earlier_settlement``). Probes and coverage respect row filters.
Projection bypasses and sums without a row count refuse with EMPTY_GROUPS_UNSETTLED. Stocks
and non-additive values remain NULL.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
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
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from .bind import _parse_public_expr, earlier_settlement_applies, is_conditional_case
from .dependencies import plan_is_root
from .indexes import _measure_index, _recipe_index

GUARDED_BASE = "guarded_base"

_ZERO_AGGREGATIONS = {"sum", "count", "count_distinct"}
_COUNTING = {"count", "count_distinct"}
ZERO_MEASURE_CLASSES = frozenset({"additive", "event_count", "entity_count"})


def resolves_to_zero(
    aggregation: str,
    measure: MeasureConfig | None,
    classes: Collection[str] = ZERO_MEASURE_CLASSES,
) -> bool:
    return (
        measure is not None
        and measure.additive
        and measure.measure_class in classes
        and (aggregation or measure.default_aggregation or "").lower() in _ZERO_AGGREGATIONS
    )


def counts_rows(aggregation: str, measure: MeasureConfig | None) -> bool:
    """Whether the measure's leaf counts the rows it reads in each group, beside its value.

    A sum settles to 0 only where that count is 0; a count is already 0 there. Under the
    earlier settlement no leaf counts its rows.
    """
    if (
        earlier_settlement_applies()
        or measure is None
        or not resolves_to_zero(aggregation, measure)
    ):
        return False
    return (aggregation or measure.default_aggregation).lower() not in _COUNTING


def reads_every_row(measure: MeasureConfig) -> bool:
    """Whether a sum of the measure reads every row it is given, so a count of the rows says
    whether a group has data. A CASE with no ELSE (or ELSE NULL) reads only the rows its
    conditions keep, which a rollup's pre-aggregated value can't tell apart; the leaf's row
    marker (``_row_marker``) applies the same test."""
    return not is_conditional_case(measure.expr)


def zero_aliases(rows: Iterable[MeasurePlan], config: PackageConfig) -> dict[str, str]:
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
    return {
        alias: "sum"
        for alias, payload in plan.post_aggregation_exprs.items()
        if expr_resolves_to_zero(_parse_public_expr(payload), config)
    }


@dataclass(frozen=True)
class LeafScope:
    """A plain leaf's aggregate, untimed filters and base time axis."""

    from_table: SqlTableRef
    joins: tuple[SqlJoin, ...]
    where: tuple[Any, ...]
    value: Any
    bucket: Any = None
    raw_time: Any | None = None
    storage_zone: str = "UTC"
    calendar: SqlJoin | None = None
    now: Any = None
    bounded: bool = False


_leaf_scopes: ContextVar[dict[str, LeafScope] | None] = ContextVar("leaf_scopes", default=None)


@contextmanager
def recording_leaf_scopes() -> Iterator[dict[str, LeafScope]]:
    scopes: dict[str, LeafScope] = {}
    token = _leaf_scopes.set(scopes)
    try:
        yield scopes
    finally:
        _leaf_scopes.reset(token)


def record_leaf_scope(alias: str, scope: LeafScope) -> None:
    scopes = _leaf_scopes.get()
    if scopes is not None and plan_is_root():
        scopes[alias] = scope


def guard_empty_groups(
    source: str,
    keys: Iterable[str],
    measures: Iterable[str],
    zero: Mapping[str, str],
    scopes: Mapping[str, LeafScope] | None = None,
    *,
    rows: Mapping[str, str] | None = None,
    time_key: str = "",
    dialect: Any = None,
) -> list[SqlCte]:
    """Settle measures centrally, with untimed observation and loaded coverage guards.

    ``rows`` names, for each sum, the source column counting the rows it read in the group:
    the sum reads 0 only where that count is NULL (the group has no row of the measure) or
    0, never where its rows' values are all NULL. A sum without one is refused, as are
    scopes and ``time_key`` without a dialect with time coverage. Inside
    ``earlier_settlement`` a NULL sum reads 0 wherever its measure has data in scope.
    """
    if (scopes or time_key) and not (dialect is not None and dialect.has_time_coverage):
        raise _unsettled_error({"time_coverage": getattr(dialect, "name", "")})
    rows = rows or {}
    fields_ = [SqlField(SqlIdentifier(parts=["base", key]), key) for key in keys]
    ctes: list[SqlCte] = []
    joins: list[SqlJoin] = []
    coverage: dict[str, str] = {}
    for alias in measures:
        value: Any = SqlIdentifier(parts=["base", alias])
        aggregation = zero.get(alias)
        if aggregation is not None:
            seen: Any = SqlBinary(
                SqlWindow(
                    function=SqlCall("MAX" if aggregation in _COUNTING else "COUNT", [value])
                ),
                ">",
                SqlLiteral(0),
            )
            scope = (scopes or {}).get(alias)
            if scope is not None:
                if scope.bounded:
                    seen = SqlBinary(seen, "OR", _seen_outside_window(scope))
                if time_key and scope.bucket is not None:
                    loaded = repr(
                        (
                            scope.from_table,
                            scope.raw_time,
                            scope.storage_zone,
                            scope.bucket,
                            scope.calendar,
                        )
                    )
                    name = coverage.get(loaded)
                    if name is None:
                        name = coverage[loaded] = f"coverage_{len(coverage) + 1}"
                        ctes.append(SqlCte(name=name, query=coverage_select(scope, dialect)))
                        joins.append(SqlJoin("CROSS", SqlTableRef(name=name)))
                    seen = SqlBinary(seen, "AND", _loaded_bucket(time_key, name))
            if aggregation not in _COUNTING and not earlier_settlement_applies():
                if alias not in rows:
                    raise _unsettled_error({"measures": [alias], "missing": "row_count"})
                # A populated sum always survives; a NULL one is 0 only if it read no rows.
                count = SqlIdentifier(parts=["base", rows[alias]])
                empty = SqlBinary(SqlIsNull(count), "OR", SqlBinary(count, "=", SqlLiteral(0)))
                fill = SqlBinary(seen, "AND", empty)
                value = SqlCall("COALESCE", [value, SqlCase([SqlCaseWhen(fill, SqlLiteral(0))])])
            elif scope is not None:
                # A zero count records no observation. A positive count or populated sum
                # always survives; coverage gates only the empty-group substitution.
                if aggregation in _COUNTING:
                    value = SqlCall("NULLIF", [value, SqlLiteral(0)])
                value = SqlCall("COALESCE", [value, SqlCase([SqlCaseWhen(seen, SqlLiteral(0))])])
            else:
                value = SqlCase(
                    whens=[SqlCaseWhen(seen, SqlCall("COALESCE", [value, SqlLiteral(0)]))],
                )
        fields_.append(SqlField(value, alias))
    guard = SqlSelect(
        select=fields_, from_table=SqlTableRef(name=source, alias="base"), joins=joins
    )
    return [*ctes, SqlCte(name=GUARDED_BASE, query=guard)]


def _seen_outside_window(scope: LeafScope) -> SqlExists:
    if (
        not isinstance(scope.value, SqlCall)
        or scope.value.name not in {"SUM", "COUNT", "COUNT_IF", "COUNTIF"}
        or len(scope.value.args) != 1
    ):
        raise _unsettled_error({"observation": "unsupported_aggregate"})
    operand = scope.value.args[0]
    observed = (
        operand
        if scope.value.name in {"COUNT_IF", "COUNTIF"}
        else SqlBinary(operand, "IS NOT", SqlLiteral(None))
    )
    return SqlExists(
        SqlSelect(
            select=[SqlField(SqlLiteral(1), "seen")],
            from_table=replace(scope.from_table),
            joins=list(scope.joins),
            where=[*scope.where, observed],
            limit=1,
            observation_scan=True,
        )
    )


def _loaded_bucket(time_key: str, coverage: str) -> SqlBinary:
    bucket = SqlIdentifier(parts=["base", time_key])
    return SqlBinary(
        SqlBinary(bucket, ">=", SqlIdentifier(parts=[coverage, "loaded_from"])),
        "AND",
        SqlBinary(bucket, "<=", SqlIdentifier(parts=[coverage, "loaded_to"])),
    )


def coverage_select(scope: LeafScope, dialect: Any) -> SqlSelect:
    known = dialect.utc_timestamp(scope.raw_time, scope.storage_zone)
    now = scope.now
    joins = [scope.calendar] if scope.calendar is not None else []
    last_bucket = SqlCase([SqlCaseWhen(SqlBinary(known, "<=", now), scope.bucket)])
    return SqlSelect(
        select=[
            SqlField(
                SqlCall("MIN", [scope.bucket]),
                "loaded_from",
            ),
            SqlField(SqlCall("MAX", [last_bucket]), "loaded_to"),
        ],
        from_table=replace(scope.from_table),
        joins=joins,
        observation_scan=True,
    )


def require_time_scopes(aliases: Iterable[str], scopes: Mapping[str, LeafScope]) -> None:
    missing = set(aliases) - scopes.keys()
    if missing:
        raise _unsettled_error({"time_scopes": sorted(missing)})


def absent_entities_gate(name: str, source: str, value: str) -> tuple[SqlCte, SqlJoin, SqlBinary]:
    """Gate absent entities on the centrally settled source, without inventing a zero."""
    count = SqlCall("COUNT", [SqlIdentifier(parts=["settled", value])])
    cte = SqlCte(
        name=name,
        query=SqlSelect(
            select=[SqlField(count, "settled_rows")],
            from_table=SqlTableRef(name=source, alias="settled"),
        ),
    )
    join = SqlJoin(join_type="CROSS", table=SqlTableRef(name=name), on=None)
    return cte, join, SqlBinary(SqlIdentifier(parts=[name, "settled_rows"]), ">", SqlLiteral(0))


def refuse_unsettled(
    projection: SqlSelect, plan: LogicalPlan, config: PackageConfig, *, combined: bool
) -> None:
    """Recompute required guards from the plan and refuse a projection that bypasses them."""
    expected = zero_outputs(plan, config) if combined else zero_aliases(plan.measure_plans, config)
    unsettled = sorted(base_reads(projection.select) & expected.keys())
    if unsettled and not reads_guarded_base(projection):
        raise _unsettled_error({"measures": unsettled})


def reads_guarded_base(select: SqlSelect) -> bool:
    source = select.from_table
    return isinstance(source, SqlTableRef) and source.name == GUARDED_BASE


def require_settled_source(source: SqlSelect, details: Mapping[str, Any]) -> None:
    if not reads_guarded_base(source):
        raise _unsettled_error(details)


def refuse_shared_names(row_counts: Iterable[str], taken: Collection[str]) -> None:
    """Refuse row counts named like each other or like a name in ``taken`` (case-folded): the
    guard could read the wrong column, or a rewrite of the name reach a physical column."""
    folded = [name.casefold() for name in row_counts]
    shared = sorted({name for name in folded if name in taken or folded.count(name) > 1})
    if shared:
        raise _unsettled_error({"row_counts_named_like": shared})


def _unsettled_error(details: Mapping[str, Any]) -> SemanticLayerError:
    return SemanticLayerError(
        "EMPTY_GROUPS_UNSETTLED",
        "The query reads a sum or count without settling its empty groups, so a group with "
        "no rows could read NULL instead of 0, or a group whose values are all unknown read "
        "0. This is an engine defect, not a query error.",
        details=dict(details),
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
    """Record guarded outputs, including nested compiles, for runtime scope warnings."""
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
