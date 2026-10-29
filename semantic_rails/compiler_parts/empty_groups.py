"""Settle additive empty groups once, below projection: observed and loaded means 0.

A sum reads 0 only in a group where it read no rows; rows whose values are all NULL are
unknown and stay NULL. Beside a distribution, each branch settles its own groups and the
combine fills only those a branch has no row for. A metric predicate's source over several
measures under a threshold that 0 passes keeps the earlier settlement, which reads them as 0
(``earlier_settlement``), as does a measure with a CASE below its root that can drop rows
(``drops_rows_below_root``). Probes and coverage respect row filters.
Projection bypasses and sums without a row count refuse with EMPTY_GROUPS_UNSETTLED. Stocks
and non-additive values remain NULL.

A table's declared edge (``zero_through``: ``now``, or a load log's loaded-through mark)
caps what its measures read (``declared_cap``): no row past the edge exists for any leaf,
nested or not. It also replaces the last row as the upper bound of loaded coverage: an empty
period reads 0 only when it ends at or before that edge (or, under a load log that reached
today, starts before it), and NULL after it. The lower bound (the first row) is unchanged.
The edge, not a matching row, says such a period is observed: its probes read the table's rows
without the measure's authored filter conditions (``LeafScope.source_where``), so a filter that
never matched reads 0 there.

``observation_scope`` says where "observed" is judged: ``dataset`` (the default) in the
measure's own rows under its authored conditions, ``query`` inside the query's filters too.
For a top-level conditional operand, a supported source probe judges whether the relation
has rows, even if none ever meets the condition, for an output whose bucket the loaded-bucket
check gates or that has no time bucket; matching rows with NULL amounts stay NULL.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from ..clock import current_time, engine_window, local_now
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    CaseExpr,
    MeasureRefExpr,
    MetricRecipeRefExpr,
    SemanticExpr,
)
from ..ir import LogicalPlan, MeasurePlan
from ..schema import MeasureConfig, PackageConfig, ZeroThrough
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
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
    sql_nodes,
)
from .bind import (
    _measure_count_distinct_key_columns,
    _parse_public_expr,
    earlier_settlement_applies,
    is_conditional_case,
)
from .dependencies import holding, plan_is_root, settles_as_a_call
from .indexes import _measure_index, _recipe_index
from .temporal import _source_filter_refusals

GUARDED_BASE = "guarded_base"
# How a capped leaf's EXISTS names the load log it reads (``declared_cap``).
LOAD_LOG = "zero_through_log"

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
    earlier settlement no leaf counts its rows; a measure with a conditional CASE below its
    root retains that settlement individually (``drops_rows_below_root``).
    """
    if (
        earlier_settlement_applies()
        or measure is None
        or drops_rows_below_root(measure)
        or not resolves_to_zero(aggregation, measure)
    ):
        return False
    return (aggregation or measure.default_aggregation).lower() not in _COUNTING


def has_nested_case(measure: MeasureConfig) -> bool:
    """A CASE below the expression root cannot read a rollup.

    Detect the unsupported shape without interpreting arithmetic or deriving its row marker.
    """
    return any(
        isinstance(node, CaseExpr) and node is not measure.expr for node in sql_nodes(measure.expr)
    )


def drops_rows_below_root(measure: MeasureConfig) -> bool:
    """Whether a CASE below the expression root can leave a row without a value: one with no
    ELSE (or ELSE NULL), whose failed conditions a count of the rows can't tell from an unknown
    amount without interpreting the arithmetic around it. Such a measure keeps the earlier
    settlement individually. A CASE below the root that gives every row a value, such as
    ``amount * CASE WHEN store_id = 'a' THEN 1.1 ELSE 1.2 END``, reads every row as any other
    arithmetic does."""
    return any(
        isinstance(node, CaseExpr) and node is not measure.expr and is_conditional_case(node)
        for node in sql_nodes(measure.expr)
    )


def reads_every_row(measure: MeasureConfig) -> bool:
    """Whether a sum of the measure reads every row it is given, so a count of the rows says
    whether a group has data. A CASE with no ELSE (or ELSE NULL) reads only the rows its
    conditions keep, which a rollup's pre-aggregated value can't tell apart; the leaf's row
    marker (``_row_marker``) applies the same test. Below the root, such a CASE cannot prove
    this either and keeps base settlement instead of deriving a marker."""
    return not (drops_rows_below_root(measure) or is_conditional_case(measure.expr))


