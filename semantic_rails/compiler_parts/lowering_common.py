"""What every lowering step shares: a measure's relations and owned keys, stock snapshot keys,
the coverage and time-scope rules a leaf records, a clock's zone conversion, the query's
key aliases, filter conditions, the day window a source scan reads, and the anchor plan of
an entity expressed through its parent.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .. import operators
from ..ast import (
    NormalizedQuery,
    child_groups,
    normalize_query,
    refuse_child_groups,
    time_output_alias,
)
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ColumnRefExpr,
    ComparisonExpr,
    DistributionExpr,
    EntityValueExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    SemanticExpr,
    TotalExpr,
    expr_to_dict,
)
from ..fanout import (
    analyze_fanout,
    recording_route_choices,
    resolve_path,
)
from ..ir import (
    LogicalPlan,
    MeasurePlan,
    PathSelection,
)
from ..schema import (
    AggregateRelationConfig,
    MeasureConfig,
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlIdentifier,
    SqlIsNull,
    SqlLiteral,
    SqlSelect,
    SqlSetQuery,
    SqlTableRef,
    build_negation,
)
from .bind import (
    _bound_filter_clauses,
    _bound_metric_predicates,
    _config_expr_to_sql,
    _freeze_payload,
    _parse_public_expr,
    earlier_settlement_applies,
)
from .dependencies import (
    holding,
    settles_as_a_call,
)
from .empty_groups import (
    LeafScope,
    declared_cap,
    declared_edge,
    expr_resolves_to_zero,
    has_nested_case,
    reads_every_row,
    record_balance_scope,
    record_leaf_scope,
    refuse_declared_edges,
    request_now,
    resolves_to_zero,
    sql_nodes,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
)
from .paths import (
    _apply_role_timezone,
    _column_ref,
    _direct_dimension_source_expr,
    _entity_in_terms_of_parent_relationship,
    _joins_for_paths,
    _leaf_time_role,
    _path_selection,
    _resolve_dimension_expr,
    _role_conversion,
    dimension_filter,
    rollup_held_lookups,
)
from .post_aggregation import (
    _expr_requires_dense_series,
)
from .temporal import (
    _fractional_second,
    window_total_refusal,
)


def _dialect(config: PackageConfig):
    return dialect_for_warehouse(config.package.warehouse)


def _aggregate_relation_index(config: PackageConfig) -> dict[str, AggregateRelationConfig]:
    return {row.id: row for row in config.aggregate_relations}


def _selected_aggregate_relation(
    measure_plan: MeasurePlan, config: PackageConfig
) -> AggregateRelationConfig | None:
    relation_id = str(getattr(measure_plan, "aggregate_relation_id", "") or "")
    if not relation_id:
        return None
    return _aggregate_relation_index(config).get(relation_id)


def _stock_row_key(measure, config: PackageConfig) -> list[str]:
    """The row key a stock's series come from: its declared grain, else its entity's key."""
    return [column for column in list(measure.row_grain or []) if column] or [
        column for column in _entity_index(config)[measure.entity].key if column
    ]


# The fixes that name no clock, so a refusal can pass them on to any caller.
_KEY_BY_SNAPSHOT_FIX = (
    "Key the entity by the series columns plus its snapshot time, e.g. key: [store_id, date_day]."
)


def _stock_clock_key_gap(measure, temporal_role_id: str, config: PackageConfig) -> dict[str, Any]:
    """Why a stock measure's row key can't tell its snapshots apart on this clock, or ``{}``.

    A snapshot table holds one row per series per snapshot, so its key is the series
    columns plus the snapshot time. A key without the clock's column (a surrogate
    such as ``repo@2026-09-26``) makes every row its own series, and a grain coarser
    than the snapshots then sums them. A one-row-per-series current-state table has
    the same shape and is right, so only the caller decides whether to refuse. A
    clock on another model isn't checked here.
    """
    if measure.measure_class != "semi_additive" or not temporal_role_id:
        return {}
    role = _temporal_role_index(config).get(temporal_role_id)
    clock = _dimension_index(config).get(role.dimension) if role is not None else None
    if role is None or clock is None or clock.entity != measure.entity:
        return {}
    key = _stock_row_key(measure, config)
    if not key or clock.column in key:
        return {}
    return {
        "measure_id": measure.id,
        "temporal_role": temporal_role_id,
        "clock_column": clock.column,
        "clock_class": role.temporal_class,
        "row_key": key,
        "fix": _KEY_BY_SNAPSHOT_FIX,
    }


_ONE_AS_OF_CLOCK_FIX = "Keep one as-of clock in the key and query the stock on it."


def _stock_snapshot_refusal(
    measure, temporal_role_id: str, config: PackageConfig
) -> dict[str, Any]:
    """Why a stock's queries on this clock would add up snapshots of one series, or ``{}``.

    An ``as_of_time`` clock of the stock (one of its own ``times:``) declares a
    snapshot table. If the row key holds none of them, the key can't tell snapshots
    apart on any clock. If it holds one that the query isn't ordered by, the series
    still holds it, so each snapshot is its own series. Either way a bucket would sum
    snapshots, so the caller refuses. The clocks are read from the package analysis,
    not the binding index: examining them doesn't make them query inputs.
    """
    if measure.measure_class != "semi_additive":
        return {}
    analysis = get_package_analysis(config)
    as_of: dict[str, str] = {}  # column -> temporal role
    for role_id in measure.compatible_temporal_roles or []:
        role = analysis.temporal_roles.get(role_id)
        dimension = analysis.dimensions.get(role.dimension) if role is not None else None
        if (
            role is not None
            and role.temporal_class == "as_of_time"
            and dimension is not None
            and dimension.entity == measure.entity
            and dimension.column
        ):
            as_of.setdefault(dimension.column, role_id)
    key = _stock_row_key(measure, config)
    role = analysis.temporal_roles.get(temporal_role_id)
    clock = analysis.dimensions.get(role.dimension) if role is not None else None
    ordering = clock.column if clock is not None else ""
    in_key = [column for column in as_of if column in key]
    held = [column for column in in_key if column != ordering]
    if not key or not as_of or (in_key and not held):
        return {}
    column = (held or list(as_of))[0]
    if not in_key:
        fix = _KEY_BY_SNAPSHOT_FIX
    elif len(in_key) > 1:
        fix = _ONE_AS_OF_CLOCK_FIX
    else:
        fix = f"Query it on the {as_of[column]!r} clock."
    return {
        "measure_id": measure.id,
        "temporal_role": temporal_role_id,
        "clock_column": column,
        "clock_class": "as_of_time",
        "row_key": key,
        "reason": "series_holds_as_of_clock" if held else "key_missing_as_of_clock",
        "fix": fix,
    }


_stock_key_gaps: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "stock_key_gaps", default=None
)


@contextmanager
def recording_stock_key_gaps() -> Iterator[list[dict[str, Any]]]:
    """Collect the key gap of every stock lowered in this block, nested compiles included.

    A stock whose key lacks its event- or state-time clock answers as if each row
    were a series; the runtime warns about each one the SQL actually reads.
    """
    gaps: list[dict[str, Any]] = []
    with holding(_stock_key_gaps, gaps):
        yield gaps


def _snapshot_series_columns(measure, temporal_role_id: str, config: PackageConfig) -> list[str]:
    """The columns that identify one snapshot series of a stock measure.

    That is its row key without the ordering time column. The time column never
    belongs in the series key: partitioning "last snapshot per key" by the
    snapshot's own time makes every snapshot its own group, so the final combine
    sums a stock across periods. An empty result means the series is keyed by time
    alone (one snapshot per day), a valid singleton series.
    """
    order_column = ""
    if temporal_role_id:
        role = _temporal_role_index(config)[temporal_role_id]
        order_column = _dimension_index(config)[role.dimension].column
    key = _stock_row_key(measure, config)
    if not key:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Semi-additive measure '{measure.id}' requires a row grain"
        )
    refusal = _stock_snapshot_refusal(measure, temporal_role_id, config)
    if refusal.get("clock_column") == order_column:
        # An as-of clock declares a snapshot table, so a series that can't tell its
        # snapshots apart is never a current-state table: refuse rather than sum them.
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Stock measure '{measure.id}' takes the last snapshot per key on the as-of clock "
            f"{order_column!r}, but its key doesn't contain that column, so each snapshot would "
            f"count as its own series and be summed. {refusal['fix']}",
            # The key may hold another clock a policy hides: it's left out.
            details={key: value for key, value in refusal.items() if key != "row_key"},
        )
    if refusal:
        # The clock at fault isn't the one queried, and a policy may hide it from the
        # caller, so the error names neither it nor a key that holds it; the parse
        # warning tells the author which one it is.
        fix = refusal["fix"]
        if fix not in (_KEY_BY_SNAPSHOT_FIX, _ONE_AS_OF_CLOCK_FIX):
            fix = "Query it on its as-of clock; the package's parse warning names it."
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Stock measure '{measure.id}' can't be answered on the clock {temporal_role_id!r}: "
            "its key doesn't tell apart the snapshots of one of its as-of clocks, so each "
            f"snapshot would count as its own series and be summed. {fix}",
            details={
                key: refusal[key]
                for key in ("measure_id", "temporal_role", "clock_class", "reason")
            },
        )
    gaps = _stock_key_gaps.get()
    if gaps is not None and (gap := _stock_clock_key_gap(measure, temporal_role_id, config)):
        gaps.append(gap)
    return [column for column in key if column != order_column]


def _time_bucket_expr(time: dict[str, Any], raw_expr: Any, config: PackageConfig) -> Any:
    """The expression a leaf groups its time axis by.

    A window total has no time axis of its own: every row in the window falls in
    one bucket, so the key is a constant that still names a column. A bare literal
    would not do, since some warehouses refuse to group by one.
    """
    if time.get("grain"):
        return _dialect(config).date_trunc(time["grain"], raw_expr)
    if time.get("window_total"):
        return SqlCase(
            whens=[SqlCaseWhen(SqlIsNull(raw_expr), SqlLiteral(0))], else_expr=SqlLiteral(0)
        )
    return raw_expr


def _reads_coverage(plan: LogicalPlan, config: PackageConfig) -> bool:
    """Whether the plan's empty periods are bounded by the base's loaded range: every series on
    DuckDB and Postgres; elsewhere a named window's every-period series (``fill`` with both
    bounds), which can't otherwise tell an empty period inside the data from one after its
    edge."""
    dialect = _dialect(config)
    time = plan.time or {}
    return dialect.has_time_coverage or bool(
        dialect.reads_coverage
        and time.get("fill")
        and time.get("start") is not None
        and time.get("end") is not None
    )


def _emits_time_coverage(plan: LogicalPlan, config: PackageConfig) -> bool:
    """The one boundary for base time coverage: the guard emits it, routing refuses rollups.

    A fill, a dense series or combined branches can show a bucket no leaf has, and a retained
    filtered series one where its authored filter matched no row: each reads 0 only inside
    the base's loaded range (``_reads_coverage``).
    Branches are counted unrouted, so the answer never depends on routing.
    """
    if not plan.time or not _reads_coverage(plan, config):
        return False
    if plan.time.get("window_total"):
        return True
    if _query_requires_dense_series(plan, config) or _retained_filtered_series(plan, config):
        return True
    unrouted = [replace(row, aggregate_relation_id="") for row in plan.measure_plans]
    return len(_measure_plan_groups(replace(plan, measure_plans=unrouted), config)) > 1


def _needs_time_scope(plan: LogicalPlan, config: PackageConfig) -> bool:
    """Whether base leaves record their scope: for coverage, or a window's observation."""
    return _emits_time_coverage(plan, config) or bool(
        plan.time
        and (plan.time.get("start") or plan.time.get("end"))
        and _dialect(config).has_time_coverage
    )


