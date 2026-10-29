"""Measure leaves: each measure's own SELECT, from its source rows, a rollup, a parent lookup,
an entity expressed through its parent, a fan-out deduplication or a semi-additive snapshot,
with its filters, child groups and time bucket.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from ..acceleration.routing import (
    record_rollup_scan,
)
from ..acceleration.selection import recombine_aggregation
from ..ast import (
    ChildGroup,
    child_groups,
    is_child_group,
    normalize_query,
    plain_filters,
    refuse_child_groups,
    time_output_alias,
)
from ..config_parts.lookup_measures import lookup_links
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..fanout import recording_route_choices
from ..ir import (
    LogicalPlan,
    MeasurePlan,
    PathSelection,
)
from ..schema import (
    AggregateRelationConfig,
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlExists,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from . import nested
from .bind import (
    _aggregation_expr,
    _bound_filter_clauses,
    _bound_metric_predicates,
    _config_expr_to_sql,
)
from .child_values import CHILD_VALUES, child_value_dimension, child_values_cte, keep_rows
from .dependencies import (
    cut_owners,
    leaf_predicate_roles,
    measure_objects,
    project_is_cut,
    record_bound_object,
    settles_as_a_call,
)
from .empty_groups import (
    LeafScope,
    absent_entities_gate,
    has_nested_case,
    observation_scope,
    reads_every_row,
    recording_zero_outputs,
    require_settled_source,
    resolves_to_zero,
    sql_nodes,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _relationship_index,
    _temporal_role_index,
)
from .listings import is_listing_plan, listing_rows
from .lowering_common import (
    _aggregate_relation_index,
    _all_metric_predicates,
    _and_conditions,
    _apply_role_timezone,
    _dialect,
    _edge_cap,
    _entity_in_terms_of_anchor_plan,
    _filter_condition_for_item,
    _filtered_series_operand,
    _owned_predicate_reads,
    _paths_are_single_hop_safe,
    _preserves_filtered_series,
    _query_metric_predicates,
    _record_all_time_scope,
    _record_balance_edge,
    _record_time_scope,
    _selected_aggregate_relation,
    _snapshot_series_columns,
    _source_time_window,
    _time_bucket_expr,
    _value_filter_condition,
)
from .namespacing import _namespace_sql_select
from .parent_values import PARENT_VALUE, parent_value_leaf_select
from .paths import (
    _column_ref,
    _direct_dimension_source_expr,
    _entity_key_dimension_ids,
    _joins_for_paths,
    _leaf_time_role,
    _resolve_dimension_expr,
    dimension_filter,
    unknown_as_null,
)
from .plan_checks import _validate_non_additive_sums, child_group_route
from .predicate import _predicate_ctes_and_join
from .predicate_sets import (
    _reads_chosen_snapshot,
    _source_join_columns_for_paths,
    _stock_period_split,
)
from .row_counts import _row_markers, _rows_fields
from .snapshot_reads import ClosingDay, SnapshotRead, closing_day, select_snapshots


def _aggregate_relation_time_expr(
    aggregate: AggregateRelationConfig, plan: LogicalPlan, config: PackageConfig
) -> tuple[Any | None, Any | None, str]:
    if not plan.time:
        return None, None, ""
    raw_expr = _column_ref(aggregate.relation, aggregate.time_column)
    time = dict(plan.time)
    time_expr = (
        _dialect(config).date_trunc(time["grain"], raw_expr) if time.get("grain") else raw_expr
    )
    time_alias = time_output_alias(time)
    return raw_expr, time_expr, time_alias


def _aggregate_relation_filter_expr(
    aggregate: AggregateRelationConfig,
    item: dict[str, Any],
    config: PackageConfig,
    *,
    path: str = "where",
) -> Any:
    dim_id = str(item.get("field", "") or "")
    column = aggregate.dimension_columns.get(dim_id)
    if not column:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Aggregate relation '{aggregate.id}' cannot satisfy filter dimension '{dim_id}'",
            details={"aggregate_relation": aggregate.id, "dimension": dim_id},
        )
    return _value_filter_condition(_column_ref(aggregate.relation, column), item, config, path=path)


def _aggregate_relation_leaf_select(
    plan: LogicalPlan,
    measure_plans: list[MeasurePlan],
    aggregate: AggregateRelationConfig,
    config: PackageConfig,
) -> SqlSelect:
    nested = [
        row.bound_measure.measure_id
        for row in measure_plans
        if has_nested_case(_measure_index(config)[row.bound_measure.measure_id])
    ]
    if nested:
        raise SemanticLayerError(
            "EMPTY_GROUPS_UNSETTLED",
            "A measure containing a nested CASE cannot read a rollup.",
            details={"measures": nested, "aggregate_relation": aggregate.id},
        )
    record_rollup_scan(aggregate.id)
    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    for dim_id in plan.group_by:
        column = aggregate.dimension_columns.get(dim_id)
        if not column:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"Aggregate relation '{aggregate.id}' cannot group by '{dim_id}'",
                details={"aggregate_relation": aggregate.id, "dimension": dim_id},
            )
        expr = _column_ref(aggregate.relation, column)
        select_fields.append(SqlField(expr, dim_id))
        group_fields.append(expr)
    raw_time_expr, time_expr, time_alias = _aggregate_relation_time_expr(aggregate, plan, config)
    if time_expr is not None:
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    for item in refuse_child_groups(plan.query.get("where"), "on a rollup"):
        where_clauses.append(_aggregate_relation_filter_expr(aggregate, dict(item), config))
    if raw_time_expr is not None and plan.time:
        where_clauses.extend(
            _source_time_window(raw_time_expr, plan.time, config, role_id=aggregate.temporal_role)
        )

    for measure_plan in measure_plans:
        measure_id = measure_plan.bound_measure.measure_id
        column = aggregate.measure_columns.get(measure_id)
        if not column:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"Aggregate relation '{aggregate.id}' cannot satisfy measure '{measure_id}'",
                details={"aggregate_relation": aggregate.id, "measure": measure_id},
            )
        for item in _bound_filter_clauses(measure_plan.bound_measure, config):
            where_clauses.append(
                _aggregate_relation_filter_expr(
                    aggregate, dict(item), config, path="measure_filter"
                )
            )
        aggregation = recombine_aggregation(aggregate, measure_id)
        held = _column_ref(aggregate.relation, column)
        select_fields.append(
            SqlField(
                _aggregation_expr(
                    held,
                    aggregation,
                    parameters=measure_plan.bound_measure.aggregation_params,
                    dialect=_dialect(config),
                ),
                measure_plan.bound_measure.alias,
            )
        )
        # Each rollup row stands for base rows of its group, but can't say which of them met
        # a CASE measure's condition: that measure gets no row count (routing keeps it off).
        measure = _measure_index(config)[measure_id]
        if reads_every_row(measure):
            select_fields.extend(_rows_fields(measure_plan, measure, held))

    return SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=aggregate.relation),
        where=where_clauses,
        group_by=group_fields,
    )


def _semi_additive_leaf_select(
    *,
    key_fields: list[SqlField],
    period_aliases: list[str],
    row_grain_exprs: list[Any],
    order_expr: Any,
    value_expr: Any,
    leaf_alias: str,
    from_table: SqlTableRef,
    joins: list[SqlJoin],
    where_clauses: list[Any],
    snapshot_filters: list[Any],
    zero_unmatched: bool,
    window_choice: str,
    final_aggregation: str,
    aggregation_params: dict[str, Any],
    warehouse: str,
    ctes: list[SqlCte] | None = None,
    day: ClosingDay | None = None,
    measure_id: str = "",
) -> SqlSelect:
    # A snapshot series whose only row key is the ordering time column is a
    # singleton series: there is exactly one snapshot per point in time. The
    # callers pass row_grain_exprs=[] for that case (the time column never
    # belongs in the partition key, or "last snapshot per key" degenerates to
    # every snapshot and the outer combine sums stocks across periods —
    # e.g. a YTD value 31x overstated at month grain). With no row keys:
    # last/first/sum collapse to one snapshot per output bucket, while
    # distributional aggregations (median/avg/min/max/...) keep every
    # snapshot in the bucket and aggregate across them.
    if not row_grain_exprs and final_aggregation not in {"last_value", "first_value", "sum"}:
        row_grain_exprs = [order_expr]
    row_key_aliases = [f"__snapshot_key_{index + 1}" for index in range(len(row_grain_exprs))]
    key_aliases = [field.alias for field in key_fields]
    fields = [
        *key_fields,
        *[
            SqlField(expr, alias)
            for expr, alias in zip(row_grain_exprs, row_key_aliases, strict=True)
        ],
        SqlField(value_expr, "__snapshot_value"),
    ]
    # `snapshot_filters` (on attributes, never on time) keep or drop each chosen snapshot,
    # as a grouped attribute is read from it: marked here, applied after the choice. With
    # `zero_unmatched`, grouped only by periods, a period whose chosen snapshots all fail
    # them reads 0 (data of nothing); a period with no snapshot still reads NULL (no data).
    if snapshot_filters:
        kept = SqlCase(
            [SqlCaseWhen(_and_conditions(snapshot_filters), SqlLiteral(1))], SqlLiteral(0)
        )
        fields.append(SqlField(kept, "__snapshot_kept"))
    settles = bool(snapshot_filters) and zero_unmatched and set(key_aliases) <= set(period_aliases)
    dialect = dialect_for_warehouse(warehouse)
    # Each series' snapshot is chosen per period first (`period_aliases` name the key
    # fields that split time), and the other grouping dimensions are read from that
    # row. Choosing it per group too would count a series whose attribute changes
    # within the period once under each value it held. An end-of-period balance on an
    # as-of clock (``day``) closes on the period's closing day (snapshot_reads).
    closes = window_choice == "last_value" and final_aggregation in {"last_value", "sum"}
    read = SnapshotRead(
        fields=fields,
        periods=[alias for alias in key_aliases if alias in period_aliases],
        series=row_key_aliases,
        order=order_expr,
        day=day if closes else None,
        first=window_choice == "first_value",
        source=from_table,
        joins=joins,
        where=where_clauses,
        ctes=list(ctes or []),
    )
    chosen, rows = select_snapshots(
        read, name="snapshot_rows", prefix="snapshot", dialect=dialect, measure_id=measure_id
    )
    value: Any = SqlIdentifier(parts=[rows, "__snapshot_value"])
    flag = SqlIdentifier(parts=[rows, "__snapshot_kept"])
    kept_rows: list[Any] = [SqlBinary(flag, "=", SqlLiteral(1))] if snapshot_filters else []
    if settles:
        value = SqlCase([SqlCaseWhen(kept_rows[0], value)])
    final_expr = (
        SqlCall("SUM", [value])
        if final_aggregation in {"last_value", "first_value"}
        else _aggregation_expr(
            value, final_aggregation, parameters=aggregation_params, dialect=dialect
        )
    )
    if settles:
        none_kept = SqlBinary(SqlCall("MAX", [flag]), "=", SqlLiteral(0))
        final_expr, kept_rows = SqlCase([SqlCaseWhen(none_kept, SqlLiteral(0))], final_expr), []
    return SqlSelect(
        ctes=chosen,
        select=[
            *[SqlField(SqlIdentifier(parts=[rows, alias]), alias) for alias in key_aliases],
            SqlField(final_expr, leaf_alias),
        ],
        from_table=SqlTableRef(name=rows),
        where=kept_rows,
        group_by=[SqlIdentifier(parts=[rows, alias]) for alias in key_aliases],
    )


def _filter_dimensions_are_source_local(
    measure_entity: str, items: Iterable[dict[str, Any]], config: PackageConfig
) -> bool:
    dimensions = _dimension_index(config)
    for item in items:
        if is_child_group(item):
            return False
        dim_id = str(item.get("field", "") or "")
        dim = dimensions.get(dim_id)
        if dim is None:
            return False
        if (
            dim.entity != measure_entity
            and _direct_dimension_source_expr(measure_entity, dim_id, config) is None
        ):
            return False
    return True


def _paths_have_temporal_validity(
    path_selections: Iterable[PathSelection], config: PackageConfig
) -> bool:
    relationships = _relationship_index(config)
    for selection in path_selections:
        for rel_id in selection.chosen_path:
            rel = relationships.get(rel_id)
            if rel is not None and rel.temporal_validity:
                return True
    return False


def _expression_reads_joined_entity(path_selections: Iterable[PathSelection]) -> bool:
    """Whether the measure's expression reads a joined entity, so it can't aggregate before
    the join (an authored cross-entity expression, or an aggregate_if's condition)."""
    return any(row.purpose in {"measure_expr", "aggregate_if"} for row in path_selections)


def _count_key_expr(table: str, columns: list[str]) -> Any:
    if len(columns) == 1:
        return _column_ref(table, columns[0])
    return SqlCall(
        "CONCAT_WS",
        [
            SqlLiteral("|"),
            *[SqlCast(_column_ref(table, column), "VARCHAR") for column in columns],
        ],
    )


def _entity_in_terms_of_leaf_select(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> SqlSelect | None:
    anchor_plan = _entity_in_terms_of_anchor_plan(plan, measure_plan, config)
    if anchor_plan is None:
        return None
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    anchor_entity = str(anchor_plan["anchor_entity"])
    anchor_table = entities[anchor_entity].table
    path_selections = list(anchor_plan["path_selections"])
    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    where_clauses: list[Any] = []
    for dim_id in plan.group_by:
        expr, alias = _direct_dimension_source_expr(
            anchor_entity, dim_id, config
        ) or _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[
            _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config)
        ]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(anchor_entity, role.dimension, config)
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(entities[dim.entity].table, dim.column)
        )
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        time_expr = _time_bucket_expr(time, raw_expr, config)
        time_alias = time_output_alias(time)
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)
        where_clauses.extend(_source_time_window(raw_expr, time, config, role_id=role.id))
    for item in refuse_child_groups(plan.query.get("where"), "in a child-grain count"):
        where_clauses.append(_filter_condition_for_item(anchor_entity, item, config))
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        where_clauses.append(_filter_condition_for_item(anchor_entity, item, config))
    where_clauses.extend(
        _edge_cap(
            _measure_index(config)[measure_plan.bound_measure.measure_id],
            _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config),
            config,
            entity=anchor_entity,
        )
    )
    select_fields.append(
        SqlField(
            _aggregation_expr(
                _count_key_expr(anchor_table, list(anchor_plan["anchor_key_columns"])),
                "count_distinct",
                dialect=dialect_for_warehouse(config.package.warehouse),
            ),
            measure_plan.bound_measure.alias,
        )
    )
    return SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=anchor_table),
        joins=_joins_for_paths(
            anchor_entity,
            path_selections,
            config,
            measure_entity=str(anchor_plan["omitted_root_entity"]),
            time_spec=plan.time,
        ),
        where=where_clauses,
        group_by=group_fields,
    )


