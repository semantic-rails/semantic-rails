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

A metric predicate keeps the same invariant for the entities its source doesn't list: they
read what an entity with no match reads there, 0 where the measure has data in the predicate's
scope and NULL where it has none. :func:`absent_entities_gate` decides that from the settled
source, and :func:`require_settled_source` refuses a source that skipped the guard.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
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
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlStar,
    SqlTableRef,
    SqlWindow,
)
from .bind import _parse_public_expr
from .dependencies import plan_is_root
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


@dataclass(frozen=True)
class LeafScope:
    """What one leaf reads, kept so the guard can look at its measure outside the query's window.

    ``where`` holds the leaf's filters without the query's time bounds. ``bucket`` is the label
    of the leaf's time axis and ``raw_time`` the column it comes from, when the guard can also
    tell which buckets the relation has loaded.
    """

    from_table: SqlTableRef
    joins: tuple[SqlJoin, ...]
    where: tuple[Any, ...]
    value: Any
    grain: str = ""
    bucket: Any | None = None
    raw_time: Any | None = None
    calendar: SqlJoin | None = None
    as_of: str = ""


_leaf_scopes: ContextVar[dict[str, LeafScope] | None] = ContextVar("leaf_scopes", default=None)


@contextmanager
def recording_leaf_scopes() -> Iterator[dict[str, LeafScope]]:
    """Collect the scope of each plain leaf of the request's own query, by measure alias."""
    scopes: dict[str, LeafScope] = {}
    token = _leaf_scopes.set(scopes)
    try:
        yield scopes
    finally:
        _leaf_scopes.reset(token)


def record_leaf_scope(alias: str, scope: LeafScope) -> None:
    """Keep ``scope`` for the guard; a nested query's leaves (a predicate's, say) are not kept."""
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
    """The ``guarded_base`` CTE (last), with any coverage CTEs it reads: ``source`` settled.

    ``zero`` maps a measure alias to its aggregation. Its NULL reads 0 while the measure has
    data in scope, and stays NULL when it has none. The other measures pass through. It sits
    below the projection, so a LIMIT or a metric filter can't change which groups it sees.

    A measure has data in scope when some row of the whole grouped result holds a value, or,
    given its ``scopes`` (a query with a time window), when it has a value anywhere outside
    the window under the same filters. With a ``time_key`` and a bucket, only buckets that the
    relation has loaded read 0: one before its first row or after its last (up to now)
    reads NULL, since nothing says it was recorded.
    """
    fields_: list[SqlField] = []
    for key in keys:
        fields_.append(SqlField(SqlIdentifier(parts=["base", key]), key))
    ctes: list[SqlCte] = []
    joins: list[SqlJoin] = []
    coverage: dict[str, str] = {}
    for alias in measures:
        value: Any = SqlIdentifier(parts=["base", alias])
        aggregation = zero.get(alias)
        if aggregation is not None:
            seen: Any = SqlBinary(
                SqlWindow(function=SqlCall("MAX" if aggregation in _COUNTING else "COUNT", [value])),
                ">",
                SqlLiteral(0),
            )
            fill: Any = SqlLiteral(0)
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
                    fill = _loaded_bucket(time_key, name, fill)
            value = SqlCase(
                whens=[SqlCaseWhen(seen, SqlCall("COALESCE", [value, fill]))], else_expr=None
            )
        fields_.append(SqlField(value, alias))
    guard = SqlSelect(
        select=fields_, from_table=SqlTableRef(name=source, alias="base"), joins=joins
    )
    return [*ctes, SqlCte(name=GUARDED_BASE, query=guard)]


def _seen_outside_window(scope: LeafScope) -> SqlExists:
    """Whether the measure has a value under the leaf's filters, whatever the time window."""
    where = list(scope.where)
    if not isinstance(scope.value, SqlStar):
        where.append(SqlBinary(scope.value, "IS NOT", SqlLiteral(None)))
    probe = SqlSelect(
        select=[SqlField(SqlLiteral(1), "seen")],
        # A copy: a row filter finds each scan of a relation by its own node.
        from_table=replace(scope.from_table),
        joins=list(scope.joins),
        where=where,
        limit=1,
    )
    return SqlExists(probe)