# What a measure plans paths for itself: its filter and expression, never the query's filters.
_AUTHORED_PURPOSES = frozenset({"aggregate_if", "measure_expr", "metric_filter"})


def _filtered_series_candidate(plan: LogicalPlan, row: MeasurePlan, config: PackageConfig) -> bool:
    """An additive time series whose authored filter can remove observed buckets."""
    bound = row.bound_measure
    return bool(
        (plan.time.get("grain") or plan.time.get("window_total"))
        and resolves_to_zero(bound.aggregation, _measure_index(config)[bound.measure_id])
        and bound.filter_spec
        and any(
            expr_resolves_to_zero(_parse_public_expr(payload), config)
            for payload in plan.post_aggregation_exprs.values()
        )
    )


def _preserves_filtered_series(plan: LogicalPlan, row: MeasurePlan, config: PackageConfig) -> bool:
    """Only plain, local-clock leaves can retain their source rows with a conditional operand,
    and only on a warehouse with loaded coverage, which alone lets a retained bucket read 0.

    Rewrites, predicate populations and earlier settlement keep their existing lowering.
    The runtime diagnoses their missing buckets through a separately authorized source query.
    """
    if not _filtered_series_candidate(plan, row, config):
        return False
    measure = _measure_index(config)[row.bound_measure.measure_id]
    role = _temporal_role_index(config)[
        _leaf_time_role(row.bound_measure, normalize_query(plan.query), config)
    ]
    return bool(
        _dialect(config).has_time_coverage
        and row.rewrite_strategy == "direct"
        and settles_as_a_call()
        and not earlier_settlement_applies()
        and not row.aggregate_relation_id
        and reads_every_row(measure)
        and not _all_metric_predicates(plan, row)
        and not _plan_requires_agent_dag_lowering(plan, config)
        and all(
            expr_resolves_to_zero(_parse_public_expr(payload), config)
            for payload in plan.post_aggregation_exprs.values()
        )
        and (not row.path_selections or _paths_are_single_hop_safe(row.path_selections, config))
        and all(path.analysis.get("status") == "ok" for path in row.path_selections)
        and _dimension_index(config)[role.dimension].entity == measure.entity
        and _entity_in_terms_of_anchor_plan(plan, row, config) is None
    )