def counts_its_own_key(
    aggregation: str, measure: MeasureConfig | None, config: PackageConfig
) -> bool:
    """Whether a count of the measure is at least 1 in every group its rows reach: it counts its
    own entity's key, which every row holds (``_measure_count_distinct_key_columns``). Only then
    is a 0 a group its rows never reached; a conditional count, a count of another column and a
    sum read a real 0 as well."""
    return (
        measure is not None
        and aggregation in _COUNTING
        and bool(_measure_count_distinct_key_columns(measure, config))
    )


def absent_marker(alias: str, aggregation: str, rows: Mapping[str, str]) -> SqlIdentifier | None:
    """The guard's source column that is NULL exactly where the measure's own rows never
    reached the group (another measure's rows hold it): a count's value, which is never NULL
    where it read a row, or a sum's row count. None for a sum that counts no rows (the earlier
    settlement, or a CASE below the root that can drop rows), which can't tell."""
    if aggregation in _COUNTING:
        return SqlIdentifier(parts=["base", alias])
    if alias in rows and not earlier_settlement_applies():
        return SqlIdentifier(parts=["base", rows[alias]])
    return None


def zero_aliases(rows: Iterable[MeasurePlan], config: PackageConfig) -> dict[str, str]:
    measures = _measure_index(config)
    zero: dict[str, str] = {}
    for row in rows:
        if row.rewrite_strategy == "parent_value":
            continue  # a group's rows name no parent: no data, NULL (``parent_values``)
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


def carried_zero_lookups(expr: SemanticExpr, config: PackageConfig) -> list[str]:
    """A lookup output whose source reads 0 for an observed parent: NULL everywhere is no data.

    Never zero-filled here: the lookup leaf settles the carried value itself.
    """
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return carried_zero_lookups(recipe.expression, config) if recipe is not None else []
    measures = _measure_index(config)
    measure = (
        measures.get(expr.measure) if isinstance(expr, MeasureRefExpr | AggregateExpr) else None
    )
    if measure is None or not measure.lookup_from:
        return []
    return [measure.id] if resolves_to_zero("", measures.get(measure.lookup_from)) else []


def zero_outputs(plan: LogicalPlan, config: PackageConfig) -> dict[str, str]:
    return {
        alias: "sum"
        for alias, payload in plan.post_aggregation_exprs.items()
        if expr_resolves_to_zero(_parse_public_expr(payload), config)
    }


@dataclass(frozen=True)
class DeclaredEdge:
    """A measure's declared edge in the guard's bucket space: ``rows`` reads the load log's
    mark (None under ``now``), and ``bound`` reads it into ``loaded_before``, the first bucket
    the edge doesn't certify, ``loaded_through``, the exclusive edge as wall time in the clock's
    zone, and ``edge_source``."""

    rows: SqlSelect | None
    bound: SqlSelect


@dataclass(frozen=True)
class LeafScope:
    """A plain leaf's aggregate, untimed filters and base time axis. Under a declared edge,
    ``source_where`` is ``where`` without the measure's authored filter conditions: the rows
    whose loaded periods the edge says are observed (None: no authored condition to leave
    out, or no edge)."""

    from_table: SqlTableRef
    joins: tuple[SqlJoin, ...]
    where: tuple[Any, ...]
    value: Any
    bucket: Any = None
    raw_time: Any | None = None
    storage_zone: str = "UTC"
    bounded: bool = False
    edge: DeclaredEdge | None = None
    source_where: tuple[Any, ...] | None = None


_leaf_scopes: ContextVar[dict[str, LeafScope] | None] = ContextVar("leaf_scopes", default=None)


@contextmanager
def recording_leaf_scopes() -> Iterator[dict[str, LeafScope]]:
    scopes: dict[str, LeafScope] = {}
    with holding(_leaf_scopes, scopes):
        yield scopes


def record_leaf_scope(alias: str, scope: LeafScope) -> None:
    scopes = _leaf_scopes.get()
    if scopes is not None and settles_as_a_call():
        scopes[alias] = scope


def observation_scope(query: Mapping[str, Any], config: PackageConfig) -> str:
    """The one reader of the option: the query's own value, else the package default."""
    return str(query.get("observation_scope") or config.package.observation_scope)