def _fanout_dedup_leaf_select(
    measure_plan: MeasurePlan,
    config: PackageConfig,
    *,
    key_fields: list[SqlField],
    joins: list[SqlJoin],
    where: list[Any],
    value_expr: Any,
) -> SqlSelect:
    """Aggregate one row per (measure-entity key, output grain) across one-to-many hops.

    The joins repeat a measure row once per matching child row. The DISTINCT keeps one copy per
    output group: the row's key is in it and its value depends on that row alone, so only the
    join's copies go. Each row then counts once in every group it has a matching child in, and
    a leaf that only filters on the child is the semi-join of the qualifying rows.
    """
    entities = _entity_index(config)
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    entity = entities[measure.entity]
    table = entities[measure.entity].table
    rows_name = f"{measure_plan.cte_name}_entity_rows"
    # Each de-duplicated row carries its own row marker, which the group counts.
    markers = _row_markers(measure_plan, measure, value_expr)
    # A distinct count keeps each value once per group whatever row it came from, so a
    # row-grain model's rows (which the checks let through only for that) need no key.
    values_only = entity.is_row_grain and measure_plan.bound_measure.aggregation == "count_distinct"
    rows = SqlSelect(
        select=[
            *(
                SqlField(_column_ref(table, column), f"__entity_key_{index + 1}")
                for index, column in enumerate(
                    [] if values_only else entity.key or [entity.primary_key]
                )
            ),
            *key_fields,
            SqlField(value_expr, "__entity_value"),
            *(SqlField(marker, "__entity_rows") for marker, _ in markers),
        ],
        from_table=SqlTableRef(name=table),
        joins=joins,
        where=where,
        distinct=True,
    )
    group: list[Any] = [SqlIdentifier(parts=[rows_name, field.alias]) for field in key_fields]
    value = _aggregation_expr(
        SqlIdentifier(parts=[rows_name, "__entity_value"]),
        measure_plan.bound_measure.aggregation,
        parameters=measure_plan.bound_measure.aggregation_params,
        dialect=_dialect(config),
    )
    return SqlSelect(
        ctes=[SqlCte(name=rows_name, query=rows)],
        select=[
            *(SqlField(expr, field.alias) for expr, field in zip(group, key_fields, strict=True)),
            SqlField(value, measure_plan.bound_measure.alias),
            *(
                SqlField(
                    SqlCall("COUNT", [SqlIdentifier(parts=[rows_name, "__entity_rows"])]), alias
                )
                for _, alias in markers
            ),
        ],
        from_table=SqlTableRef(name=rows_name),
        group_by=group,
    )