def _retained_filtered_series(plan: LogicalPlan, config: PackageConfig) -> set[str]:
    """The leaves that retain their source buckets: each leaf of a group whose leaves all can.

    The one rule lowering, the guard and routing share: a plan that retains one emits loaded
    coverage (``_emits_time_coverage``), which gates every retained bucket's 0.
    """
    return {
        row.bound_measure.alias
        for group in _measure_plan_groups(plan, config)
        if all(_preserves_filtered_series(plan, row, config) for row in group)
        for row in group
    }


def _filtered_series_operand(value: Any, conditions: list[Any]) -> Any:
    return SqlCase([SqlCaseWhen(_and_conditions(conditions), value)]) if conditions else value


def _require_filtered_series_operand(
    leaf: SqlSelect, row: MeasurePlan, config: PackageConfig
) -> None:
    """Refuse a retained-bucket claim if the leaf bypassed its conditional operand."""
    measure = _measure_index(config)[row.bound_measure.measure_id]
    conditions = []
    for item in _bound_filter_clauses(row.bound_measure, config):
        conditions.append(_filter_condition_for_item(measure.entity, item, config))
    aggregate = next(
        field.expression for field in leaf.select if field.alias == row.bound_measure.alias
    )
    expected = SqlCase(
        [
            SqlCaseWhen(
                _and_conditions(conditions), _config_expr_to_sql(measure.expr, measure, config)
            )
        ]
    )
    if not isinstance(aggregate, SqlCall) or aggregate.args != [expected]:
        raise SemanticLayerError(
            "EMPTY_GROUPS_UNSETTLED",
            "A filtered series bypassed its conditional operand.",
            details={"missing": "filtered_series_operand"},
        )


def _edge_cap(
    measure: MeasureConfig,
    role_id: str,
    config: PackageConfig,
    *,
    entity: str = "",
) -> list[Any]:
    """What a measure's declared edge (``zero_through``) lets the leaf that reads it on the
    clock ``role_id`` (an untimed read: the measure's default clock) read: the rows at or
    before the edge (``declared_cap``). None past it exist for anything the leaf feeds, nested
    predicates and windows included. A clock on another model isn't capped; its edge isn't
    read there either (``_record_time_scope``)."""
    role_id = role_id or measure.default_temporal_role
    if measure.zero_through is None or not role_id:
        return []
    dialect = _dialect(config)
    refuse_declared_edges([measure], dialect)
    role = _temporal_role_index(config)[role_id]
    dim = _dimension_index(config)[role.dimension]
    direct = _direct_dimension_source_expr(entity or measure.entity, role.dimension, config)
    if direct is None and dim.entity != (entity or measure.entity):
        return []
    entities = _entity_index(config)
    raw = direct[0] if direct is not None else _column_ref(entities[dim.entity].table, dim.column)
    zone = role.column_timezone or role.timezone or "UTC"
    local = _apply_role_timezone(raw, role, config)
    return declared_cap(measure.zero_through, raw, local, zone, dialect)


def _record_time_scope(
    plan: LogicalPlan,
    config: PackageConfig,
    leaf: SqlSelect,
    rows: list[MeasurePlan],
    untimed: list[Any],
    *,
    raw_time: Any,
    bucket: Any,
    local: bool,
) -> None:
    if not plan.time or not _needs_time_scope(plan, config):
        return
    # A leaf with CTEs of its own (an entity filter's population) keeps its joins and
    # conditions: they read those CTEs under the names the leaf's own CTE gives them
    # (_namespaced_scope), so the untimed probe observes the qualifying population.
    nested = bool(leaf.ctes)
    if nested and (
        not isinstance(leaf.from_table, SqlTableRef)
        or leaf.from_table.name in {cte.name for cte in leaf.ctes}
    ):
        return
    if not isinstance(leaf.from_table, SqlTableRef):
        raise SemanticLayerError("EMPTY_GROUPS_UNSETTLED", "A time scope needs a base relation.")
    for row in rows:
        alias = row.bound_measure.alias
        value = next(field.expression for field in leaf.select if field.alias == alias)
        role = _temporal_role_index(config)[
            _leaf_time_role(row.bound_measure, normalize_query(plan.query), config)
        ]
        # A sum or count's declared edge bounds its gated buckets; others keep the last row.
        measure = _measure_index(config)[row.bound_measure.measure_id]
        edge = (
            declared_edge(measure.zero_through, plan.time, role.timezone or "UTC", _dialect(config))
            if measure.zero_through is not None
            and local
            and _emits_time_coverage(plan, config)
            and resolves_to_zero(row.bound_measure.aggregation, measure)
            else None
        )
        record_leaf_scope(
            alias,
            LeafScope(
                from_table=leaf.from_table,
                joins=tuple(leaf.joins),
                where=tuple(untimed),
                value=value,
                bucket=_coverage_bucket(plan, config, raw_time, bucket, role) if local else None,
                raw_time=raw_time,
                storage_zone=role.column_timezone or role.timezone or "UTC",
                bounded=bool(plan.time.get("start") or plan.time.get("end")),
                edge=edge,
            ),
        )