def observed_outside_filters(query: Mapping[str, Any], config: PackageConfig) -> bool:
    """Whether observation looks outside the query's own ``where``: under ``dataset``, when it
    has a filter or an entity filter, which is part of the call's ``where`` (the engine holds
    it as a metric predicate). A time window is judged as before, in either scope."""
    entity_filters = any(
        dict(row.get("expression") or {}).get("kind") == "metric_predicate"
        for row in query.get("metric_filters") or []
    )
    return observation_scope(query, config) == "dataset" and bool(
        query.get("where") or entity_filters
    )


_dataset_observation: ContextVar[bool] = ContextVar("dataset_observation", default=False)


@contextmanager
def observing(query: Mapping[str, Any], config: PackageConfig) -> Iterator[None]:
    """While the request's own query lowers, its guard judges observation as it says."""
    with holding(_dataset_observation, observed_outside_filters(query, config)):
        yield


def observes_dataset() -> bool:
    """Whether the guard being built settles the request's own outputs under ``dataset``."""
    return _dataset_observation.get() and settles_as_a_call()


def guard_empty_groups(
    source: str,
    keys: Iterable[str],
    measures: Iterable[str],
    zero: Mapping[str, str],
    scopes: Mapping[str, LeafScope] | None = None,
    *,
    rows: Mapping[str, str] | None = None,
    earlier: Collection[str] = (),
    time_key: str = "",
    dialect: Any = None,
    observed: Mapping[str, LeafScope] | None = None,
    observed_buckets: Collection[str] = (),
    bucketed: bool = True,
    keep: Iterable[str] = (),
    names: Mapping[str, Sequence[str]] | None = None,
    absent: Mapping[str, str] | None = None,
) -> list[SqlCte]:
    """Settle measures centrally, with untimed observation and loaded coverage guards.

    ``rows`` names, for each sum, the source column counting the rows it read in the group:
    the sum reads 0 only where that count is NULL (the group has no row of the measure) or
    0, never where its rows' values are all NULL. A sum without one is refused, as are
    scopes and ``time_key`` without a dialect with time coverage. Inside
    ``earlier_settlement`` a NULL sum reads 0 wherever its measure has data in scope. Aliases
    in ``earlier`` use that same rule individually, for measures with a CASE below the root
    that can drop rows (``drops_rows_below_root``).

    Under the ``dataset`` scope, the request's own outputs are observed where ``observed``
    (each measure's own rows under its authored conditions, never the query's filters) holds
    a value. An output without a probe is refused, never judged inside the filters. Bounded
    scopes observe outside the window the same way. A top-level conditional operand is also
    observed where its probe has a row, even if none meets the condition, but only for an
    output whose bucket the loaded-bucket check gates (``time_key`` and the scope's bucket)
    or that has no time bucket (``bucketed=False``: one total over its scope). Earlier
    settlement retains its value-based observation. Where a declared edge gates the output
    (the scope's ``edge``), its probes read the source rows without the measure's authored
    filter conditions (``source_where``), bounded or not, whether or not they hold a value: the
    edge, not a matching row, says a loaded period is observed.
    ``observed_buckets`` names leaves whose conditional operand retained the pre-filter
    source groups. Where the loaded-bucket check gates the output, their non-NULL count
    (including 0) proves that specific bucket was observed; the sum's matching-row count
    still prevents filling unknown amounts. Ungated, a retained leaf gets no such proof.
    ``keep`` names source columns passed through unchanged.
    ``names`` gives each measure alias's output names, which a refusal states.
    ``absent`` names, for a measure alias, a column reading 1 in each group its own rows never
    reached (``absent_marker``) and 0 elsewhere: what the answer's ``ZERO_FILLED_GROUPS``
    counts. It changes no value.
    """
    if (scopes or time_key) and not (dialect is not None and dialect.reads_coverage):
        raise _unsettled_error({"time_coverage": getattr(dialect, "name", "")})
    rows = rows or {}
    measures = list(measures)
    observed = observed or {}
    absent = absent or {}
    dataset = observes_dataset()
    if dataset and (unobserved := [a for a in measures if a in zero and a not in observed]):
        named = (names or {}).get
        error = _unobserved_error(list(dict.fromkeys(n for a in unobserved for n in named(a, [a]))))
        deferred = _source_filter_refusals.get()
        if deferred is None:
            raise error
        # Binding finishes so the caller's policy gate runs first: a denial outranks this.
        deferred.append(error)
        dataset = False
    fields_ = [SqlField(SqlIdentifier(parts=["base", key]), key) for key in keys]
    ctes: list[SqlCte] = []
    joins: list[SqlJoin] = []
    coverage: dict[str, str] = {}
    edges: dict[str, str] = {}
    probes: dict[str, str] = {}

    def probed(probe: SqlSelect) -> SqlBinary:
        """Whether ``probe`` finds a row, read once through plain CTEs the guard cross-joins."""
        name = probes.get(repr(probe))
        if name is None:
            name = probes[repr(probe)] = f"observed_{len(probes) + 1}"
            ctes.extend(_dataset_probe_ctes(name, probe))
            joins.append(SqlJoin("CROSS", SqlTableRef(name=name)))
        return SqlBinary(SqlIdentifier(parts=[name, "seen"]), ">", SqlLiteral(0))

    markers: list[SqlField] = []
    for alias in measures:
        value: Any = SqlIdentifier(parts=["base", alias])
        aggregation = zero.get(alias)
        marker = absent_marker(alias, aggregation, rows) if aggregation is not None else None
        if marker is not None and alias in absent:
            unreached = SqlCaseWhen(SqlIsNull(marker), SqlLiteral(1))
            markers.append(SqlField(SqlCase([unreached], else_expr=SqlLiteral(0)), absent[alias]))
        if aggregation is not None:
            seen: Any = SqlBinary(
                SqlWindow(
                    function=SqlCall("MAX" if aggregation in _COUNTING else "COUNT", [value])
                ),
                ">",
                SqlLiteral(0),
            )
            scope = (scopes or {}).get(alias)
            gated = bool(time_key) and scope is not None and scope.bucket is not None
            # Source rows settle a never-matched condition only where the loaded-bucket check
            # below gates the output's bucket, or where the output has no time bucket.
            source_rows = gated or not bucketed
            # Under a declared edge, the edge says which gated periods are observed: its probes
            # read the source rows whether or not the authored filter ever matched one. So it
            # does for one total over all time (no window): the edge caps every row it reads.
            declared = (
                scope is not None
                and (
                    (gated and scope.edge is not None)
                    or (not bucketed and scope.source_where is not None)
                )
                and alias not in earlier
                and not earlier_settlement_applies()
            )
            if dataset:
                probe = _seen_outside_window(
                    observed[alias], source_rows=source_rows, declared=declared
                ).query
                seen = SqlBinary(seen, "OR", probed(probe))
            if alias in observed_buckets:
                # A retained leaf group proves rows existed before its authored filter.
                # A missing leaf beside another fact has NULL here and proves nothing.
                if aggregation not in _COUNTING and alias not in rows:
                    raise _unsettled_error({"measures": [alias], "missing": "row_count"})
                # Only the loaded-bucket check below can make that proof a 0: ungated, the
                # leaf settles as if it retained nothing.
                if gated:
                    marker = (
                        value
                        if aggregation in _COUNTING
                        else SqlIdentifier(parts=["base", rows[alias]])
                    )
                    seen = SqlBinary(seen, "OR", SqlBinary(marker, ">=", SqlLiteral(0)))
            if scope is not None:
                # An unbounded answer sees every matching row itself, but not the source rows
                # a declared edge observes without its authored filter.
                unfiltered = declared and scope.source_where is not None
                if (scope.bounded or unfiltered) and not dataset:
                    outside = _seen_outside_window(
                        scope,
                        source_rows=source_rows
                        and alias not in earlier
                        and not earlier_settlement_applies(),
                        declared=declared,
                    )
                    # Where EXISTS may not sit in a select (Snowflake, Spark), the probe is read
                    # through plain CTEs instead.
                    found = outside if dialect.exists_in_select else probed(outside.query)
                    seen = SqlBinary(seen, "OR", found)
                if gated:
                    loaded = repr(
                        (
                            scope.from_table,
                            scope.raw_time,
                            scope.storage_zone,
                            scope.bucket,
                        )
                    )
                    name = coverage.get(loaded)
                    if name is None:
                        name = coverage[loaded] = f"coverage_{len(coverage) + 1}"
                        ctes.append(SqlCte(name=name, query=coverage_select(scope, dialect)))
                        joins.append(SqlJoin("CROSS", SqlTableRef(name=name)))
                    if scope.edge is None:
                        seen = SqlBinary(seen, "AND", _loaded_bucket(time_key, name))
                    else:
                        # One edge per binding: two tables, or two bindings over one table,
                        # never share it.
                        edge = edges.get(repr(scope.edge))
                        if edge is None:
                            edge = edges[repr(scope.edge)] = f"edge_{len(edges) + 1}"
                            ctes.extend(_edge_ctes(edge, scope.edge))
                            joins.append(SqlJoin("CROSS", SqlTableRef(name=edge)))
                        seen = SqlBinary(seen, "AND", _certified_bucket(time_key, name, edge))
            if (
                aggregation not in _COUNTING
                and not earlier_settlement_applies()
                and alias not in earlier
            ):
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
    fields_.extend(markers)
    fields_.extend(SqlField(SqlIdentifier(parts=["base", name]), name) for name in keep)
    guard = SqlSelect(
        select=fields_, from_table=SqlTableRef(name=source, alias="base"), joins=joins
    )
    return [*ctes, SqlCte(name=GUARDED_BASE, query=guard)]