def _parent_lookup_leaf_select(
    plan: LogicalPlan,
    measure_plan: MeasurePlan,
    config: PackageConfig,
    *,
    key_fields: list[SqlField],
    group_by: list[Any],
    joins: list[SqlJoin],
    where: list[Any],
    ctes: list[SqlCte],
) -> SqlSelect:
    """Carry a lookup's source total per ``via`` key onto the rows of its own entity.

    The source is compiled as its own query grouped by the via key, under the same bindings, so
    its filters, grants and empty-group settlement apply; the query's filters and time select
    the child rows only. A parent with no source rows reads 0 while the source is observed (the
    gate), a NULL key reads NULL, and the guard proves one total per output row, so MAX picks it.
    """
    bound = measure_plan.bound_measure
    # Every plan that reaches this leaf passes the one-total-per-row guard here as well.
    _validate_non_additive_sums([bound], config, normalize_query(plan.query))
    measure = _measure_index(config)[bound.measure_id]
    source_measure = _measure_index(config)[measure.lookup_from]
    child_link, _ = lookup_links(measure, config)
    via = _entity_index(config)[measure.lookup_via]
    via_dims = dict(
        zip(
            via.key or [via.primary_key],
            _entity_key_dimension_ids(via.id, config),
            strict=True,
        )
    )
    payload = {
        "version": 1,
        "select": [{"expression": {"measure": source_measure.id}, "as": "__lookup_value"}],
        "group_by": list(via_dims.values()),
    }
    # The inner query's settled values are internal: no NO_DATA_IN_SCOPE output of their own.
    with measure_objects(measure.id), recording_zero_outputs():
        record_bound_object(child_link, config)
        source_sql = nested.query_sql(config, payload, project_cut=project_is_cut())
    table = _entity_index(config)[measure.entity].table
    occupied = {table.casefold()} | {
        node.name.casefold()
        for node in sql_nodes([source_sql, ctes, joins])
        if isinstance(node, (SqlCte, SqlTableRef))
    }
    source = base = f"{measure_plan.cte_name}_lookup_source"
    index = 1
    # Reserve the source, gate and nested CTE names, including namespace prefixes.
    while any(source.casefold() in name for name in occupied):
        index += 1
        source = f"{base}_{index}"
    keys = list(
        zip(
            child_link.source_columns or [child_link.source_column],
            child_link.target_columns or [child_link.target_column],
            strict=True,
        )
    )
    on: Any = None
    for column, target in keys:
        match = SqlBinary(
            _column_ref(table, column), "=", SqlIdentifier(parts=[source, via_dims[target]])
        )
        on = match if on is None else SqlBinary(on, "AND", match)
    ctes = [*ctes, SqlCte(name=source, query=_namespace_sql_select(source_sql, f"{source}__"))]
    joins = [*joins, SqlJoin(join_type="LEFT", table=SqlTableRef(name=source), on=on)]
    value: Any = SqlIdentifier(parts=[source, "__lookup_value"])
    if resolves_to_zero("", source_measure):
        # The source settled each parent it holds. One it lacks (no source rows) reads 0 only
        # while the source holds a value in scope, the test its own guard applied; a NULL key
        # is no parent at all.
        require_settled_source(source_sql, {"lookup": measure.id})
        gate_cte, gate_join, gate_condition = absent_entities_gate(
            f"{source}_gate", source, "__lookup_value"
        )
        ctes.append(gate_cte)
        joins.append(gate_join)
        found = SqlIdentifier(parts=[source, via_dims[keys[0][1]]])
        absent: Any = gate_condition
        for column, _ in keys:
            absent = SqlBinary(
                SqlBinary(_column_ref(table, column), "IS NOT", SqlLiteral(None)), "AND", absent
            )
        value = SqlCase(
            [
                SqlCaseWhen(SqlBinary(found, "IS NOT", SqlLiteral(None)), value),
                SqlCaseWhen(absent, SqlLiteral(0)),
            ]
        )
    return SqlSelect(
        ctes=ctes,
        select=[*key_fields, SqlField(SqlCall("MAX", [value]), bound.alias)],
        from_table=SqlTableRef(name=table),
        joins=joins,
        where=where,
        group_by=group_by,
    )