def _loaded_bucket(time_key: str, coverage: str, zero: Any) -> SqlCase:
    """``zero`` for a bucket between the relation's first loaded bucket and its last, else NULL."""
    bucket = SqlIdentifier(parts=["base", time_key])
    inside = SqlBinary(
        SqlBinary(bucket, ">=", SqlIdentifier(parts=[coverage, "loaded_from"])),
        "AND",
        SqlBinary(bucket, "<=", SqlIdentifier(parts=[coverage, "loaded_to"])),
    )
    return SqlCase(whens=[SqlCaseWhen(inside, zero)], else_expr=None)


def coverage_select(scope: LeafScope, dialect: Any) -> SqlSelect:
    """The buckets a relation has loaded: its first, and its last up to now, in one row.

    The last is the relation's authored ``freshness_as_of`` when it declares one, else the
    latest row that isn't in the future, so a placeholder such as 9999-12-31 never extends
    it. ``first_at`` and ``last_at`` are the raw times the buckets come from.
    """
    known = dialect.timestamp_cast(scope.raw_time)
    now = dialect.timestamp_cast(dialect.now())
    if (stamp := _authored_as_of(scope.as_of)) is None:
        past = SqlBinary(known, "<=", now)
        last_at: Any = SqlCase(whens=[SqlCaseWhen(past, known)])
        last_bucket: Any = SqlCase(whens=[SqlCaseWhen(past, scope.bucket)])
    else:
        authored = dialect.timestamp_cast(SqlLiteral(stamp))
        last_at = SqlCase(
            whens=[SqlCaseWhen(SqlBinary(authored, "<=", now), authored)], else_expr=now
        )
        last_bucket = dialect.date_trunc(scope.grain, last_at)
    return SqlSelect(
        select=[
            SqlField(SqlCall("MIN", [scope.bucket]), "loaded_from"),
            SqlField(SqlCall("MAX", [last_bucket]), "loaded_to"),
            SqlField(SqlCall("MIN", [known]), "first_at"),
            SqlField(SqlCall("MAX", [last_at]), "last_at"),
        ],
        from_table=replace(scope.from_table),
        joins=[scope.calendar] if scope.calendar is not None else [],
    )


def _authored_as_of(value: str) -> str | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=None).isoformat(sep=" ")


def absent_entities_gate(name: str, source: str, value: str) -> tuple[SqlCte, SqlJoin, SqlBinary]:
    """What lets an entity ``source`` doesn't list read 0: the source holds a settled value.

    ``source`` is a query settled by :func:`guard_empty_groups`, so its ``value`` is non-NULL
    on every row where the measures have data in scope, and NULL on every row where they have
    none. An entity absent from it reads like an entity with no match: 0 in the first case
    and NULL in the second, by the same test, so a threshold that 0 passes may keep it only
    while some row holds a value. Nothing here turns a NULL into 0. Returns the one-row CTE,
    the join to it, and the condition that keeps a row.
    """
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
    """Refuse a projection that reads an empty-group measure from anywhere but ``guarded_base``.

    What must be guarded is worked out again from the plan (``combined`` for the outputs of
    branches joined together), never taken from lowering, so a path that never built the guard
    or read past it is refused here instead of answering with a silent NULL.
    """
    expected = zero_outputs(plan, config) if combined else zero_aliases(plan.measure_plans, config)
    unsettled = sorted(base_reads(projection.select) & expected.keys())
    if unsettled and not reads_guarded_base(projection):
        raise _unsettled_error({"measures": unsettled})


def reads_guarded_base(select: SqlSelect) -> bool:
    source = select.from_table
    return isinstance(source, SqlTableRef) and source.name == GUARDED_BASE


def require_settled_source(source: SqlSelect, details: Mapping[str, Any]) -> None:
    """Refuse a source that a consumer reads as settled when it reads past the guard.

    A metric predicate lets an entity its source doesn't list read like the ones it does, which
    holds only while every row of the source is settled together.
    """
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