def _seen_outside_window(
    scope: LeafScope, *, source_rows: bool = False, declared: bool = False
) -> SqlExists:
    """Whether the scope has a row that holds a value. ``declared`` (a declared edge gates the
    output) reads the scope's rows without the measure's authored filter conditions, and then
    any such row will do: the edge says its period is loaded, so a filter that matched no row
    there reads 0 even while the column holds no value yet (refunds before the first one)."""
    source_where = scope.source_where if declared else None
    unfiltered = source_where is not None
    where = scope.where if source_where is None else source_where
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
            # A top-level conditional additive operand, or a filtered one under a declared
            # edge, can be measured as zero even when it never matched. Its row count still
            # protects matching NULL sums. Keep every scope filter and leave
            # unsupported/earlier settlement alone.
            where=[
                *where,
                *(
                    []
                    if unfiltered or (source_rows and is_conditional_case(operand))
                    else [observed]
                ),
            ],
            limit=1,
            observation_scan=True,
        )
    )


def _dataset_probe_ctes(name: str, probe: SqlSelect) -> list[SqlCte]:
    """The probe's first row, counted into one row the guard cross-joins: plain CTEs that
    every warehouse runs, and the scan stops at the first row it finds."""
    rows = f"{name}_rows"
    count = SqlCall("COUNT", [SqlIdentifier(parts=[rows, "seen"])])
    return [
        SqlCte(name=rows, query=probe),
        SqlCte(name=name, query=SqlSelect([SqlField(count, "seen")], SqlTableRef(name=rows))),
    ]