def _join_table_name(join: SqlJoin) -> str:
    return join.table.name if isinstance(join.table, SqlTableRef) else ""


def _tables_read(condition: Any) -> set[str]:
    """The tables a join condition reads; an expression it doesn't know reads an unknown one."""
    if isinstance(condition, SqlIdentifier):
        return {".".join(condition.parts[:-1])}
    if isinstance(condition, SqlBinary):
        return _tables_read(condition.left) | _tables_read(condition.right)
    if isinstance(condition, SqlIsNull):
        return _tables_read(condition.expr)
    return set() if isinstance(condition, SqlLiteral) else {""}


def _fanout_filter_leaf_select(
    plan: LogicalPlan,
    measure_plan: MeasurePlan,
    config: PackageConfig,
    *,
    key_fields: list[SqlField],
    where: list[Any],
    child_where: list[Any],
    value_expr: Any,
) -> SqlSelect:
    """Filter the measure's rows with correlated EXISTS, never join copies into its sum.

    Each hop is nested so its authored join condition correlates with the preceding row. A
    lookup on the rows an EXISTS reads joins LEFT inside it instead, so a child row it finds
    no match for stays, with NULL for the lookup, as do the lookups outside EXISTS. The flat
    child filter takes one EXISTS; each child group takes its own (see
    ``_child_group_condition``).
    """
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    crossing = [
        row
        for row in measure_plan.path_selections
        if row.analysis.get("status") != "ok" and row.purpose != "child_group"
    ]
    child_joins = _joins_for_paths(
        measure.entity, crossing, config, measure_entity=measure.entity, time_spec=plan.time
    )
    conditions = _nested_exists(child_joins, child_where)
    query = normalize_query(plan.query)
    conditions += [
        _child_group_condition(plan, query, config, measure, group)
        for group in child_groups(query.where)
    ]
    joins = _joins_for_paths(
        measure.entity,
        [row for row in measure_plan.path_selections if row.analysis.get("status") == "ok"],
        config,
        measure_entity=measure.entity,
        time_spec=plan.time,
    )
    return SqlSelect(
        select=[
            *key_fields,
            SqlField(
                _aggregation_expr(
                    value_expr,
                    measure_plan.bound_measure.aggregation,
                    parameters=measure_plan.bound_measure.aggregation_params,
                    dialect=_dialect(config),
                ),
                measure_plan.bound_measure.alias,
            ),
            *_rows_fields(measure_plan, measure, value_expr),
        ],
        from_table=SqlTableRef(name=_entity_index(config)[measure.entity].table),
        joins=joins,
        where=[*where, *conditions],
        group_by=[field.expression for field in key_fields],
    )


def _nested_exists(child_joins: list[SqlJoin], conditions: list[Any]) -> list[Any]:
    """``conditions`` inside one EXISTS per hop of ``child_joins``, each correlated on its
    authored join. A LEFT lookup on the rows an EXISTS reads joins inside it. No joins leave
    ``conditions`` as they are."""
    levels: list[tuple[SqlJoin, SqlExpr, list[SqlJoin]]] = []
    for join in child_joins:
        if join.on is None:
            raise SemanticLayerError(
                "MIXED_GRAIN_INVALID", "A child filter needs a correlated join."
            )
        # A lookup from the measure's own row nests like any hop: the hop after it needs its
        # match anyway.
        holders = [
            lookups
            for level, _, lookups in reversed(levels)
            if join.join_type == "LEFT"
            and _tables_read(join.on) <= {_join_table_name(row) for row in (level, *lookups, join)}
        ]
        if holders:
            holders[0].append(join)
        else:
            levels.append((join, join.on, []))
    for join, on, lookups in reversed(levels):
        conditions = [
            SqlExists(
                SqlSelect(
                    select=[SqlField(SqlLiteral(1), "match")],
                    from_table=join.table,
                    joins=lookups,
                    where=[on, *conditions],
                )
            )
        ]
    return conditions


def _distinct_parent_group_conditions(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig, measure: Any
) -> list[Any]:
    """A child group's conditions in the de-duplicated parent leaf, the semi-join of a
    warehouse without correlated EXISTS.

    The leaf joins the group's route once, INNER, so its conditions hold on one child row
    together: ``match: any``. A second group, a ``none`` group, a lookup from the child (an
    INNER join drops what a LEFT join reads as NULL) or another child condition beside it
    would need more than that one join; the planner refuses them, and so does this, so no
    plan reaches the leaf with a group it would drop or misread.
    """
    query = normalize_query(plan.query)
    groups = child_groups(query.where)
    if not groups:
        return []
    group = groups[0]
    _, lookups = child_group_route(config, query, measure, group)
    crossing = [row for row in measure_plan.path_selections if row.analysis.get("status") != "ok"]
    if (
        _dialect(config).correlated_exists
        or len(groups) > 1
        or group.match != "any"
        or any(lookups)
        or [row.purpose for row in crossing] != ["child_group"]
    ):
        raise SemanticLayerError(
            "MIXED_GRAIN_INVALID",
            "This child group needs correlated EXISTS, which this leaf does not use.",
            details={
                "child": group.child,
                "match": group.match,
                "why_invalid": (
                    "The de-duplicated parent leaf answers one 'any' child group on the "
                    "child's own columns, as the leaf's only condition across a one-to-many hop."
                ),
            },
        )
    return _child_conditions(group, config)


def _child_conditions(group: ChildGroup, config: PackageConfig) -> list[Any]:
    """A child group's conditions on one child row, read from the child's own columns first."""
    return [
        dimension_filter(
            (
                _direct_dimension_source_expr(group.child, condition.field, config)
                or _resolve_dimension_expr(condition.field, config)
            )[0],
            condition.field,
            condition.op,
            condition.value,
            config,
        )
        for condition in group.where
    ]


