"""SQL lowering: a logical plan becomes one SQL AST.

This module assembles the query from the modules beside it (measure leaves, predicate sets,
distributions, the time series and its coverage) and re-exports the names other modules import
from it. A nested compile goes through :mod:`.nested`.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import Any, cast

from ..ast import normalize_query
from ..errors import SemanticLayerError
from ..expressions import (
    BooleanExpr,
    ComparisonExpr,
    MetricPredicateExpr,
    ShareExpr,
)
from ..ir import (
    LogicalPlan,
)
from ..schema import PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
    build_filter_condition,
)
from .bind import (
    _bound_metric_predicates,
    _expression_alias,
    _parse_public_expr,
    earlier_settlement,
)
from .conversion import _conversion_leaf_cte
from .dependencies import (
    binding_cut,
    cut_owners,
    plan_is_root,
    project_is_cut,
    record_retained_filtered_series,
)
from .distribution import _lower_agent_dag_to_sql
from .empty_groups import (
    GUARDED_BASE,
    LeafScope,
    base_reads,
    carried_zero_lookups,
    expr_resolves_to_zero,
    guard_empty_groups,
    has_nested_case,
    observes_dataset,
    record_balance_output,
    record_zero_output,
    recording_leaf_scopes,
    refuse_declared_edges,
    refuse_shared_names,
    refuse_unsettled,
    require_time_scopes,
    zero_aliases,
)
from .indexes import (
    _measure_index,
)
from .leaves import _distinct_value_select, _measure_group_leaf_select
from .lowering_common import _apply_role_timezone as _apply_role_timezone
from .lowering_common import (
    _dataset_scope,
    _dialect,
    _emits_time_coverage,
    _entity_in_terms_of_anchor_plan,
    _measure_plan_groups,
    _metric_filter_alias,
    _needs_time_scope,
    _plan_requires_agent_dag_lowering,
    _query_key_aliases,
    _query_metric_predicates,
    _query_requires_dense_series,
    _recording_owned_predicate_reads,
    _refuse_varying_time_key,
    _require_filtered_series_operand,
    _retained_filtered_series,
    _time_alias_for_plan,
)
from .lowering_common import _filtered_series_candidate as _filtered_series_candidate
from .lowering_common import _snapshot_series_columns as _snapshot_series_columns
from .lowering_common import _stock_clock_key_gap as _stock_clock_key_gap
from .lowering_common import _stock_snapshot_refusal as _stock_snapshot_refusal
from .lowering_common import _value_filter_condition as _value_filter_condition
from .paths import (
    _join_condition,
    _leaf_time_role,
)
from .paths import _joins_for_paths as _joins_for_paths
from .paths import _resolve_dimension_expr as _resolve_dimension_expr
from .post_aggregation import (
    _compile_post_expr,
    _namespace_sql_select,
    share_partition,
)
from .predicate_sets import _anchored_entity_set_select, _requested_order
from .row_counts import (
    _names_in_use,
    _row_count_names,
    naming_row_counts,
    override_row_counts,
    share_group_counts,
)
from .temporal import (
    refuse_authored_calendar,
    window_total_refusal,
)
from .time_series import (
    _calendar_fill_binding,
    _conversion_exprs_for_plan,
    _series_base_ctes,
    _tier_internal_aliases,
)


def _namespaced_scope(scope: LeafScope, leaf: SqlSelect, prefix: str) -> LeafScope:
    """A nested leaf's scope reading its CTEs by the names they take inside the leaf's CTE."""
    named = _namespace_sql_select(
        SqlSelect(
            select=[],
            from_table=scope.from_table,
            joins=list(scope.joins),
            where=list(scope.where),
            ctes=list(leaf.ctes),
        ),
        prefix,
    )
    return replace(scope, joins=tuple(named.joins), where=tuple(named.where))


def lower_to_sql(
    plan: LogicalPlan,
    config: PackageConfig,
    *,
    guard_empty: bool = True,
    default_order: bool = True,
) -> SqlSelect:
    if plan.time:
        # Validation resolves the calendar; a plan that bypassed it refuses the same way.
        refuse_authored_calendar(
            plan.time.get("grain"),
            [str(plan.time.get("temporal_role") or "")]
            + [row.bound_measure.temporal_role for row in plan.measure_plans],
            config,
        )
    # Row-based rewrites do not choose a snapshot per series before filtering. Keep the
    # planner/loader's stock refusal even when a caller supplies a plan that bypasses them.
    measures = _measure_index(config)
    for row in plan.measure_plans:
        if row.rewrite_strategy not in {"fanout_dedup", "parent_lookup"}:
            continue
        measure = measures[row.bound_measure.measure_id]
        source = (
            measures[measure.lookup_from] if row.rewrite_strategy == "parent_lookup" else measure
        )
        if source.measure_class != "semi_additive":
            continue
        details = {
            "reason": "stock_requires_snapshot_selection",
            "measure": source.id,
            "rewrite_strategy": row.rewrite_strategy,
        }
        if row.rewrite_strategy == "parent_lookup":
            details["lookup"] = measure.id
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "A stock requires snapshot selection before attribute filters; "
            "row-based fan-out and parent-lookup rewrites cannot preserve that reading.",
            details=details,
        )
    if _emits_time_coverage(plan, config) and any(
        row.aggregate_relation_id for row in plan.measure_plans
    ):
        raise SemanticLayerError(
            "EMPTY_GROUPS_UNSETTLED", "A coverage-dependent plan must read the base relation."
        )
    select = _lower_query_to_sql(plan, config, guard_empty)
    _refuse_per_period_total(plan)
    if plan.query.get("order_by") and not all(term.nulls_last for term in select.order_by):
        # Requested terms come only from _requested_order; a lowering that built its own
        # would leave NULL placement to the backend's default.
        raise SemanticLayerError(
            "INVALID_ORDER_BY", "Every requested order_by term must sort NULLs last."
        )
    if plan.time.get("window_total"):
        # One total over the window: the constant time key isn't a column.
        time_alias = _time_alias_for_plan(plan)
        return replace(select, select=[item for item in select.select if item.alias != time_alias])
    if default_order and plan.time and not plan.query.get("order_by"):
        # Every request's final projection orders by time, then the authored groups.
        # Nested query sources opt out at their shared compilation boundary.
        # Use output aliases after internal aliasing so this also covers dense and combined SQL.
        order_keys = [_time_alias_for_plan(plan), *plan.group_by]
        output_aliases = {item.alias for item in select.select}
        if any(key not in output_aliases for key in order_keys):
            raise SemanticLayerError(
                "INVALID_ORDER_BY", "Default time ordering requires projected time and group keys"
            )
        select = replace(
            select,
            order_by=[SqlOrder(SqlIdentifier(parts=[key]), "ASC") for key in order_keys],
        )
    return select


def _refuse_per_period_total(plan: LogicalPlan) -> None:
    """A request's window with no grain is one total, never a row per raw timestamp.

    Planning settles every other request as a total or refuses it; a predicate tested per
    output period is refused here, once lowering has raised any refusal of its own.
    """
    time = plan.time
    if (
        not plan_is_root()
        or not time
        or time.get("grain")
        or time.get("window_total")
        or (time.get("start") is None and time.get("end") is None)
    ):
        return
    predicates = [
        *(p for row in plan.measure_plans for p in _bound_metric_predicates(row.bound_measure)),
        *_query_metric_predicates(plan),
    ]
    per_period = any(predicate.scope_mode != "entity_only" for predicate in predicates)
    raise window_total_refusal(
        time,
        "an entity filter in it is tested per period"
        if per_period
        else "it would group by the raw time",
    )


def _lower_query_to_sql(plan: LogicalPlan, config: PackageConfig, guard_empty: bool) -> SqlSelect:
    anchored_select = _anchored_entity_set_select(plan, config)
    if anchored_select is not None:
        return _tier_internal_aliases(plan, anchored_select, measure_aliases=[])
    if _plan_requires_agent_dag_lowering(plan, config):
        # Arithmetic beside a distribution settles each branch, then their combine, before
        # it can tell an operand's unknown amounts from its empty groups: every branch keeps
        # the earlier settlement.
        with earlier_settlement():
            return _lower_agent_dag_to_sql(plan, config, guard_empty)

    key_aliases = _query_key_aliases(plan)
    measure_aliases: list[str] = []
    # Each sum's count of the rows it read, by measure alias: only those its leaf emitted, so
    # the guard refuses a sum whose leaf can't tell no rows from rows of unknown values.
    rows: dict[str, str] = {}
    taken = _names_in_use(plan, config)
    row_counts = _row_count_names(plan, taken)
    refuse_shared_names(row_counts.values(), taken)
    overrides = override_row_counts(plan, config) if plan_is_root() else {}
    conversion_exprs = _conversion_exprs_for_plan(plan, config)
    # Under the dataset scope, each measure's own rows, which the query's where filters and
    # entity filters never cut. A where beside an entity filter is not probed, so it refuses.
    observed: dict[str, LeafScope] = {}
    probing = (
        guard_empty
        and observes_dataset()
        and not (_query_metric_predicates(plan) and plan.query.get("where"))
    )
    retained = _retained_filtered_series(plan, config)
    if plan.measure_plans or conversion_exprs:
        leaf_ctes: list[SqlCte] = []
        measure_groups = _measure_plan_groups(plan, config)
        # A window on the request's own query hides data outside it: keep what each leaf reads.
        with (
            recording_leaf_scopes() as scopes,
            naming_row_counts(row_counts, overrides),
            _recording_owned_predicate_reads() as owned_reads,
        ):
            for measure_group in measure_groups:
                cte_name = measure_group[0].cte_name
                # A folded group shares one scan, so its filters cut every leaf in it.
                with cut_owners(*(row.bound_measure.alias for row in measure_group)):
                    leaf_select = _measure_group_leaf_select(plan, measure_group, config)
                    for row in measure_group:
                        if row.bound_measure.alias in retained:
                            _require_filtered_series_operand(leaf_select, row, config)
                            record_retained_filtered_series(row.bound_measure.alias)
                    for row in measure_group if probing else ():
                        owned = owned_reads.get(row.bound_measure.alias)
                        if owned is not None:
                            owned = _namespaced_scope(owned, leaf_select, f"{cte_name}__")
                        role = _leaf_time_role(
                            row.bound_measure, normalize_query(plan.query), config
                        )
                        if (scope := _dataset_scope(row, config, owned, role)) is not None:
                            observed[row.bound_measure.alias] = scope
                emitted = {field.alias for field in leaf_select.select}
                rows.update(
                    (row.bound_measure.alias, row_counts[row.bound_measure.alias])
                    for row in measure_group
                    if row_counts[row.bound_measure.alias] in emitted
                )
                if (
                    guard_empty
                    and plan_is_root()
                    and _needs_time_scope(plan, config)
                    and not leaf_select.ctes
                    and isinstance(leaf_select.from_table, SqlTableRef)
                    and not any(row.aggregate_relation_id for row in measure_group)
                    and not any(row.rewrite_strategy == "fanout_dedup" for row in measure_group)
                    and _entity_in_terms_of_anchor_plan(plan, measure_group[0], config) is None
                ):
                    require_time_scopes((row.bound_measure.alias for row in measure_group), scopes)
                _refuse_varying_time_key(plan, leaf_select)
                for row in measure_group if leaf_select.ctes else ():
                    if (scope := scopes.get(row.bound_measure.alias)) is not None:
                        scopes[row.bound_measure.alias] = _namespaced_scope(
                            scope, leaf_select, f"{cte_name}__"
                        )
                leaf_ctes.append(
                    SqlCte(name=cte_name, query=_namespace_sql_select(leaf_select, f"{cte_name}__"))
                )

        conversion_aliases: list[str] = []
        for index, expr in enumerate(conversion_exprs):
            with cut_owners(_expression_alias(expr, config)):
                conversion_cte = _conversion_leaf_cte(expr, index=index, plan=plan, config=config)
            _refuse_varying_time_key(plan, conversion_cte.query)
            leaf_ctes.append(
                SqlCte(
                    name=conversion_cte.name,
                    query=_namespace_sql_select(
                        cast(SqlSelect, conversion_cte.query), f"{conversion_cte.name}__"
                    ),
                )
            )
            conversion_aliases.append(_expression_alias(expr, config))

        measure_aliases = [
            row.bound_measure.alias for row in plan.measure_plans
        ] + conversion_aliases

        combine_ctes: list[SqlCte] = []
        combine_sources = [group[0].cte_name for group in measure_groups] + [
            f"conversion_leaf_{index + 1}" for index in range(len(conversion_aliases))
        ]
        source_alias_groups = [
            [
                *(row.bound_measure.alias for row in group),
                *(
                    rows[row.bound_measure.alias]
                    for row in group
                    if row.bound_measure.alias in rows
                ),
            ]
            for group in measure_groups
        ] + [[alias] for alias in conversion_aliases]
        combined_name = combine_sources[0]
        available_aliases = list(source_alias_groups[0])
        for source_index, source_name in enumerate(combine_sources[1:], start=1):
            left_alias = "left_side"
            right_alias = "right_side"
            next_name = f"combined_{source_index + 1}"
            select_fields: list[SqlField] = []
            for key in key_aliases:
                select_fields.append(
                    SqlField(
                        SqlCall(
                            "COALESCE",
                            [
                                SqlIdentifier(parts=[left_alias, key]),
                                SqlIdentifier(parts=[right_alias, key]),
                            ],
                        ),
                        key,
                    )
                )
            for alias in available_aliases:
                select_fields.append(SqlField(SqlIdentifier(parts=[left_alias, alias]), alias))
            current_aliases = list(source_alias_groups[source_index])
            for current_alias in current_aliases:
                select_fields.append(
                    SqlField(SqlIdentifier(parts=[right_alias, current_alias]), current_alias)
                )
            combine_ctes.append(
                SqlCte(
                    name=next_name,
                    query=SqlSelect(
                        select=select_fields,
                        from_table=SqlTableRef(name=combined_name, alias=left_alias),
                        joins=[
                            SqlJoin(
                                join_type="FULL OUTER",
                                table=SqlTableRef(name=source_name, alias=right_alias),
                                on=_join_condition(
                                    key_aliases, left_alias, right_alias, dialect=_dialect(config)
                                ),
                            )
                        ],
                    ),
                )
            )
            combined_name = next_name
            available_aliases.extend(current_aliases)

        base_leaf_select = SqlSelect(
            ctes=[*leaf_ctes, *combine_ctes],
            select=[
                SqlField(SqlIdentifier(parts=["base", alias]), alias)
                for alias in [*key_aliases, *measure_aliases, *rows.values()]
            ],
            from_table=SqlTableRef(name=combined_name, alias="base"),
        )
    else:
        base_leaf_select = _distinct_value_select(plan, config)

    time_alias = key_aliases[-1] if plan.time else ""
    group_aliases = key_aliases[:-1] if plan.time else list(key_aliases)

    fill_binding = _calendar_fill_binding(
        plan, config, force=_query_requires_dense_series(plan, config)
    )
    # A window total answers its one row even when no row fell in the window; the guard
    # then reads it as 0 or NULL by the loaded coverage, like any filled period.
    total_spine = bool(
        plan.time.get("window_total")
        and not group_aliases
        and plan.measure_plans
        and _emits_time_coverage(plan, config)
    )
    # When no calendar fill is needed, the `leaf_base` CTE is a passthrough
    # rename of the underlying leaf (`SELECT base.X AS X FROM <leaf> AS base`)
    # — pure noise. Skip it whenever we have leaf CTEs to reference directly,
    # whether they came from measures or from conversion metrics.
    if fill_binding is None and not total_spine and (plan.measure_plans or conversion_exprs):
        ctes: list[SqlCte] = list(base_leaf_select.ctes)
        assert isinstance(base_leaf_select.from_table, SqlTableRef)  # leaf select uses table ref
        base_table = SqlTableRef(name=base_leaf_select.from_table.name, alias="base")
        base_name = base_table.alias
    else:
        ctes = [SqlCte(name="leaf_base", query=base_leaf_select)]
        base_table = SqlTableRef(name="leaf_base", alias="base")
        base_name = "leaf_base"

    if fill_binding is not None or total_spine:
        ctes.extend(
            _series_base_ctes(
                plan,
                config,
                fill_binding,
                time_alias,
                group_aliases,
                [*measure_aliases, *rows.values()],
            )
        )
        base_name = "series_base"
        base_table = SqlTableRef(name=base_name, alias="base")

    # What the answer states beside its rows, in hidden columns the runtime reads and removes:
    # each re-aggregated measure's row count, and how many rows each share's total spans.
    hidden = [
        SqlField(SqlIdentifier(parts=["base", name]), name)
        for name in overrides.values()
        if name in rows.values()
    ]
    kept = [field.alias for field in hidden]  # the guard passes these source columns through
    shares = share_group_counts(plan, config) if plan_is_root() else {}
    # The one place an empty group reads 0 instead of NULL: the projection reads every such
    # measure through the guard below, so nothing after it can change what it sees.
    zero = zero_aliases(plan.measure_plans, config) if guard_empty else {}
    if zero and plan.time:
        measures = _measure_index(config)
        refuse_declared_edges(
            (
                measures[row.bound_measure.measure_id]
                for row in plan.measure_plans
                if row.bound_measure.alias in zero
            ),
            _dialect(config),
        )
    projected_fields: list[SqlField] = [
        SqlField(SqlIdentifier(parts=["base", alias]), alias) for alias in key_aliases
    ]
    for alias, post_expr in plan.post_aggregation_exprs.items():
        with binding_cut() if project_is_cut() else nullcontext():
            compiled_output = _compile_post_expr(
                _parse_public_expr(post_expr),
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=str(plan.time.get("grain", "") if plan.time else ""),
                table_alias="base",
            )
        projected_fields.append(SqlField(compiled_output, alias))
        if alias in shares:
            share = _parse_public_expr(post_expr)
            assert isinstance(share, ShareExpr)
            spanned = SqlWindow(
                function=SqlCall("COUNT", [SqlLiteral(1)]),
                partition_by=share_partition(share, "base", group_aliases, time_alias, config),
            )
            hidden.append(SqlField(spanned, shares[alias]))
        # Only an output that follows the rule (a sum, count or difference of them): a
        # prior-period, ratio or window output is NULL for reasons other than no data.
        if zero and expr_resolves_to_zero(_parse_public_expr(post_expr), config):
            reads = base_reads(compiled_output) & zero.keys()
            record_zero_output(
                alias,
                (
                    row.bound_measure.measure_id
                    for row in plan.measure_plans
                    if row.bound_measure.alias in reads
                ),
            )
        elif guard_empty and (
            lookups := carried_zero_lookups(_parse_public_expr(post_expr), config)
        ):
            record_zero_output(alias, lookups)
        reads = base_reads(compiled_output)
        record_balance_output(
            alias,
            reads,
            (
                row.bound_measure.measure_id
                for row in plan.measure_plans
                if row.bound_measure.alias in reads
            ),
        )

    if zero:
        ctes.extend(
            guard_empty_groups(
                base_table.name,
                key_aliases,
                measure_aliases,
                zero,
                scopes,
                rows=rows,
                earlier={
                    row.bound_measure.alias
                    for row in plan.measure_plans
                    if has_nested_case(_measure_index(config)[row.bound_measure.measure_id])
                },
                time_key=time_alias if _emits_time_coverage(plan, config) else "",
                dialect=_dialect(config),
                observed=observed,
                observed_buckets=retained,
                bucketed=bool(plan.time),
                keep=kept,
                names={
                    measure: [
                        f.alias for f in projected_fields if measure in base_reads(f.expression)
                    ]
                    for measure in measure_aliases
                },
            )
        )
        base_table = SqlTableRef(name=GUARDED_BASE, alias="base")

    metric_filter_aliases: list[str] = []
    for index, item in enumerate(list(plan.query.get("metric_filters", []) or [])):
        filter_expr = _parse_public_expr(dict(item["expression"]))
        if isinstance(filter_expr, MetricPredicateExpr):
            continue
        alias = _metric_filter_alias(index)
        metric_filter_aliases.append(alias)
        with binding_cut():
            compiled_expr = _compile_post_expr(
                filter_expr,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=str(plan.time.get("grain", "") if plan.time else ""),
                table_alias="base",
            )
        projected_fields.append(SqlField(compiled_expr, alias))

    final_where: list[Any] = []
    metric_filter_position = 0
    for item in list(plan.query.get("metric_filters", []) or []):
        filter_expr = _parse_public_expr(dict(item["expression"]))
        if isinstance(filter_expr, MetricPredicateExpr):
            continue
        alias = metric_filter_aliases[metric_filter_position]
        metric_filter_position += 1
        op = str(item.get("op", "="))
        value = item.get("value")
        projected_ref = SqlIdentifier(parts=["projected", alias])
        if (
            isinstance(filter_expr, (BooleanExpr, ComparisonExpr))
            and value is None
            and str(op).upper() in {"=", "IS"}
        ):
            # A bare comparison/boolean expression IS the predicate. The
            # envelope defaults (op '=', value null) used to compare the
            # compiled boolean to NULL — `(revenue > 100) IS NULL` —
            # which silently dropped every row. Treat the expression as
            # the filter itself instead.
            final_where.append(SqlBinary(projected_ref, "=", SqlLiteral(True)))
        else:
            final_where.append(
                build_filter_condition(projected_ref, op, value, path="metric_filters")
            )

    order_by = _requested_order(plan, "projected")
    if plan.time.get("window_total"):
        # A single row per group has no time order.
        order_by = [
            item
            for item in order_by
            if not (
                isinstance(item.expression, SqlIdentifier)
                and item.expression.parts[-1] == time_alias
            )
        ]

    if guard_empty:
        refuse_unsettled(
            SqlSelect(select=projected_fields, from_table=base_table),
            plan,
            config,
            combined=False,
        )
    projected_fields.extend(hidden)

    if not final_where:
        # Inline the `projected` passthrough CTE directly into the final SELECT.
        # When there are no metric filters (final_where empty), the CTE adds
        # only a layer of `projected.X AS X` aliasing. ORDER BY still works
        # because Snowflake and DuckDB both allow ordering by output alias —
        # we rewrite `projected.X` → bare `X` in the ORDER BY clause.
        inlined_order_by = [
            SqlOrder(
                SqlIdentifier(parts=[item.expression.parts[-1]])
                if isinstance(item.expression, SqlIdentifier)
                and len(item.expression.parts) == 2
                and item.expression.parts[0] == "projected"
                else item.expression,
                item.direction,
                nulls_last=item.nulls_last,
            )
            for item in order_by
        ]
        return _tier_internal_aliases(
            plan,
            SqlSelect(
                ctes=ctes,
                select=projected_fields,
                from_table=base_table,
                order_by=inlined_order_by,
                limit=plan.query.get("limit"),
            ),
            measure_aliases=measure_aliases,
            metric_filter_aliases=metric_filter_aliases,
        )

    projected = SqlSelect(
        ctes=ctes,
        select=projected_fields,
        from_table=base_table,
    )

    final_fields = [
        SqlField(SqlIdentifier(parts=["projected", alias]), alias)
        for alias in [
            *key_aliases,
            *plan.post_aggregation_exprs.keys(),
            *(field.alias for field in hidden),
        ]
    ]

    return _tier_internal_aliases(
        plan,
        SqlSelect(
            ctes=[SqlCte(name="projected", query=projected)],
            select=final_fields,
            from_table=SqlTableRef(name="projected"),
            where=final_where,
            order_by=order_by,
            limit=plan.query.get("limit"),
        ),
        measure_aliases=measure_aliases,
        metric_filter_aliases=metric_filter_aliases,
    )