def _loaded_bucket(time_key: str, coverage: str) -> SqlBinary:
    bucket = SqlIdentifier(parts=["base", time_key])
    return SqlBinary(
        SqlBinary(bucket, ">=", SqlIdentifier(parts=[coverage, "loaded_from"])),
        "AND",
        SqlBinary(bucket, "<=", SqlIdentifier(parts=[coverage, "loaded_to"])),
    )


def _certified_bucket(time_key: str, coverage: str, edge: str) -> SqlBinary:
    """From the base's first loaded bucket up to (not including) the edge's first uncertified
    one. A NULL mark certifies nothing."""
    bucket = SqlIdentifier(parts=["base", time_key])
    return SqlBinary(
        SqlBinary(bucket, ">=", SqlIdentifier(parts=[coverage, "loaded_from"])),
        "AND",
        SqlBinary(bucket, "<", SqlIdentifier(parts=[edge, "loaded_before"])),
    )


def _edge_ctes(name: str, edge: DeclaredEdge) -> list[SqlCte]:
    if edge.rows is None:
        return [SqlCte(name=name, query=edge.bound)]
    rows = f"{name}_rows"
    return [
        SqlCte(name=rows, query=edge.rows),
        SqlCte(name=name, query=replace(edge.bound, from_table=SqlTableRef(name=rows))),
    ]