def _child_group_condition(
    plan: LogicalPlan, query: Any, config: PackageConfig, measure: Any, group: ChildGroup
) -> Any:
    """EXISTS over every condition of the group on one child row, nested hop by hop along the
    route to the child; NOT EXISTS for ``match: none``. Lookups from the child are LEFT joins,
    so a missing row reads NULL, and NULL fails a comparison."""
    route, lookups = child_group_route(config, query, measure, group)
    found = [row for row in lookups if row is not None]
    inner_joins = _joins_for_paths(
        group.child, found, config, measure_entity=None, time_spec=plan.time
    )
    conditions = _child_conditions(group, config)
    hops = _joins_for_paths(
        measure.entity, [route], config, measure_entity=measure.entity, time_spec=plan.time
    )
    [exists] = _nested_exists([*hops, *inner_joins], conditions)
    return replace(exists, negated=group.match == "none")


def _source_rollup_leaf_select(
    plan: LogicalPlan,
    measure_plan: MeasurePlan,
    config: PackageConfig,
    *,
    where_clauses: list[Any],
    leaf_value_expr: Any,
    leaf_alias: str,
    time_alias: str,
    time_expr: Any | None,
    time_source_local: bool,
) -> SqlSelect | None:
    dimensions = _dimension_index(config)
    entities = _entity_index(config)
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    aggregation = str(measure_plan.bound_measure.aggregation or "").lower()
    if measure.measure_class == "semi_additive" or aggregation not in {"sum", "count"}:
        return None
    if not measure_plan.path_selections or _all_metric_predicates(plan, measure_plan):
        return None
    if _paths_are_single_hop_safe(measure_plan.path_selections, config):
        return None
    if _expression_reads_joined_entity(measure_plan.path_selections):
        return None
    if time_alias and not time_source_local:
        return None
    if _paths_have_temporal_validity(measure_plan.path_selections, config):
        return None
    if not _filter_dimensions_are_source_local(
        measure.entity, list(plan.query.get("where", []) or []), config
    ):
        return None
    if not _filter_dimensions_are_source_local(
        measure.entity, _bound_filter_clauses(measure_plan.bound_measure, config), config
    ):
        return None

    source_join_columns = _source_join_columns_for_paths(
        measure.entity, measure_plan.path_selections, config
    )
    if not source_join_columns:
        return None

    source_table = entities[measure.entity].table
    rollup_name = f"{measure_plan.cte_name}_source_rollup"
    preagg_fields: dict[str, Any] = {
        column: _column_ref(source_table, column) for column in source_join_columns
    }
    local_group_aliases: list[str] = []
    for dim_id in plan.group_by:
        dim = dimensions[dim_id]
        direct = _direct_dimension_source_expr(measure.entity, dim_id, config)
        if dim.entity == measure.entity:
            preagg_fields[dim_id] = unknown_as_null(_column_ref(source_table, dim.column), dim)
            local_group_aliases.append(dim_id)
        elif direct is not None:
            preagg_fields[dim_id] = direct[0]
            local_group_aliases.append(dim_id)
    if time_alias and time_expr is not None:
        preagg_fields[time_alias] = time_expr

    markers = _row_markers(measure_plan, measure, leaf_value_expr)
    taken = {alias.casefold() for alias in preagg_fields}
    taken.update(alias.casefold() for _, alias in markers)
    value_alias, suffix = "__source_value", 2
    while value_alias.casefold() in taken:
        value_alias, suffix = f"__source_value_{suffix}", suffix + 1

    preagg_select_fields = [SqlField(expr, alias) for alias, expr in preagg_fields.items()]
    preagg_select_fields.append(
        SqlField(
            _aggregation_expr(
                leaf_value_expr,
                aggregation,
                parameters=measure_plan.bound_measure.aggregation_params,
                dialect=dialect_for_warehouse(config.package.warehouse),
            ),
            value_alias,
        )
    )
    preagg_select_fields.extend(
        SqlField(SqlCall("COUNT", [marker]), alias) for marker, alias in markers
    )
    preagg_group_fields = list(preagg_fields.values())

    final_select_fields: list[SqlField] = []
    final_group_fields: list[Any] = []
    local_group_set = set(local_group_aliases)
    for dim_id in plan.group_by:
        if dim_id in local_group_set:
            expr = SqlIdentifier(parts=[rollup_name, dim_id])
            alias = dim_id
        else:
            expr, alias = _resolve_dimension_expr(dim_id, config)
        final_select_fields.append(SqlField(expr, alias))
        final_group_fields.append(expr)
    if time_alias:
        expr = SqlIdentifier(parts=[rollup_name, time_alias])
        final_select_fields.append(SqlField(expr, time_alias))
        final_group_fields.append(expr)
    final_select_fields.append(
        SqlField(SqlCall("SUM", [SqlIdentifier(parts=[rollup_name, value_alias])]), leaf_alias)
    )
    final_select_fields.extend(
        SqlField(SqlCall("SUM", [SqlIdentifier(parts=[rollup_name, alias])]), alias)
        for _, alias in markers
    )

    return SqlSelect(
        ctes=[
            SqlCte(
                name=rollup_name,
                query=SqlSelect(
                    select=preagg_select_fields,
                    from_table=SqlTableRef(name=source_table),
                    where=where_clauses,
                    group_by=preagg_group_fields,
                ),
            )
        ],
        select=final_select_fields,
        from_table=SqlTableRef(name=rollup_name),
        joins=_joins_for_paths(
            measure.entity,
            measure_plan.path_selections,
            config,
            measure_entity=measure.entity,
            time_spec=plan.time,
            table_overrides={measure.entity: rollup_name},
        ),
        group_by=final_group_fields,
    )