def _coverage_bucket(
    plan: LogicalPlan, config: PackageConfig, raw_time: Any, bucket: Any, role: Any
) -> Any:
    """The bucket loaded coverage compares: the leaf's own, or for a window total the whole
    [start, end) interval as bucket 0 and times before/after it as -1/1, using the source
    clock's bound semantics, so coverage reads it just as a grained bucket."""
    if not plan.time.get("window_total"):
        return bucket
    window = _source_time_window(
        _apply_role_timezone(raw_time, role, config), plan.time, config, role_id=role.id
    )
    return SqlCase(
        [SqlCaseWhen(SqlIsNull(raw_time), SqlLiteral(None))]
        + [
            SqlCaseWhen(build_negation(condition), SqlLiteral(-1 if condition.op == ">=" else 1))
            for condition in window
        ],
        else_expr=SqlLiteral(0),
    )


def _record_balance_edge(
    plan: LogicalPlan,
    config: PackageConfig,
    row: MeasurePlan,
    relation: str,
    raw_time: Any,
    bucket: Any,
) -> None:
    """A balance's loaded coverage on its own clock, to say where its data ends."""
    if not _reads_coverage(plan, config):
        return
    role = _temporal_role_index(config)[
        _leaf_time_role(row.bound_measure, normalize_query(plan.query), config)
    ]
    scope = LeafScope(
        from_table=SqlTableRef(name=relation),
        joins=(),
        where=(),
        value=None,
        bucket=_coverage_bucket(plan, config, raw_time, bucket, role),
        raw_time=raw_time,
        storage_zone=role.column_timezone or role.timezone or "UTC",
    )
    record_balance_scope(row.bound_measure.alias, scope, _dialect(config))


def _dataset_scope(
    row: MeasurePlan, config: PackageConfig, owned: LeafScope | None = None, role_id: str = ""
) -> LeafScope | None:
    """A sum or count's own rows under its authored conditions (its filter, its CASE, the joins
    they need), without the query's filters or window: what ``dataset`` observation probes,
    whichever leaf answers. Its own metric predicates (a scoped aggregate's, over all time, a
    window or each period) are read through the leaf's joins to their sets (``owned``, renamed
    as the leaf's CTE names them). None for a predicate it can't read that way, a fan-out or a
    hop valid over time, and for a nested CASE, whose settlement can't tell unknown amounts
    from no rows: the guard then refuses."""
    bound = row.bound_measure
    measure = _measure_index(config)[bound.measure_id]
    paths = [s for s in row.path_selections if s.purpose in _AUTHORED_PURPOSES]
    if (
        not resolves_to_zero(bound.aggregation, measure)
        or measure.lookup_from
        or has_nested_case(measure)
        or (_bound_metric_predicates(bound) and owned is None)
        or any(
            s.analysis.get("status") != "ok" or _path_has_temporal_validity(s.chosen_path, config)
            for s in paths
        )
    ):
        return None
    where = []
    for item in _bound_filter_clauses(bound, config):
        where.append(_filter_condition_for_item(measure.entity, item, config))
    # Rows past a declared edge are never read, so they observe nothing either.
    where.extend(_edge_cap(measure, role_id, config))
    relation = _entity_index(config)[measure.entity].table
    joins = [*_joins_for_paths(measure.entity, paths, config, measure_entity=measure.entity)]
    if owned is not None and _bound_metric_predicates(bound):
        joins.extend(owned.joins)
        where.extend(owned.where)
        # Every column the predicates' joins read must come from a relation the probe reads.
        readable = {relation} | {
            name
            for join in joins
            for name in (getattr(join.table, "name", ""), getattr(join.table, "alias", ""))
            if name
        }
        if any(
            isinstance(node, SqlIdentifier)
            and len(node.parts) > 1
            and ".".join(node.parts[:-1]) not in readable
            for node in sql_nodes([[join.on for join in owned.joins if join.on], list(owned.where)])
        ):
            return None
    return LeafScope(
        from_table=SqlTableRef(name=relation),
        joins=tuple(joins),
        where=tuple(where),
        value=SqlCall("COUNT", [_config_expr_to_sql(measure.expr, measure, config)]),
    )


# Each root leaf's joins to its own predicates' sets and their row conditions, by output alias.
_owned_predicate_reads: ContextVar[dict[str, LeafScope] | None] = ContextVar(
    "owned_predicate_reads", default=None
)


@contextmanager
def _recording_owned_predicate_reads() -> Iterator[dict[str, LeafScope]]:
    reads: dict[str, LeafScope] = {}
    with holding(_owned_predicate_reads, reads):
        yield reads


def _is_window_total_key(expr: Any, time_alias: str) -> bool:
    """Whether ``expr`` can only ever hold the window-total constant.

    That is the constant itself, or a reference to a column a lower select already
    named ``time_alias`` (the combine step passes the key up, possibly through
    ``COALESCE``). A raw column under that alias still varies row to row.
    """
    if isinstance(expr, SqlCase):
        return (
            len(expr.whens) == 1
            and isinstance(expr.whens[0].condition, SqlIsNull)
            and expr.whens[0].result == SqlLiteral(0)
            and expr.else_expr == SqlLiteral(0)
        )
    if isinstance(expr, SqlIdentifier):
        return bool(expr.parts) and expr.parts[-1] == time_alias
    if isinstance(expr, SqlCall):
        return bool(expr.args) and all(_is_window_total_key(arg, time_alias) for arg in expr.args)
    return False


def _varying_time_keys(query: Any, time_alias: str) -> list[str]:
    """Time keys in ``query`` and its CTEs that are not the window-total constant."""
    if isinstance(query, SqlSetQuery):
        return [key for branch in query.queries for key in _varying_time_keys(branch, time_alias)]
    found = [
        item.alias
        for item in query.select
        if item.alias == time_alias and not _is_window_total_key(item.expression, time_alias)
    ]
    for cte in query.ctes:
        found.extend(f"{cte.name}.{key}" for key in _varying_time_keys(cte.query, time_alias))
    return found


def _refuse_varying_time_key(plan: LogicalPlan, leaf: Any) -> None:
    """Refuse a window total whose leaf still groups by the raw time column.

    The final SELECT drops the time column of a window total. Dropping one that
    still varies would return several rows per group as if they were one total.
    """
    if not plan.time.get("window_total"):
        return
    if varying := _varying_time_keys(leaf, _time_alias_for_plan(plan)):
        raise window_total_refusal(
            plan.time, "part of this query still groups by the raw time", time_keys=varying
        )


_CONVERTED_ROLE_PATHS = {
    "entity_only_predicate_window": "Entity-only predicate time windows",
    "predicate_period_join": "Metric predicates joined on the query's time period",
    "conversion_metric": "Conversion metrics",
}