def declared_edge(
    edge: ZeroThrough, time: Mapping[str, Any], zone: str, dialect: Any
) -> DeclaredEdge:
    """``edge`` as a bound on the buckets of ``time`` in the clock's ``zone``, wall time on
    both sides. The edge is the load log's mark (a DATE through the end of that day: the next
    local midnight; a timestamp at its instant), or ``now``, capped at ``now``. A bucket is
    certified when its period, inside the window, ends at or before the edge: every bucket
    before the edge's own, or all of them when the window ends by then. A window total's one
    bucket (0) is certified when the window ends by then.

    A load log that reaches the start of today (a nightly that ran) also certifies every
    period, or window, that starts before its mark: today's unfinished part is the period "so
    far", which the answer states, and a period that starts at or after the mark stays NULL.
    A load that stalled a whole day or more keeps the strict rule."""
    now = request_now()
    window = engine_window(time, now=now, timezone=zone)
    clock = _wall_time(now, zone, dialect)
    end = _wall_time(window.end, zone, dialect) if window.end else clock
    rows = None
    through: Any = clock
    nightly: Any = None
    if edge.kind == "loaded":
        mark = SqlIdentifier(parts=["watermark"])
        if edge.data_type == "date":
            through = dialect.date_add("day", SqlLiteral(1), dialect.timestamp_cast(mark))
        else:
            through = dialect.utc_timestamp(mark, edge.timezone)
            if zone != "UTC":
                through = dialect.convert_timezone("UTC", zone, through)
        through = SqlCase([SqlCaseWhen(SqlBinary(through, ">", clock), clock)], else_expr=through)
        column = SqlIdentifier(parts=[edge.relation, edge.column])
        rows = SqlSelect(
            select=[SqlField(SqlCall("MAX", [column]), "watermark")],
            from_table=SqlTableRef(name=edge.relation),
            observation_scan=True,
        )
        today = local_now(now, zone).replace(hour=0, minute=0, second=0, microsecond=0)
        nightly = SqlBinary(through, ">=", _wall_time(today, zone, dialect))
    certified: Any = SqlBinary(through, ">=", end)
    # An ungrained series' buckets are the times themselves.
    own = dialect.date_trunc(time["grain"], through) if time.get("grain") else through
    if time.get("window_total"):
        if nightly is not None:
            if window.start is not None:
                start = _wall_time(window.start, zone, dialect)
                nightly = SqlBinary(nightly, "AND", SqlBinary(start, "<", through))
            certified = SqlBinary(nightly, "OR", certified)
        before = SqlCase([SqlCaseWhen(certified, SqlLiteral(1))], else_expr=SqlLiteral(0))
    else:
        cases = [SqlCaseWhen(nightly, through)] if nightly is not None else []
        before = SqlCase([*cases, SqlCaseWhen(certified, end)], else_expr=own)
    bound = SqlSelect(
        select=[
            SqlField(before, "loaded_before"),
            # Typed: a bare literal (``now`` on BigQuery, which casts nothing) is text.
            SqlField(
                SqlCast(through, dialect.timestamp_type_name())
                if isinstance(through, SqlLiteral)
                else through,
                "loaded_through",
            ),
            SqlField(SqlLiteral(edge.kind), "edge_source"),
        ],
        from_table=None,
    )
    return DeclaredEdge(rows=rows, bound=bound)


def _wall_time(instant: Any, zone: str, dialect: Any) -> Any:
    """``instant`` (an aware ``datetime``, or a window bound's text) as a timestamp literal of
    wall time in ``zone``."""
    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if instant.tzinfo is not None:
        instant = local_now(instant, zone)
    return dialect.timestamp_cast(SqlLiteral(instant.isoformat(sep=" ")))


def refuse_declared_edges(measures: Iterable[MeasureConfig], dialect: Any) -> None:
    """A declared edge where loaded coverage never runs would change nothing: refused, as the
    loader refuses it for the package's own warehouse."""
    declared = sorted(row.id for row in measures if row.zero_through is not None)
    if declared and not dialect.reads_coverage:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"zero_through can only be data on {dialect.name}, which reads no loaded coverage; "
            f"{', '.join(declared)} declare(s) another edge. Write zero_through: data.",
            details={"path": "zero_through", "measures": declared, "warehouse": dialect.name},
        )