def _measure_leaf_select(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> SqlSelect:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    measures = _measure_index(config)
    temporal_roles = _temporal_role_index(config)
    query = plan.query
    measure = measures[measure_plan.bound_measure.measure_id]
    selected_aggregate = _selected_aggregate_relation(measure_plan, config)
    if selected_aggregate is not None:
        return _aggregate_relation_leaf_select(plan, [measure_plan], selected_aggregate, config)
    if measure_plan.rewrite_strategy == PARENT_VALUE:
        return parent_value_leaf_select(plan, measure_plan, config)
    # A grouping by a child's values after a lookup joins those values, distinct per parent.
    child, child_cte = (
        child_values_cte(measure_plan, plan.group_by, config)
        if measure_plan.rewrite_strategy == CHILD_VALUES
        else ("", None)
    )

    predicate_ctes: list[SqlCte] = []
    predicate_joins: list[SqlJoin] = []
    predicate_row_conditions: list[Any] = []
    owned_joins: list[SqlJoin] = []
    owned_conditions: list[Any] = []
    owned_predicates = _bound_metric_predicates(measure_plan.bound_measure)
    query_predicates = _query_metric_predicates(plan)
    for index, predicate in enumerate(_all_metric_predicates(plan, measure_plan)):
        with (
            leaf_predicate_roles(measure_plan.bound_measure.alias)
            if predicate in owned_predicates
            else nullcontext(),
            # Query metric_filters predicates filter every leaf: whole-query cuts.
            cut_owners() if predicate in query_predicates else nullcontext(),
        ):
            ctes, joins, row_conditions = _predicate_ctes_and_join(
                predicate, index=index, plan=plan, measure_plan=measure_plan, config=config
            )
        predicate_ctes.extend(ctes)
        predicate_joins.extend(joins)
        predicate_row_conditions.extend(row_conditions)
        if predicate in owned_predicates:
            owned_joins.extend(joins)
            owned_conditions.extend(row_conditions)
    if child_cte is not None:
        predicate_ctes.append(child_cte)
    owned_reads = _owned_predicate_reads.get()
    # A contextual predicate's set reads the query's own filters: no probe sees past them.
    if (
        owned_reads is not None
        and owned_predicates
        and settles_as_a_call()
        and all(predicate.scope_mode != "contextual" for predicate in owned_predicates)
    ):
        owned_reads[measure_plan.bound_measure.alias] = LeafScope(
            from_table=SqlTableRef(name=entities[measure.entity].table),
            joins=tuple(owned_joins),
            where=tuple(owned_conditions),
            value=None,
        )

    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    time_alias = ""
    time_expr: Any | None = None
    time_source_local = True
    leaf_time_role = _leaf_time_role(
        measure_plan.bound_measure, normalize_query(plan.query), config
    )
    for dim_id in plan.group_by:
        child_expr = (
            child_value_dimension(dim_id, child, child_cte.name, config) if child_cte else None
        )
        expr, alias = (
            (child_expr, dim_id)
            if child_expr is not None
            else _direct_dimension_source_expr(measure.entity, dim_id, config)
            or _resolve_dimension_expr(dim_id, config)
        )
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(
            measure.entity,
            role.dimension,
            config,
        )
        time_source_local = dim.entity == measure.entity or direct_time is not None
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(entities[dim.entity].table, dim.column)
        )
        coverage_time = raw_expr
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        time_expr = _time_bucket_expr(time, raw_expr, config)
        time_alias = time_output_alias(time)
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    child_where: list[Any] = []
    authored_conditions: list[Any] = []
    preserve_buckets = _preserves_filtered_series(plan, measure_plan, config)
    crossing_filters = {
        (row.target_entity, row.purpose)
        for row in measure_plan.path_selections
        if row.analysis.get("status") != "ok"
    }
    # Without correlated EXISTS (ClickHouse 24.8), the DISTINCT-parent leaf implements the same
    # semi-join.
    semijoin = (
        _dialect(config).correlated_exists
        and measure_plan.rewrite_strategy == "fanout_dedup"
        and all(
            purpose in {"where", "metric_filter", "child_group"} for _, purpose in crossing_filters
        )
    )
    # Child groups lower in the semi-join leaf, each to its own EXISTS (_fanout_filter_leaf_select),
    # or as the one child of ClickHouse's de-duplicated leaf below. Any other leaf refuses them.
    filters = (
        plain_filters(query.get("where"))
        if measure_plan.rewrite_strategy == "fanout_dedup"
        else refuse_child_groups(query.get("where"), "in this measure's leaf")
    )
    # The conditions a stock applies to the snapshot it chooses, not before choosing it: only
    # its own leaf below takes them out of `where_clauses`.
    snapshot_filters: list[Any] = []
    stock_role = measure_plan.bound_measure.temporal_role
    for item in filters:
        target = (
            child_where
            if semijoin and (dimensions[item["field"]].entity, "where") in crossing_filters
            else where_clauses
        )
        target.append(condition := _filter_condition_for_item(measure.entity, item, config))
        if _reads_chosen_snapshot(measure, stock_role, str(item["field"]), config):
            snapshot_filters.append(condition)
    # The authored filter's conditions, wherever they apply: a declared edge's probe leaves
    # them out (_record_time_scope).
    filter_conditions: list[Any] = []
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        target = (
            child_where
            if semijoin and (dimensions[item["field"]].entity, "metric_filter") in crossing_filters
            else authored_conditions
            if preserve_buckets
            else where_clauses
        )
        target.append(condition := _filter_condition_for_item(measure.entity, item, config))
        filter_conditions.append(condition)
        if _reads_chosen_snapshot(measure, stock_role, str(item["field"]), config):
            snapshot_filters.append(condition)
    where_clauses.extend(_edge_cap(measure, leaf_time_role, config))
    untimed = [*where_clauses, *authored_conditions, *predicate_row_conditions]
    if plan.time:
        where_clauses.extend(
            _source_time_window(raw_expr, plan.time, config, role_id=leaf_time_role)
        )
    where_clauses.extend(predicate_row_conditions)

    order_expr = None
    if measure_plan.bound_measure.temporal_role:
        role = temporal_roles[measure_plan.bound_measure.temporal_role]
        time_dim = dimensions[role.dimension]
        order_expr = _column_ref(entities[time_dim.entity].table, time_dim.column)

    def path_joins() -> list[SqlJoin]:
        """The leaf's joins along its paths; built in the branch that uses them, since building
        records the routes they take."""
        joins = _joins_for_paths(
            measure.entity,
            measure_plan.path_selections,
            config,
            measure_entity=measure.entity,
            time_spec=plan.time,
            table_overrides={child: child_cte.name} if child_cte else None,
        )
        return keep_rows(joins, child_cte.name) if child_cte else joins

    def record_scope(leaf: SqlSelect) -> None:
        """The scope of the rows ``leaf`` reads, for the empty-group guard."""
        if plan.time:
            _record_time_scope(
                plan,
                config,
                leaf,
                [measure_plan],
                untimed,
                raw_time=coverage_time,
                bucket=time_expr,
                local=time_source_local,
                authored=filter_conditions,
            )
        else:
            _record_all_time_scope(
                config, leaf, [measure_plan], untimed, authored=filter_conditions
            )

    leaf_alias = measure_plan.bound_measure.alias
    if measure_plan.rewrite_strategy == "parent_lookup":
        return _parent_lookup_leaf_select(
            plan,
            measure_plan,
            config,
            key_fields=select_fields,
            group_by=group_fields,
            joins=[
                *path_joins(),
                *predicate_joins,
            ],
            where=where_clauses,
            ctes=predicate_ctes,
        )
    leaf_value_expr = _config_expr_to_sql(measure.expr, measure, config)
    leaf_value_expr = _filtered_series_operand(leaf_value_expr, authored_conditions)
    if semijoin:
        return _fanout_filter_leaf_select(
            plan,
            measure_plan,
            config,
            key_fields=select_fields,
            where=where_clauses,
            child_where=child_where,
            value_expr=leaf_value_expr,
        )
    if measure_plan.rewrite_strategy == "fanout_dedup":
        where_clauses += _distinct_parent_group_conditions(plan, measure_plan, config, measure)
        joins = path_joins()
        return _fanout_dedup_leaf_select(
            measure_plan,
            config,
            key_fields=select_fields,
            joins=joins,
            where=where_clauses,
            value_expr=leaf_value_expr,
        )
    entity_in_terms_of = (
        None if child_cte else _entity_in_terms_of_leaf_select(plan, measure_plan, config)
    )
    if entity_in_terms_of is not None:
        return entity_in_terms_of
    source_rollup = (
        None
        if child_cte
        else _source_rollup_leaf_select(
            plan,
            measure_plan,
            config,
            where_clauses=where_clauses,
            leaf_value_expr=leaf_value_expr,
            leaf_alias=leaf_alias,
            time_alias=time_alias,
            time_expr=time_expr,
            time_source_local=time_source_local,
        )
    )
    if source_rollup is not None:
        # It adds up the measure's own rows before the hops: those rows, as the plain leaf
        # reads them, are its scope, so its empty groups settle as a one-hop grouping's do. The
        # rollup's own joins note the route.
        with recording_route_choices():
            scope_joins = path_joins()
        value = _aggregation_expr(
            leaf_value_expr,
            measure_plan.bound_measure.aggregation,
            order_expr=order_expr,
            parameters=measure_plan.bound_measure.aggregation_params,
            dialect=dialect_for_warehouse(config.package.warehouse),
        )
        record_scope(
            SqlSelect(
                select=[*select_fields, SqlField(value, leaf_alias)],
                from_table=SqlTableRef(name=entities[measure.entity].table),
                joins=scope_joins,
                where=where_clauses,
                group_by=group_fields,
            )
        )
        return source_rollup
    joins = [
        *path_joins(),
        *predicate_joins,
    ]
    if measure.measure_class == "semi_additive":
        if order_expr is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE",
                f"{measure_plan.bound_measure.aggregation} requires an ordering temporal role",
            )
        row_grain_exprs = [
            _column_ref(entities[measure.entity].table, column)
            for column in _snapshot_series_columns(
                measure, measure_plan.bound_measure.temporal_role, config
            )
        ]
        window_choice = measure_plan.bound_measure.aggregation
        if window_choice not in {"first_value", "last_value"}:
            window_choice = (
                "first_value"
                if measure.accumulation.snapshot == "start_of_period"
                else "last_value"
            )
        # The query's time bucket, the stock's own clock and calendar dimensions split a
        # series into periods; any other grouped dimension is an attribute of the snapshot.
        period_aliases = [
            field.alias
            for field in select_fields
            if field.alias == time_alias
            or (field.alias in dimensions and _stock_period_split(stock_role, field.alias, config))
        ]
        for dim_id in plan.group_by:
            if not _stock_period_split(stock_role, dim_id, config) and dimensions[
                dim_id
            ].data_type in {
                "date",
                "timestamp",
            }:
                raise SemanticLayerError(
                    "REWRITE_NOT_SUPPORTED",
                    "Grouping a stock by a date or timestamp attribute is ambiguous. "
                    "Group by the stock's clock or a calendar dimension instead.",
                    details={
                        "reason": "stock_grouped_by_date_attribute",
                        "dimension": dim_id,
                    },
                )
        if plan.time and time_source_local:
            _record_balance_edge(
                plan,
                config,
                measure_plan,
                entities[measure.entity].table,
                coverage_time,
                time_expr,
            )
        return _semi_additive_leaf_select(
            key_fields=list(select_fields),
            period_aliases=period_aliases,
            row_grain_exprs=row_grain_exprs,
            order_expr=order_expr,
            value_expr=leaf_value_expr,
            leaf_alias=leaf_alias,
            from_table=SqlTableRef(name=entities[measure.entity].table),
            joins=joins,
            where_clauses=[
                clause
                for clause in where_clauses
                if not any(clause is condition for condition in snapshot_filters)
            ],
            snapshot_filters=snapshot_filters,
            # A stock that adds up its series; judged inside the query's filters
            # (observation_scope: query), a period with no snapshot passing them has no data.
            zero_unmatched=measure.additive
            and measure_plan.bound_measure.aggregation in {"last_value", "first_value", "sum"}
            and observation_scope(query, config) == "dataset",
            window_choice=window_choice,
            final_aggregation=measure_plan.bound_measure.aggregation,
            aggregation_params=dict(measure_plan.bound_measure.aggregation_params),
            warehouse=config.package.warehouse,
            ctes=predicate_ctes,
            day=closing_day(
                measure, temporal_roles[measure_plan.bound_measure.temporal_role], config
            ),
            measure_id=measure.id,
        )

    select_fields.append(
        SqlField(
            _aggregation_expr(
                leaf_value_expr,
                measure_plan.bound_measure.aggregation,
                order_expr=order_expr,
                parameters=measure_plan.bound_measure.aggregation_params,
                dialect=dialect_for_warehouse(config.package.warehouse),
            ),
            leaf_alias,
        )
    )
    select_fields.extend(_rows_fields(measure_plan, measure, leaf_value_expr))
    leaf = SqlSelect(
        ctes=predicate_ctes,
        select=select_fields,
        from_table=SqlTableRef(name=entities[measure.entity].table),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )
    record_scope(leaf)
    return leaf