def _refuse_converted_role(role: Any, path: str) -> None:
    """Refuse a path that reads ``role``'s raw stored column when the role converts.

    Such a path would compare or bucket stored values against role-local ones.
    """
    zones = _role_conversion(role)
    if zones is None:
        return
    column_tz, target_tz = zones
    raise SemanticLayerError(
        "WINDOWED_TIME_FILTER_UNSUPPORTED",
        f"{_CONVERTED_ROLE_PATHS[path]} do not support timezone conversion for temporal "
        f"role '{role.id}' ({column_tz} to {target_tz}); use an unconverted role.",
        details={
            "temporal_role": role.id,
            "column_timezone": column_tz,
            "timezone": target_tz,
            "path": path,
        },
    )


def _metric_filter_alias(index: int) -> str:
    return f"metric_filter__{index + 1}"


def _query_key_aliases(plan: LogicalPlan) -> list[str]:
    aliases = list(plan.group_by)
    if plan.time:
        aliases.append(time_output_alias(plan.time))
    return aliases


def _expr_contains_distribution(expr: SemanticExpr, config: PackageConfig) -> bool:
    if isinstance(expr, DistributionExpr):
        return True
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return recipe is not None and _expr_contains_distribution(recipe.expression, config)
    if isinstance(expr, (EntityValueExpr, TotalExpr)):
        return _expr_contains_distribution(expr.input, config)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_contains_distribution(expr.left, config) or _expr_contains_distribution(
            expr.right, config
        )
    if isinstance(expr, (BooleanExpr, CallExpr)):
        return any(_expr_contains_distribution(arg, config) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _expr_contains_distribution(item.when, config)
            or _expr_contains_distribution(item.then, config)
            for item in expr.whens
        ) or (expr.else_expr is not None and _expr_contains_distribution(expr.else_expr, config))
    if isinstance(expr, OffsetWindowExpr):
        return _expr_contains_distribution(expr.input, config)
    return False


def _plan_requires_agent_dag_lowering(plan: LogicalPlan, config: PackageConfig) -> bool:
    for expr_payload in plan.post_aggregation_exprs.values():
        if _expr_contains_distribution(_parse_public_expr(expr_payload), config):
            return True
    return False


def _value_filter_condition(
    value_expr: Any, item: dict[str, Any], config: PackageConfig, *, path: str = "where"
) -> Any:
    # Single shared op/value → condition mapping; see
    # ``sql_ast.build_filter_condition`` for the NULL / IN semantics.
    field = str(item.get("field", "") or "")
    op, value = item.get("op", "="), item.get("value")
    return dimension_filter(value_expr, field, op, value, config, path=path)


def _filter_condition_for_item(
    measure_entity: str, item: dict[str, Any], config: PackageConfig
) -> Any:
    """``item``'s condition on the measure's own column when it has one, else on its dimension."""
    expr, _ = _direct_dimension_source_expr(
        measure_entity, str(item["field"]), config
    ) or _resolve_dimension_expr(str(item["field"]), config)
    return _value_filter_condition(expr, item, config)


def _validate_metric_predicate_filter_envelope(item: dict[str, Any]) -> None:
    if operators.word_of(item.get("op", "is")) == "is" and item.get("value") is True:
        return
    raise SemanticLayerError(
        "INVALID_METRIC_FILTER",
        "metric_predicate filters must use op 'is' and value true; put the threshold inside expression.op/expression.value",
        details={"op": item.get("op", "is"), "value": item.get("value")},
    )


def _query_metric_predicates(plan: LogicalPlan) -> list[MetricPredicateExpr]:
    predicates: list[MetricPredicateExpr] = []
    for item in list(plan.query.get("metric_filters", []) or []):
        raw_expr = item.get("expression")
        if not raw_expr:
            continue
        expr = _parse_public_expr(dict(raw_expr))
        if isinstance(expr, MetricPredicateExpr):
            _validate_metric_predicate_filter_envelope(dict(item))
            predicates.append(expr)
    return predicates


def _time_alias_for_plan(plan: LogicalPlan) -> str:
    if not plan.time:
        return ""
    return time_output_alias(plan.time)


def _and_conditions(conditions: list[Any]) -> Any:
    current = conditions[0]
    for condition in conditions[1:]:
        current = SqlBinary(current, "AND", condition)
    return current


def _paths_are_single_hop_safe(
    path_selections: Iterable[PathSelection], config: PackageConfig
) -> bool:
    relationships = _relationship_index(config)
    selections = list(path_selections)
    if not selections:
        return False
    for selection in selections:
        if len(selection.chosen_path) != 1:
            return False
        rel = relationships.get(selection.chosen_path[0])
        if rel is None:
            return False
        if rel.temporal_validity:
            return False
        if rel.cardinality not in {"1:1", "N:1"}:
            return False
        if str(rel.safety or "safe").lower() != "safe":
            return False
    return True


def _anchor_path_selection(
    *,
    config: PackageConfig,
    plan: LogicalPlan,
    start_entity: str,
    target_entity: str,
    purpose: str,
) -> PathSelection | None:
    selection = _path_selection(
        config=config,
        query=normalize_query(plan.query),
        start_entity=start_entity,
        target_entity=target_entity,
        purpose=purpose,
    )
    return selection if selection is not None and selection.analysis.get("status") == "ok" else None


def _anchor_route_agrees(
    config: PackageConfig,
    *,
    root_entity: str,
    anchor_entity: str,
    anchor_to_root: list[str],
    target_entity: str,
) -> bool:
    """True when the anchor's own route to ``target_entity`` is the root's route to it seen
    from the anchor: the hops back to the root, then the root's route, where a root route
    that starts back through the anchor continues from the anchor's own row."""
    try:
        root_route, _ = resolve_path(config, start=root_entity, target=target_entity)
        anchor_route, _ = resolve_path(config, start=anchor_entity, target=target_entity)
    except SemanticLayerError:
        return False
    shared = 0
    while (
        shared < min(len(anchor_to_root), len(root_route))
        and anchor_to_root[-1 - shared] == root_route[shared]
    ):
        shared += 1
    return anchor_route == [*anchor_to_root[: len(anchor_to_root) - shared], *root_route[shared:]]