def coverage_select(scope: LeafScope, dialect: Any) -> SqlSelect:
    """The base's loaded buckets: from its first row's bucket to the bucket of its last row
    at or before the request's ``now`` (the data-through edge, never the warehouse clock), and
    that last row's instant in UTC."""
    known = dialect.utc_timestamp(scope.raw_time, scope.storage_zone)
    loaded = SqlBinary(known, "<=", now_literal(dialect))
    return SqlSelect(
        select=[
            SqlField(SqlCall("MIN", [scope.bucket]), "loaded_from"),
            SqlField(SqlCall("MAX", [SqlCase([SqlCaseWhen(loaded, scope.bucket)])]), "loaded_to"),
            SqlField(SqlCall("MAX", [SqlCase([SqlCaseWhen(loaded, known)])]), "data_through"),
        ],
        from_table=replace(scope.from_table),
        observation_scan=True,
    )


@dataclass
class CoverageClock:
    """The request's ``now``, which loaded coverage caps at; ``read`` once SQL embeds it."""

    now: datetime
    read: bool = False

    @property
    def literal(self) -> str:
        return self.now.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")


_coverage_clock: ContextVar[CoverageClock | None] = ContextVar("coverage_clock", default=None)


@contextmanager
def capping_at(now: datetime) -> Iterator[CoverageClock]:
    """While a request lowers, its coverage counts rows at or before its ``now``."""
    clock = CoverageClock(now)
    with holding(_coverage_clock, clock):
        yield clock


def declared_cap(
    edge: ZeroThrough, raw: Any, local: Any, storage_zone: str, dialect: Any
) -> list[Any]:
    """The conditions a declared edge puts on the rows a leaf reads, ``raw`` its clock column
    and ``local`` that column on the clock's zone: at or before the request's ``now`` (as
    coverage compares it), and, for a load log, inside a mark it records (a DATE through the
    end of that day on the clock: before its next local midnight; a timestamp at or after the
    row's instant). An empty load log, or one with no mark, lets no row through: nothing is
    certified."""
    instant = dialect.utc_timestamp(raw, storage_zone)
    cap: list[Any] = [SqlBinary(instant, "<=", now_literal(dialect))]
    if edge.kind == "loaded":
        # Its own alias: a log on the facts' own table must not shadow the outer row.
        mark = SqlIdentifier(parts=[LOAD_LOG, edge.column])
        if edge.data_type == "date":
            next_midnight = dialect.date_add("day", SqlLiteral(1), dialect.timestamp_cast(mark))
            covers = SqlBinary(next_midnight, ">", dialect.timestamp_cast(local))
        else:
            covers = SqlBinary(dialect.utc_timestamp(mark, edge.timezone), ">=", instant)
        cap.append(
            SqlExists(
                SqlSelect(
                    select=[SqlField(SqlLiteral(1), "seen")],
                    from_table=SqlTableRef(name=edge.relation, alias=LOAD_LOG),
                    where=[covers],
                    observation_scan=True,
                )
            )
        )
    return cap


def now_literal(dialect: Any) -> Any:
    return dialect.timestamp_cast(SqlLiteral(CoverageClock(request_now()).literal))


def request_now() -> datetime:
    """The request's ``now`` while it lowers; SQL that embeds it serves that ``now`` only."""
    clock = _coverage_clock.get() or CoverageClock(current_time())
    clock.read = True
    return clock.now


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


def _unobserved_error(outputs: list[str]) -> SemanticLayerError:
    return SemanticLayerError(
        "EMPTY_GROUPS_UNSETTLED",
        f"Can't tell whether {', '.join(outputs)} has data outside this query's where filters "
        "(an entity filter or an authored condition the probe can't read, or a query shape "
        "that settles its groups apart), so its empty groups can't read 0 or NULL. Resend "
        "with observation_scope 'query' to judge them inside the filters.",
        details={"outputs": outputs, "observation_scope": "dataset"},
    )


@contextmanager
def unobserved_as(inner: str, output: str) -> Iterator[None]:
    """A refusal that a nested compile can't observe its output ``inner`` names ``output``, the
    request's own, whether it is raised here or deferred until binding finishes."""
    deferred = _source_filter_refusals.get()
    start = len(deferred) if deferred is not None else 0

    def named(error: SemanticLayerError) -> SemanticLayerError:
        if error.code == "EMPTY_GROUPS_UNSETTLED" and error.details.get("outputs") == [inner]:
            return _unobserved_error([output])
        return error

    try:
        yield
    except SemanticLayerError as error:
        renamed = named(error)
        if renamed is error:
            raise
        raise renamed from error
    finally:
        if deferred is not None:
            deferred[start:] = [named(error) for error in deferred[start:]]


