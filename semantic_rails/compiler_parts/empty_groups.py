"""Settle additive empty groups once, below projection: observed and loaded means 0.

Probes and coverage respect row filters. Projection bypasses refuse with
EMPTY_GROUPS_UNSETTLED. Stocks and non-additive values remain NULL.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
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
    SqlCast,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from .bind import _parse_public_expr
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
    grain: str = ""
    bucket: Any = None
    raw_time: Any | None = None
    calendar: SqlJoin | None = None
    as_of: Any = None
    now: Any = None


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
    time_key: str = "",
    dialect: Any = None,
) -> list[SqlCte]:
    """Settle measures centrally, with untimed observation and loaded coverage guards."""
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
                seen = SqlBinary(seen, "OR", _seen_outside_window(scope))
                if time_key and scope.bucket is not None:
                    loaded = repr(
                        (scope.from_table, scope.raw_time, scope.bucket, scope.calendar)
                        + (scope.grain, scope.as_of)
                    )
                    name = coverage.get(loaded)
                    if name is None:
                        name = coverage[loaded] = f"coverage_{len(coverage) + 1}"
                        ctes.append(SqlCte(name=name, query=coverage_select(scope, dialect)))
                        joins.append(SqlJoin("CROSS", SqlTableRef(name=name)))
                    seen = SqlBinary(seen, "AND", _loaded_bucket(time_key, name))
            value = SqlCase(
                whens=[SqlCaseWhen(seen, SqlCall("COALESCE", [value, SqlLiteral(0)]))],
                else_expr=None,
            )
        fields_.append(SqlField(value, alias))
    guard = SqlSelect(
        select=fields_, from_table=SqlTableRef(name=source, alias="base"), joins=joins
    )
    return [*ctes, SqlCte(name=GUARDED_BASE, query=guard)]


def _seen_outside_window(scope: LeafScope) -> SqlExists:
    counting = isinstance(scope.value, SqlCall) and scope.value.name.upper() in {
        "COUNT",
        "COUNT_IF",
        "COUNTIF",
    }
    return SqlExists(
        SqlSelect(
            select=[SqlField(SqlLiteral(1), "seen")],
            from_table=replace(scope.from_table),
            joins=list(scope.joins),
            where=list(scope.where),
            having=[
                SqlBinary(
                    scope.value, ">" if counting else "IS NOT", SqlLiteral(0 if counting else None)
                )
            ],
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
    known = dialect.timestamp_cast(scope.raw_time)
    now = dialect.timestamp_cast(scope.now or dialect.now())
    joins = [scope.calendar] if scope.calendar is not None else []
    if scope.as_of is None:
        past = SqlBinary(known, "<=", now)
        last_at: Any = SqlCase(whens=[SqlCaseWhen(past, known)])
        last_bucket: Any = SqlCase(whens=[SqlCaseWhen(past, scope.bucket)])
    else:
        authored = dialect.timestamp_cast(scope.as_of)
        last_at = SqlCase(
            whens=[SqlCaseWhen(SqlBinary(authored, "<=", now), authored)], else_expr=now
        )
        last_bucket = dialect.date_trunc(scope.grain, last_at)
        if scope.calendar is not None:
            assert isinstance(scope.calendar.on, SqlBinary) and isinstance(
                scope.calendar.on.left, SqlIdentifier
            )
            assert isinstance(scope.bucket, SqlIdentifier)
            last_bucket = SqlIdentifier(["coverage_edge", scope.bucket.parts[-1]])
            joins.append(
                SqlJoin(
                    "LEFT",
                    replace(scope.calendar.table, alias="coverage_edge"),
                    SqlBinary(
                        SqlIdentifier(["coverage_edge", scope.calendar.on.left.parts[-1]]),
                        "=",
                        SqlCast(last_at, "DATE"),
                    ),
                )
            )
    return SqlSelect(
        select=[
            SqlField(
                SqlCall("MIN", [SqlCase([SqlCaseWhen(SqlBinary(known, "<=", now), scope.bucket)])]),
                "loaded_from",
            ),
            SqlField(SqlCall("MAX", [last_bucket]), "loaded_to"),
            SqlField(
                SqlBinary(
                    SqlCall("MIN", [known]),
                    ">",
                    dialect.timestamp_cast(SqlCall("MIN", [scope.bucket])),
                ),
                "first_partial",
            ),
        ],
        from_table=replace(scope.from_table),
        joins=joins,
        observation_scan=True,
    )


def authored_as_of(value: str) -> str | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    moment = moment.astimezone(UTC) if moment.tzinfo is not None else moment
    return moment.replace(tzinfo=None).isoformat(sep=" ")


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


def _unsettled_error(details: Mapping[str, Any]) -> SemanticLayerError:
    return SemanticLayerError(
        "EMPTY_GROUPS_UNSETTLED",
        "The query reads a sum or count without settling its empty groups, so a group with "
        "no rows would read NULL instead of 0. This is an engine defect, not a query error.",
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