def _entity_in_terms_of_anchor_plan(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> dict[str, Any] | None:
    measures = _measure_index(config)
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    relationships = _relationship_index(config)
    measure = measures[measure_plan.bound_measure.measure_id]
    if (
        str(measure_plan.bound_measure.aggregation or measure.default_aggregation or "").lower()
        != "count_distinct"
    ):
        return None
    if measure.measure_class not in {"event_count", "distinct_population"}:
        return None
    if not isinstance(measure.expr, ColumnRefExpr) or measure.expr.entity or measure.expr.table:
        return None
    measure_key_columns = list(entities[measure.entity].key_columns)
    if measure.expr.column not in set(measure_key_columns):
        return None
    if _all_metric_predicates(plan, measure_plan):
        return None
    if child_groups(plan.query.get("where")):
        return None  # a child group's leaf is the semi-join leaf

    anchor_entity = ""
    anchor_key_columns: list[str] = []
    anchor_relationship_id = ""
    transformed_selections: list[PathSelection] = []
    for selection in measure_plan.path_selections:
        if selection.analysis.get("status") == "ok":
            continue
        if (
            selection.purpose != "group_by"
            or selection.analysis.get("status") != "rewrite_required"
        ):
            return None
        current_entity = measure.entity
        selected_anchor = ""
        selected_key_columns: list[str] = []
        remaining_path: list[str] = []
        for index, rel_id in enumerate(selection.chosen_path):
            rel = relationships[rel_id]
            if current_entity == rel.source_entity:
                current_entity = rel.target_entity
                continue
            if current_entity != rel.target_entity:
                return None
            reverse_safe = {
                str(item).lower() for item in list(rel.rollup_safe_aggregations_reverse or [])
            }
            if "count_distinct" not in reverse_safe:
                return None
            if not selected_anchor:
                parent = _entity_in_terms_of_parent_relationship(
                    rel.source_entity, measure.entity, config
                )
                if index != 0 or parent is None or parent.id != rel.id:
                    return None
                target_to_source = dict(
                    zip(
                        list(rel.target_columns or [rel.target_column]),
                        list(rel.source_columns or [rel.source_column]),
                        strict=True,
                    )
                )
                if not all(column in target_to_source for column in measure_key_columns):
                    return None
                selected_anchor = rel.source_entity
                selected_key_columns = [target_to_source[column] for column in measure_key_columns]
                anchor_relationship_id = rel.id
                remaining_path = list(selection.chosen_path[index + 1 :])
            current_entity = rel.source_entity
        if not selected_anchor:
            return None
        if anchor_entity and (
            selected_anchor != anchor_entity or selected_key_columns != anchor_key_columns
        ):
            return None
        anchor_entity = selected_anchor
        anchor_key_columns = selected_key_columns
        transformed_selections.append(
            PathSelection(
                target_entity=selection.target_entity,
                purpose="entity_in_terms_of_group_by",
                chosen_path=remaining_path,
                candidate_paths=[],
                analysis={
                    "status": "entity_in_terms_of",
                    "original_path": list(selection.chosen_path),
                    "anchor_entity": selected_anchor,
                },
            )
        )
    if not anchor_entity:
        return None
    supplemental_selections: list[PathSelection] = []
    lookup = "entity_in_terms_of_lookup"
    required_dimension_ids = [(dim_id, lookup) for dim_id in plan.group_by]
    required_dimension_ids.extend(
        (str(item.get("field")), lookup)
        for item in refuse_child_groups(plan.query.get("where"), "in a child-grain count")
        if item.get("field")
    )
    required_dimension_ids.extend(
        (str(item.get("field")), lookup)
        for item in _bound_filter_clauses(measure_plan.bound_measure, config)
        if item.get("field")
    )
    if plan.time:
        role = _temporal_role_index(config)[
            _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config)
        ]
        # Read through a lookup, the time role joins INNER as in every other leaf.
        required_dimension_ids.append((role.dimension, "time"))

    transformed_by_target = {row.target_entity: row for row in transformed_selections}
    covered_targets = {(target, lookup) for target in transformed_by_target}
    for dim_id, purpose in dict.fromkeys(required_dimension_ids):
        dim = dimensions.get(dim_id)
        if dim is None:
            return None
        if dim.entity == anchor_entity:
            continue
        # A key read from the anchor's own column, or a lookup the anchor resolves itself, must
        # be the measure entity's route (``resolve_path``) seen from the anchor, else the
        # anchor's rows would read another row than the question means. Only a check: a read
        # is noted when the leaf lowers it, never when the rewrite declines.
        with recording_route_choices():
            direct = _direct_dimension_source_expr(anchor_entity, dim_id, config) is not None
        if (direct or dim.entity not in transformed_by_target) and not _anchor_route_agrees(
            config,
            root_entity=measure.entity,
            anchor_entity=anchor_entity,
            anchor_to_root=[anchor_relationship_id],
            target_entity=dim.entity,
        ):
            return None
        if direct or (dim.entity, purpose) in covered_targets:
            continue
        new_selection = (
            # The time role, read on a grouping's own path.
            replace(transformed_by_target[dim.entity], purpose=purpose)
            if dim.entity in transformed_by_target
            else _anchor_path_selection(
                config=config,
                plan=plan,
                start_entity=anchor_entity,
                target_entity=dim.entity,
                purpose=purpose,
            )
        )
        if new_selection is None:
            return None
        supplemental_selections.append(new_selection)
        covered_targets.add((dim.entity, purpose))
    # A dimension some rollup of the measure's model holds joins INNER on the measure's own
    # path (``_joins_for_paths``); this leaf starts from another entity, so it leaves the query
    # to the measure's own leaf.
    if rollup_held_lookups(
        config, measure.entity, [*supplemental_selections, *transformed_selections]
    ):
        return None
    # An anchor row whose parent has no record is none of the measure's entities: the hops back
    # to the measure's own entity join INNER, and the lookups past it keep rows.
    root_paths: set[tuple[str, ...]] = set()
    for row in supplemental_selections:
        if row.purpose != lookup:
            continue  # a time role's hops join INNER already
        current_entity = anchor_entity
        for index, rel_id in enumerate(row.chosen_path):
            rel = relationships[rel_id]
            current_entity = (
                rel.target_entity if current_entity == rel.source_entity else rel.source_entity
            )
            if current_entity == measure.entity:
                root_paths.add(tuple(row.chosen_path[: index + 1]))
                break
    # The counted entity must exist even when every grouping reads only the child's rows
    # or its lookups, so no supplemental dimension path happens to reach the parent.
    root = PathSelection(
        target_entity=measure.entity,
        purpose="entity_in_terms_of_root",
        chosen_path=[anchor_relationship_id],
        candidate_paths=[[anchor_relationship_id]],
        analysis=analyze_fanout(config, anchor_entity, [anchor_relationship_id]),
    )
    if root_paths and root_paths != {tuple(root.chosen_path)}:
        return None
    supplemental_selections.append(root)
    return {
        "anchor_entity": anchor_entity,
        "anchor_key_columns": anchor_key_columns,
        "path_selections": [*supplemental_selections, *transformed_selections],
        "rewritten_path_selections": transformed_selections,
        "supplemental_path_selections": supplemental_selections,
        "omitted_root_entity": measure.entity,
    }