def _distinct_value_path_selections(
    plan: LogicalPlan, config: PackageConfig
) -> list[PathSelection]:
    # The time role's path joins INNER, as in every leaf, even where a grouping shares it.
    time_entity = (
        _dimension_index(config)[
            _temporal_role_index(config)[str(plan.time["temporal_role"])].dimension
        ].entity
        if plan.time
        else None
    )
    # A listing's one-to-many hops keep their rows, as its lookups do (``_joins_for_paths``).
    purpose = "listing" if is_listing_plan(plan) else "distinct_values"
    return [
        PathSelection(
            target_entity=target_entity,
            purpose="time" if target_entity == time_entity else purpose,
            chosen_path=list(path),
            candidate_paths=[
                list(candidate)
                for candidate in list(plan.candidate_paths.get(target_entity, []) or [])
            ],
            analysis={},
        )
        for target_entity, path in sorted(dict(plan.selected_paths or {}).items())
    ]


def _distinct_value_select(plan: LogicalPlan, config: PackageConfig) -> SqlSelect:
    if _query_metric_predicates(plan):
        raise SemanticLayerError(
            "PREDICATE_NOT_SUPPORTED",
            "An entity filter on an aggregate needs a measure or conversion leaf; "
            "add a select that reads a measure, or remove the entity filter.",
            details={"path": "where", "reason": "distinct_values_without_leaf"},
        )
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    query = normalize_query(plan.query)

    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    for dim_id in plan.group_by:
        expr, alias = _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)

    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[str(time["temporal_role"])]
        dim = dimensions[role.dimension]
        raw_expr = _column_ref(entities[dim.entity].table, dim.column)
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        time_expr = _time_bucket_expr(time, raw_expr, config)
        time_alias = time_output_alias(time)
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    for item in refuse_child_groups(query.where, "in a query without a measure"):
        expr, _ = _resolve_dimension_expr(str(item.field), config)
        where_clauses.append(dimension_filter(expr, str(item.field), item.op, item.value, config))

    if plan.time:
        where_clauses.extend(_source_time_window(raw_expr, time, config))

    joins = list(
        _joins_for_paths(
            plan.root_entity,
            _distinct_value_path_selections(plan, config),
            config,
            measure_entity=None,
            time_spec=plan.time,
        )
    )
    select = SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=entities[plan.root_entity].table),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )
    return listing_rows(select, plan, config) if is_listing_plan(plan) else select