def _unsettled_error(details: Mapping[str, Any]) -> SemanticLayerError:
    return SemanticLayerError(
        "EMPTY_GROUPS_UNSETTLED",
        "The query reads a sum or count without settling its empty groups, so a group with "
        "no rows could read NULL instead of 0, or a group whose values are all unknown read "
        "0. This is an engine defect, not a query error.",
        details=dict(details),
    )


def base_reads(expr: Any) -> set[str]:
    """The aliases an output expression reads from ``base``."""
    return {
        node.parts[-1]
        for node in sql_nodes(expr)
        if isinstance(node, SqlIdentifier) and len(node.parts) == 2 and node.parts[0] == "base"
    }


_balance_edges: ContextVar[dict[str, Any] | None] = ContextVar("balance_edges", default=None)


@contextmanager
def recording_balance_edges() -> Iterator[dict[str, Any]]:
    """Record each balance output's loaded coverage: CTEs of their own, apart from the query,
    read only to say where the balance's data ends (``NO_DATA_YET``). A balance reads NULL
    in a period without a snapshot either way, so its coverage never changes a value."""
    edges: dict[str, Any] = {"ctes": {}, "leaves": {}, "outputs": []}
    with holding(_balance_edges, edges):
        yield edges


def record_balance_scope(alias: str, scope: LeafScope, dialect: Any) -> None:
    edges = _balance_edges.get()
    if edges is None or not plan_is_root():
        return
    query = coverage_select(scope, dialect)
    name = next((cte.name for cte in edges["ctes"].values() if cte.query == query), None)
    if name is None:
        name = f"balance_coverage_{len(edges['ctes']) + 1}"
        edges["ctes"][name] = SqlCte(name=name, query=query)
    edges["leaves"][alias] = name


def record_balance_output(output: str, reads: set[str], measure_ids: Iterable[str]) -> None:
    """An output that reads only balances, with the coverage of each."""
    edges = _balance_edges.get()
    if edges is None or not reads or not reads <= edges["leaves"].keys():
        return
    edges["outputs"].append(
        {
            "output": output,
            "measures": sorted(set(measure_ids)),
            "probes": sorted({edges["leaves"][alias] for alias in reads}),
        }
    )


_zero_outputs: ContextVar[list[dict[str, Any]] | None] = ContextVar("zero_outputs", default=None)


@contextmanager
def recording_zero_outputs() -> Iterator[list[dict[str, Any]]]:
    """Record guarded outputs, including nested compiles, for runtime scope warnings."""
    outputs: list[dict[str, Any]] = []
    with holding(_zero_outputs, outputs):
        yield outputs


@contextmanager
def recording_outputs_as(names: Mapping[str, str]) -> Iterator[None]:
    """Record a nested compile's guarded and balance outputs under the request's names for them;
    its other outputs are internal, with no column in the answer, and are dropped."""
    outputs = _zero_outputs.get()
    edges = _balance_edges.get()
    start = len(outputs) if outputs is not None else 0
    edge_start = len(edges["outputs"]) if edges is not None else 0
    yield
    for recorded, first in ((outputs, start), (edges["outputs"] if edges else None, edge_start)):
        if recorded is not None:
            recorded[first:] = [
                {**item, "output": names[item["output"]]}
                for item in recorded[first:]
                if item["output"] in names
            ]


def record_zero_output(
    output: str,
    measure_ids: Iterable[str],
    absent: Sequence[str] = (),
    *,
    zero_means_absent: bool = False,
) -> None:
    """``absent`` names the guard's columns marking the groups each measure the output reads
    never reached (``guard_empty_groups``); a group all of them mark, read as 0, is zero-filled.
    ``zero_means_absent``: the output reads 0 in no other group (``counts_its_own_key``)."""
    outputs = _zero_outputs.get()
    if outputs is not None:
        item: dict[str, Any] = {"output": output, "measures": sorted(set(measure_ids))}
        if absent:
            item["absent"] = list(absent)
        if zero_means_absent:
            item["zero_means_absent"] = True
        outputs.append(item)