def _path_signature(path_selections: Iterable[PathSelection]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(selection.chosen_path) for selection in path_selections)


def _foldable_leaf_signature(
    plan: LogicalPlan, measure_plan: MeasurePlan, query: NormalizedQuery, config: PackageConfig
) -> tuple[Any, ...] | None:
    measures = _measure_index(config)
    measure = measures[measure_plan.bound_measure.measure_id]
    if measure.measure_class == "semi_additive" or measure_plan.rewrite_strategy in {
        "parent_lookup",
        "parent_value",
        "child_values",
    }:
        return None
    if _all_metric_predicates(plan, measure_plan):
        return None
    if any(row.analysis.get("status") != "ok" for row in measure_plan.path_selections):
        return None  # a one-to-many hop: the leaf rewrites (entity_in_terms_of, fanout_dedup)
    if any(row.purpose == "aggregate_if" for row in measure_plan.path_selections):
        return None  # a folded scan joins as its first leaf does; this leaf's joins are its own
    if measure_plan.path_selections and not _paths_are_single_hop_safe(
        measure_plan.path_selections, config
    ):
        return None
    return (
        "foldable_leaf",
        getattr(measure_plan, "aggregate_relation_id", "") or "",
        measure.entity,
        measure.zero_through,  # one scan reads one edge
        _path_signature(measure_plan.path_selections),
        tuple(plan.group_by),
        _freeze_payload(plan.time),
        _leaf_time_role(measure_plan.bound_measure, query, config),
        _freeze_payload(plan.query.get("where", []) or []),
        _freeze_payload(measure_plan.bound_measure.filter_spec),
        _preserves_filtered_series(plan, measure_plan, config),
    )


def _measure_plan_groups(plan: LogicalPlan, config: PackageConfig) -> list[list[MeasurePlan]]:
    grouped: dict[tuple[Any, ...], list[MeasurePlan]] = {}
    ordered: list[tuple[Any, ...]] = []
    query = normalize_query(plan.query)
    for measure_plan in plan.measure_plans:
        signature = _foldable_leaf_signature(plan, measure_plan, query, config)
        if signature is None:
            signature = ("single_leaf", measure_plan.cte_name)
        if signature not in grouped:
            grouped[signature] = []
            ordered.append(signature)
        grouped[signature].append(measure_plan)
    return [grouped[key] for key in ordered]


def _query_requires_dense_series(plan: LogicalPlan, config: PackageConfig) -> bool:
    if plan.time and plan.time.get("fill"):
        return True
    if any(
        _expr_requires_dense_series(_parse_public_expr(expr), config)
        for expr in plan.post_aggregation_exprs.values()
    ):
        return True
    return _metric_filters_require_dense_series(plan, config)


def _metric_filters_require_dense_series(plan: LogicalPlan, config: PackageConfig) -> bool:
    for item in list(plan.query.get("metric_filters", []) or []):
        expr = _parse_public_expr(dict(item["expression"]))
        if not isinstance(expr, MetricPredicateExpr) and _expr_requires_dense_series(expr, config):
            return True
    return False


def _calendar_bound(value: Any) -> datetime | None:
    """Parse a bound without projecting it through the finite datetime range."""
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _offset_microseconds(offset: timedelta) -> int:
    return ((offset.days * 86400 + offset.seconds) * 1_000_000) + offset.microseconds


def _instant_tick(moment: datetime, value: Any) -> tuple[int, Decimal]:
    """Represent an instant without constructing an out-of-range UTC datetime."""
    offset = moment.utcoffset() or timedelta()
    micros = (
        (moment.toordinal() * 86400 + moment.hour * 3600 + moment.minute * 60 + moment.second)
        * 1_000_000
        + moment.microsecond
        - _offset_microseconds(offset)
    )
    return micros, _fractional_second(value) - Decimal(moment.microsecond) / 1_000_000


def _calendar_day(moment: datetime, value: Any, zone: tzinfo) -> tuple[int, bool]:
    """Return the role-zone day and whether the bound is after its midnight."""
    if moment.tzinfo is None:
        local = moment
    else:
        try:
            local = moment.astimezone(zone)
        except OverflowError:
            # UTC may be outside years 1..9999 even when the role-zone day is in range.
            # ZoneInfo's edge offset projects that instant without a UTC datetime.
            edge = datetime.min if moment.year == 1 else datetime.max
            role_offset = zone.utcoffset(edge.replace(tzinfo=zone)) or timedelta()
            instant_us, tail = _instant_tick(moment, value)
            local_us = instant_us + _offset_microseconds(role_offset)
            ordinal, day_us = divmod(local_us, 86_400_000_000)
            if 1 <= ordinal <= datetime.max.toordinal():
                seconds, micros = divmod(day_us, 1_000_000)
                hours, seconds = divmod(seconds, 3600)
                minutes, seconds = divmod(seconds, 60)
                local = datetime.fromordinal(ordinal).replace(
                    hour=hours, minute=minutes, second=seconds, microsecond=micros, tzinfo=zone
                )
                checked_offset = local.utcoffset() or timedelta()
                if checked_offset != role_offset:
                    local_us = instant_us + _offset_microseconds(checked_offset)
                    ordinal, day_us = divmod(local_us, 86_400_000_000)
            return ordinal, bool(day_us or tail)
    return local.toordinal(), bool(local.time() != datetime.min.time() or _fractional_second(value))


def _nonpositive_window(
    start: datetime, end: datetime, zone: tzinfo, start_value: Any, end_value: Any
) -> bool:
    if start.tzinfo is not None or end.tzinfo is not None:
        return _instant_tick(
            start if start.tzinfo is not None else start.replace(tzinfo=zone), start_value
        ) >= _instant_tick(end if end.tzinfo is not None else end.replace(tzinfo=zone), end_value)
    return (start, _fractional_second(start_value)) >= (end, _fractional_second(end_value))