def _source_rollup_strategy(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> dict[str, Any]:
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    aggregation = str(measure_plan.bound_measure.aggregation or "").lower()
    if plan.time:
        dimensions = _dimension_index(config)
        temporal_roles = _temporal_role_index(config)
        leaf_time_role = _leaf_time_role(
            measure_plan.bound_measure, normalize_query(plan.query), config
        )
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(measure.entity, role.dimension, config)
        if dim.entity != measure.entity and direct_time is None:
            return {"strategy": "direct_aggregate", "reason": "non_source_local_time_axis"}
    if measure.measure_class == "semi_additive":
        return {"strategy": "snapshot_select", "reason": "semi_additive_measure"}
    if aggregation not in {"sum", "count"}:
        return {"strategy": "direct_aggregate", "reason": "aggregation_not_source_rollup_safe"}
    if not measure_plan.path_selections:
        return {"strategy": "direct_aggregate", "reason": "no_dimension_join"}
    if _all_metric_predicates(plan, measure_plan):
        return {"strategy": "direct_aggregate", "reason": "metric_predicate_scope"}
    if _paths_are_single_hop_safe(measure_plan.path_selections, config):
        return {"strategy": "direct_aggregate", "reason": "single_hop_safe_join"}
    if _expression_reads_joined_entity(measure_plan.path_selections):
        return {"strategy": "direct_aggregate", "reason": "measure_expression_reads_joined_entity"}
    if _paths_have_temporal_validity(measure_plan.path_selections, config):
        return {"strategy": "direct_aggregate", "reason": "temporal_validity_path"}
    if not _filter_dimensions_are_source_local(
        measure.entity, list(plan.query.get("where", []) or []), config
    ):
        return {"strategy": "direct_aggregate", "reason": "non_source_local_query_filter"}
    if not _filter_dimensions_are_source_local(
        measure.entity, _bound_filter_clauses(measure_plan.bound_measure, config), config
    ):
        return {"strategy": "direct_aggregate", "reason": "non_source_local_measure_filter"}
    return {"strategy": "source_preaggregate", "reason": "multi_hop_or_fanout_join"}


def _dimension_is_local_or_direct(source_entity: str, dim_id: str, config: PackageConfig) -> bool:
    dim = _dimension_index(config)[dim_id]
    return (
        dim.entity == source_entity
        or _direct_dimension_source_expr(source_entity, dim_id, config) is not None
    )


def _filters_pushable_to_entity_scan(
    source_entity: str, plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> bool:
    if plan.time:
        role = _temporal_role_index(config)[
            _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config)
        ]
        if not _dimension_is_local_or_direct(source_entity, role.dimension, config):
            return False
    for item in list(plan.query.get("where", []) or []):
        if is_child_group(item) or not _dimension_is_local_or_direct(
            source_entity, str(item.get("field", "")), config
        ):
            return False
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        if not _dimension_is_local_or_direct(source_entity, str(item.get("field", "")), config):
            return False
    return True


def _measure_group_leaf_select(
    plan: LogicalPlan, measure_plans: list[MeasurePlan], config: PackageConfig
) -> SqlSelect:
    if len(measure_plans) == 1:
        return _measure_leaf_select(plan, measure_plans[0], config)
    selected_aggregate_ids = {
        str(getattr(measure_plan, "aggregate_relation_id", "") or "")
        for measure_plan in measure_plans
    }
    if len(selected_aggregate_ids) == 1:
        aggregate_id = next(iter(selected_aggregate_ids))
        if aggregate_id:
            aggregate = _aggregate_relation_index(config).get(aggregate_id)
            if aggregate is not None:
                return _aggregate_relation_leaf_select(plan, measure_plans, aggregate, config)

    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    measures = _measure_index(config)
    temporal_roles = _temporal_role_index(config)
    query = plan.query
    first_plan = measure_plans[0]
    first_measure = measures[first_plan.bound_measure.measure_id]
    leaf_time_role = _leaf_time_role(first_plan.bound_measure, normalize_query(plan.query), config)

    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    for dim_id in plan.group_by:
        expr, alias = _direct_dimension_source_expr(
            first_measure.entity, dim_id, config
        ) or _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(first_measure.entity, role.dimension, config)
        time_source_local = dim.entity == first_measure.entity or direct_time is not None
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(entities[dim.entity].table, dim.column)
        )
        coverage_time = raw_expr
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        time_expr = _time_bucket_expr(time, raw_expr, config)
        time_alias = time_output_alias(time)
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    authored_conditions: list[Any] = []
    preserve_buckets = all(_preserves_filtered_series(plan, row, config) for row in measure_plans)
    for item in refuse_child_groups(query.get("where"), "in a folded leaf"):
        where_clauses.append(_filter_condition_for_item(first_measure.entity, item, config))
    filter_conditions: list[Any] = []
    for item in _bound_filter_clauses(first_plan.bound_measure, config):
        condition = _filter_condition_for_item(first_measure.entity, item, config)
        (authored_conditions if preserve_buckets else where_clauses).append(condition)
        filter_conditions.append(condition)
    where_clauses.extend(_edge_cap(first_measure, leaf_time_role, config))
    untimed = [*where_clauses, *authored_conditions]
    if plan.time:
        where_clauses.extend(
            _source_time_window(raw_expr, plan.time, config, role_id=leaf_time_role)
        )

    for measure_plan in measure_plans:
        measure = measures[measure_plan.bound_measure.measure_id]
        order_expr = None
        if measure_plan.bound_measure.temporal_role:
            role = temporal_roles[measure_plan.bound_measure.temporal_role]
            time_dim = dimensions[role.dimension]
            order_expr = _column_ref(entities[time_dim.entity].table, time_dim.column)
        value_expr = _config_expr_to_sql(measure.expr, measure, config)
        value_expr = _filtered_series_operand(value_expr, authored_conditions)
        select_fields.append(
            SqlField(
                _aggregation_expr(
                    value_expr,
                    measure_plan.bound_measure.aggregation,
                    order_expr=order_expr,
                    parameters=measure_plan.bound_measure.aggregation_params,
                    dialect=_dialect(config),
                ),
                measure_plan.bound_measure.alias,
            )
        )
        select_fields.extend(_rows_fields(measure_plan, measure, value_expr))

    joins = list(
        _joins_for_paths(
            first_measure.entity,
            first_plan.path_selections,
            config,
            measure_entity=first_measure.entity,
            time_spec=plan.time,
        )
    )
    leaf = SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=entities[first_measure.entity].table),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )
    if plan.time:
        _record_time_scope(
            plan,
            config,
            leaf,
            measure_plans,
            untimed,
            raw_time=coverage_time,
            bucket=time_expr,
            local=time_source_local,
            authored=filter_conditions,
        )
    else:
        _record_all_time_scope(config, leaf, measure_plans, untimed, authored=filter_conditions)
    return leaf