def _whole_day_window(day: Any, time: dict[str, Any], config: PackageConfig) -> list[Any]:
    """``day`` within the days that ``[start, end)`` touches.

    Offset bounds use the temporal role's calendar zone. Naive bounds remain wall time.
    An empty interval has no spine, even if outward day rounding would make one.
    """
    role = _temporal_role_index(config).get(str(time.get("temporal_role") or ""))
    zone_name = str(getattr(role, "timezone", "UTC") or "UTC")
    zone = UTC if zone_name == "UTC" else ZoneInfo(zone_name)
    moments = {key: _calendar_bound(time.get(key)) for key in ("start", "end")}
    start, end = moments["start"], moments["end"]
    if (
        start is not None
        and end is not None
        and _nonpositive_window(start, end, zone, time["start"], time["end"])
    ):
        return [SqlBinary(SqlLiteral(1), "=", SqlLiteral(0))]
    bounds = []
    for key, operator in (("start", ">="), ("end", "<")):
        value = time.get(key)
        if value is None:
            continue
        moment = moments[key]
        if moment is not None and len(str(value).strip()) > 10:
            ordinal, after_midnight = _calendar_day(moment, value, zone)
            if (key == "start" and ordinal > datetime.max.toordinal()) or (
                key == "end" and ordinal < 1
            ):
                return [SqlBinary(SqlLiteral(1), "=", SqlLiteral(0))]
            day_value = datetime.fromordinal(min(max(ordinal, 1), datetime.max.toordinal()))
            if key == "end" and (after_midnight or ordinal > datetime.max.toordinal()):
                operator = "<="
            value = day_value.date().isoformat()
        bounds.append(SqlBinary(day, operator, SqlLiteral(value, temporal=True)))
    return bounds


def _source_time_window(
    column: Any, time: dict[str, Any], config: PackageConfig, *, role_id: str = ""
) -> list[Any]:
    """Every source scan uses the clock's day rule, also used by the calendar spine."""
    role_id = role_id or str(time.get("temporal_role") or "")
    role = _temporal_role_index(config).get(role_id)
    if role is None:
        raise SemanticLayerError("INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{role_id}'")
    dimension = _dimension_index(config)[role.dimension]
    if dimension.data_type == "date":
        for key in ("start", "end"):
            if time.get(key) is not None and _calendar_bound(time[key]) is None:
                raise SemanticLayerError(
                    "INVALID_QUERY",
                    f"time.{key} must be an ISO date or timestamp for a DATE clock",
                    details={"path": f"time.{key}"},
                )
        # A converted DATE is a timestamp; compare its role-local day. Logical
        # field identifiers remain intact for _predicate_window_filters.
        column_tz = str(role.column_timezone or "").strip()
        target_tz = str(role.timezone or "").strip()
        if (
            column_tz
            and target_tz
            and column_tz != target_tz
            and column != SqlIdentifier(parts=[role.dimension])
        ):
            column = SqlCast(column, "DATE")
        return _whole_day_window(column, {**time, "temporal_role": role_id}, config)
    bounds = [
        SqlBinary(column, operator, SqlLiteral(time[key], temporal=True))
        for key, operator in (("start", ">="), ("end", "<"))
        if time.get(key) is not None
    ]
    if time.get("end") is None or not (time.get("this") or time.get("to_now")):
        return bounds
    # The end is now. Wall time repeats an hour each autumn, so the cutoff is an instant;
    # wall time only buckets periods and starts them.
    zones = _role_conversion(role)
    if zones:
        # A converted clock compares its stored value with now in the storage zone.
        stored = _stored_time(column, zones, config)
        if stored is not None:
            cutoff = request_now().astimezone(ZoneInfo(zones[0])).replace(tzinfo=None)
            bounds[-1] = SqlBinary(stored, "<", SqlLiteral(cutoff.isoformat(), temporal=True))
    elif str(role.timezone or "UTC") != "UTC" and not str(role.column_timezone or "").strip():
        # A plain timestamp keeps its wall time (the bound above); a zone-aware one compares
        # instants with now. Only a query run in the clock's zone can tell them apart.
        if not _dialect(config).session_zone:
            from ..query_call import refusal

            raise refusal(
                "time.this" if time.get("this") else "time.start",
                f"a window that ends at now can't be cut on {config.package.warehouse}: clock "
                f"'{role.id}' reads its timestamps in {role.timezone} with no column_timezone, "
                "and a plain timestamp and a zone-aware one need different cutoffs",
                fix="Declare the clock's column_timezone (the zone its timestamps are stored "
                "in; UTC for instant columns such as BigQuery TIMESTAMP), or use a dated window.",
                temporal_role=role.id,
            )
        now = SqlCast(SqlLiteral(request_now().astimezone(UTC).isoformat(sep=" ")), "TIMESTAMPTZ")
        aware = SqlBinary(
            SqlCast(SqlCall("PG_TYPEOF", [column]), "VARCHAR"),
            "=",
            SqlLiteral("timestamp with time zone"),
        )
        before = SqlBinary(SqlCast(column, "TIMESTAMPTZ"), "<", now)
        bounds[-1] = SqlBinary(
            SqlLiteral(True), "=", SqlCase([SqlCaseWhen(aware, before)], bounds[-1])
        )
    return bounds


def _stored_time(column: Any, zones: tuple[str, str], config: PackageConfig) -> Any | None:
    """``column`` before the dialect's conversion between ``zones``; None when it isn't one."""
    hole = SqlIdentifier(parts=["stored"])

    def match(pattern: Any, value: Any) -> Any | None:
        if pattern is hole:
            return value
        if isinstance(pattern, SqlCast):
            same = isinstance(value, SqlCast) and pattern.type_name == value.type_name
            return match(pattern.expr, value.expr) if same else None
        if not (isinstance(pattern, SqlCall) and isinstance(value, SqlCall)) or (
            pattern.name,
            len(pattern.args),
        ) != (value.name, len(value.args)):
            return None
        found = None
        for part, other in zip(pattern.args, value.args, strict=True):
            if part is hole or isinstance(part, SqlCall | SqlCast):
                found = match(part, other)
                if found is None:
                    return None
            elif part != other:
                return None
        return found

    return match(_dialect(config).convert_timezone(*zones, hole), column)


def _all_metric_predicates(
    plan: LogicalPlan, measure_plan: MeasurePlan
) -> list[MetricPredicateExpr]:
    predicates = [
        *_bound_metric_predicates(measure_plan.bound_measure),
        *_query_metric_predicates(plan),
    ]
    dedup: dict[str, MetricPredicateExpr] = {}
    for predicate in predicates:
        dedup[_freeze_payload(expr_to_dict(predicate))] = predicate
    return list(dedup.values())


def _path_has_temporal_validity(path: list[str], config: PackageConfig) -> bool:
    relationships = _relationship_index(config)
    return any(
        bool(relationships.get(rel_id) and relationships[rel_id].temporal_validity)
        for rel_id in list(path or [])
    )
