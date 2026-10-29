"""Planning: a normalized query becomes a logical plan, with its bound measures, paths, rewrites
and the config extended by the measures a conditional aggregate lifts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import Any

from ..acceleration.selection import _select_aggregate_relation
from ..ast import (
    NormalizedQuery,
    child_groups,
    every_filter,
    normalize_query,
    refuse_child_groups,
    time_output_alias,
)
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    ConversionExpr,
    MetricPredicateExpr,
    SemanticExpr,
    expr_to_dict,
    is_constant_expression,
    validate_expression_calls,
)
from ..ir import (
    BoundMeasure,
    LogicalPlan,
    MeasurePlan,
    RewriteStep,
)
from ..registry import Registry
from ..schema import (
    PackageConfig,
    base_of,
)
from ..temporal_support import validate_temporal_support
from .bind import (
    _bound_filter_clauses,
    _collect_conversion_exprs,
    _collect_measure_refs,
    _freeze_payload,
    lift_conditional_aggregates,
)
from .dependencies import (
    candidate_planning,
)
from .governed_names import refuse_impersonating_aliases
from .indexes import (
    _measure_index,
    get_package_analysis,
)
from .leaf_paths import (
    _infer_root_entity_for_distinct_query,
    _leaf_path_selections,
    _root_path_summary,
)
from .lowering_common import (
    _plan_requires_agent_dag_lowering,
    _validate_metric_predicate_filter_envelope,
)
from .parent_values import (
    PARENT_VALUE,
    parent_rows,
    parent_value_selections,
    parent_value_step,
    parent_values_active,
)
from .paths import (
    _expression_root_entity,
    _leaf_time_role,
)
from .plan_checks import (
    _answers_every_period,
    _hop_steps,
    _time_valued_groups,
    _validate_measure_validity_windows,
    _validate_non_additive_sums,
    _window_total,
    observed_only_groups,
)
from .predicate_sets import _reads_chosen_snapshot
from .semantic_dag import _semantic_dag_for_query
from .temporal import (
    _validate_query_temporal_bindings,
    _validate_restrictive_time_semantics,
    _window_measure_exprs,
)


def _extend_config_with_synthetic_measures(
    config: PackageConfig, synthetic: dict[str, Any]
) -> PackageConfig:
    """Return a new PackageConfig whose ``measures`` list includes the
    synthetic measures produced by ``aggregate_if``. The original config
    is left untouched. Downstream lookups via ``_measure_index`` work
    unchanged because the synthetic entries are now in the list.
    """
    if not synthetic:
        return config
    extended = replace(config, measures=[*config.measures, *synthetic.values()])
    # A caller's view stays one: its routes still come from the whole package.
    get_package_analysis(extended).view = get_package_analysis(config).view
    return extended


def resolve_compile_config(plan: LogicalPlan, config: PackageConfig) -> PackageConfig:
    """Public helper for callers (``compile_query``, ``lower_to_sql``
    wrappers, validators) that need the synthetic-measure-extended
    config produced by ``plan_query``. When the plan has no synthetics
    the original config is returned unchanged.
    """
    return _extend_config_with_synthetic_measures(config, plan.synthetic_measures)


def plan_query(
    config: PackageConfig,
    registry: Registry | None,
    payload: dict[str, Any],
) -> LogicalPlan:
    validate_temporal_support(config, payload)
    raw_query = normalize_query(payload, config=config)
    with candidate_planning():
        plan = _plan_query(config, registry, raw_query, collapse_window=True, top_level=True)
    # Last, so a query that fails for any other reason (an unknown or hidden object, a route, a
    # policy) is refused for that reason first.
    refuse_impersonating_aliases(raw_query, config)
    return plan


def _plan_query(
    config: PackageConfig,
    registry: Registry | None,
    raw_query: NormalizedQuery,
    *,
    collapse_window: bool,
    top_level: bool = False,
) -> LogicalPlan:
    validate_expression_calls(raw_query, dialect_for_warehouse(config.package.warehouse))
    if (
        top_level
        and raw_query.select
        and all(is_constant_expression(item.expression) for item in raw_query.select)
        and not (
            raw_query.group_by or raw_query.where or raw_query.time or raw_query.metric_filters
        )
    ):
        raise SemanticLayerError(
            "INVALID_QUERY",
            "A select of literals only reads no data; add a measure, a group_by dimension or time",
            details={"reason": "literal_only_select"},
        )

    # Lift inline ``aggregate_if`` shorthand into synthetic measures. The
    # rest of plan_query (and every downstream pass) operates on
    # ``config`` extended with those synthetics so the measure-lookup
    # machinery sees them via the normal ``_measure_index`` path — no
    # per-call-site threading required. The synthetics are also stored
    # on the LogicalPlan so callers can rebuild the extended config via
    # ``resolve_compile_config`` before invoking ``lower_to_sql`` or
    # ``attach_relation_ctes``.
    query, synthetic_measures = lift_conditional_aggregates(raw_query, config)
    config = _extend_config_with_synthetic_measures(config, synthetic_measures)
    if query.time is not None and query.time.fill and (held := _time_valued_groups(query, config)):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"time.fill can't fill a series grouped by a date or time dimension ({held[0]}): "
            "every observed instant would be padded into every period. Remove 'fill'.",
            details={"path": "time.fill", "group_by": held},
        )
    if (
        collapse_window
        and query.time is not None
        and _answers_every_period(query, config)
        and not observed_only_groups(query, config)
    ):
        query = replace(query, time=replace(query.time, fill=True))

    measures = _measure_index(config)

    _validate_query_temporal_bindings(query, config)

    bound_measures: list[BoundMeasure] = []
    for items in (query.select, query.metric_filters):
        for item in items:
            if item.expression is None:
                continue
            if items is query.metric_filters and isinstance(item.expression, MetricPredicateExpr):
                _validate_metric_predicate_filter_envelope(asdict(item))
                continue
            _collect_measure_refs(item.expression, config, query, bound_measures)

    def window_filters(expr: SemanticExpr) -> Iterable[dict[str, Any]]:
        inputs: list[BoundMeasure] = []
        for leaf in _window_measure_exprs(expr, config, window_inputs=True):
            _collect_measure_refs(leaf, config, query, inputs)
        return (clause for row in inputs for clause in _bound_filter_clauses(row, config))

    _validate_restrictive_time_semantics(query, config, measure_filters=window_filters)
    conversion_exprs: list[ConversionExpr] = []
    for items in (query.select, query.metric_filters):
        for item in items:
            if item.expression is not None and not (
                items is query.metric_filters and isinstance(item.expression, MetricPredicateExpr)
            ):
                _collect_conversion_exprs(item.expression, config, conversion_exprs)

    dedup_measures = {
        (
            row.alias,
            row.aggregation,
            row.temporal_role,
            _freeze_payload(row.aggregation_params),
            _freeze_payload(row.filter_spec),
            _freeze_payload(row.window_spec),
        ): row
        for row in bound_measures
    }
    bound_measures = list(dedup_measures.values())
    # Check every condition, including child groups, before route planning can refuse a
    # stock's child path. Lowering uses this same classifier for when to apply the filter.
    for bound in bound_measures:
        measure = measures[bound.measure_id]
        for item in every_filter(query.where):
            _reads_chosen_snapshot(measure, bound.temporal_role, item.field, config)
        for clause in _bound_filter_clauses(bound, config):
            _reads_chosen_snapshot(measure, bound.temporal_role, str(clause["field"]), config)
    _validate_measure_validity_windows(bound_measures, base_of(config), query, config)
    _validate_non_additive_sums(bound_measures, config, query)
    measure_plans: list[MeasurePlan] = []
    leaf_strategies: list[str] = []  # each leaf's strategy before rollup routing
    if bound_measures:
        # A call bound with parent values reads them from the rows it groups by.
        rows = parent_rows(query, config, bound_measures) if parent_values_active() else ""
        root_entity = rows or measures[bound_measures[0].measure_id].entity
        selected_paths, candidate_paths, rewrite_steps, root_analyses = _root_path_summary(
            root_entity, bound_measures, config, query
        )
        if query.time is not None:
            for bound in bound_measures:
                leaf_role = _leaf_time_role(bound, query, config)
                if (
                    leaf_role
                    and leaf_role != query.time.temporal_role
                    and not any(step.measure_id == bound.measure_id for step in rewrite_steps)
                ):
                    rewrite_steps.append(
                        RewriteStep(
                            kind="metric_time_alignment",
                            status="applied",
                            measure_id=bound.measure_id,
                            reason="Metric leaf uses its own compatible time role and is aligned to the requested query grain",
                            details={
                                "requested_temporal_role": query.time.temporal_role,
                                "leaf_temporal_role": leaf_role,
                                "grain": query.time.grain,
                            },
                        )
                    )

        for index, bound in enumerate(bound_measures):
            parent = parent_value_selections(config, query, rows, bound) if rows else None
            if parent is not None:
                path_selections, required_entities = parent
                crossing = PARENT_VALUE
            else:
                path_selections, required_entities, crossing = _leaf_path_selections(
                    bound, config, query
                )
            grain_keys = [*query.group_by]
            if query.time is not None:
                grain_keys.append(time_output_alias(query.time))
            rewrite_strategy = (
                "leaf_preaggregate_join"
                if any(step.measure_id == bound.measure_id for step in rewrite_steps)
                else "direct"
            )
            if crossing == "fanout_dedup":
                rewrite_strategy = "fanout_dedup"
            if measures[bound.measure_id].lookup_from:
                rewrite_strategy = "parent_lookup"
            source_entity = measures[bound.measure_id].entity
            if parent is not None:
                # Read from the rows, never a rollup of its own model.
                rewrite_strategy, source_entity = PARENT_VALUE, rows
                rewrite_steps.append(
                    parent_value_step(config, query, rows, bound, path_selections[0].chosen_path)
                )
                aggregate_relation_id = ""
                aggregate_relation_rejections: dict[str, str] = {}
            else:
                # Every leaf that crosses a one-to-many hop says how.
                rewrite_steps.extend(_hop_steps(crossing, bound, path_selections, config, query))
                aggregate_relation_id, aggregate_relation_rejections = _select_aggregate_relation(
                    bound=bound,
                    query=query,
                    config=config,
                    path_selections=path_selections,
                )
            measure_plans.append(
                MeasurePlan(
                    cte_name=f"leaf_{index + 1}",
                    bound_measure=bound,
                    source_entity=source_entity,
                    path_selections=path_selections,
                    grain_keys=grain_keys,
                    rewrite_strategy="aggregate_relation"
                    if aggregate_relation_id
                    else rewrite_strategy,
                    required_entities=required_entities,
                    aggregate_relation_id=aggregate_relation_id,
                    aggregate_relation_rejections=aggregate_relation_rejections,
                )
            )
            leaf_strategies.append(rewrite_strategy)
    elif conversion_exprs:
        root_entity = _expression_root_entity(conversion_exprs[0].base, config)
        selected_paths = {}
        candidate_paths = {}
        rewrite_steps = []
        root_analyses = {}
    else:
        root_summary = _infer_root_entity_for_distinct_query(config, query)
        root_entity = str(root_summary["root_entity"])
        selected_paths = dict(root_summary["selected_paths"])
        candidate_paths = dict(root_summary["candidate_paths"])
        rewrite_steps = []
        root_analyses = dict(root_summary["analyses"])

    grain_analysis = {
        "root_entity": root_entity,
        "group_by": list(query.group_by),
        "time": asdict(query.time) if query.time else {},
        "measure_entities": sorted({measures[row.measure_id].entity for row in bound_measures}),
        "leaf_grains": {
            row.bound_measure.measure_id: list(row.grain_keys) for row in measure_plans
        },
    }
    fanout_strategy = {
        "status": "rewritten" if rewrite_steps else "ok",
        "root_paths": root_analyses,
        "leaf_paths": {
            row.bound_measure.measure_id: [asdict(item) for item in row.path_selections]
            for row in measure_plans
        },
    }
    resolved_ids = {
        "select": [
            {
                "alias": item.as_,
                "expression": expr_to_dict(item.expression)
                if item.expression is not None
                else None,
            }
            for item in query.select
        ],
        "group_by": list(query.group_by),
        "where": [asdict(item) for item in query.where],
        "temporal_role": query.time.temporal_role if query.time else "",
    }
    post_exprs = {
        item.as_: expr_to_dict(item.expression)
        for item in query.select
        if item.expression is not None
    }
    plan_time = asdict(query.time) if query.time else {}
    if collapse_window and _window_total(query, config, bound_measures):
        plan_time["window_total"] = True
    plan = LogicalPlan(
        version=2,
        query=query.to_dict(),
        root_entity=root_entity,
        resolved_ids=resolved_ids,
        selected_paths=selected_paths,
        candidate_paths=candidate_paths,
        group_by=list(query.group_by),
        time=plan_time,
        bound_measures=bound_measures,
        measure_plans=measure_plans,
        post_aggregation_exprs=post_exprs,
        grain_analysis=grain_analysis,
        fanout_strategy=fanout_strategy,
        rewrite_steps=rewrite_steps,
        semantic_dag=_semantic_dag_for_query(query, config),
        synthetic_measures=dict(synthetic_measures),
    )
    _require_child_group_leaves(plan, query, conversion_exprs, config)
    from .time_series import coverage_base_plan

    return coverage_base_plan(plan, config, leaf_strategies)


def _require_child_group_leaves(
    plan: LogicalPlan,
    query: NormalizedQuery,
    conversion_exprs: list[ConversionExpr],
    config: PackageConfig,
) -> None:
    """Refuse a plan whose child groups some lowering would not apply.

    Only the semi-join leaf (``_fanout_filter_leaf_select``, or ClickHouse's de-duplicated
    parent leaf) lowers a group, so every measure leaf must cross to each group's child with
    the fanout_dedup rewrite and read no rollup; a conversion or distribution branch lowers
    its own filters and must not see one.
    """
    groups = child_groups(query.where)
    if not groups:
        return
    if conversion_exprs:
        refuse_child_groups(query.where, "in a conversion metric")
    if _plan_requires_agent_dag_lowering(plan, config):
        refuse_child_groups(query.where, "beside a distribution")
    children = {group.child for group in groups}
    for measure_plan in plan.measure_plans:
        routed = {
            row.target_entity
            for row in measure_plan.path_selections
            if row.purpose == "child_group"
        }
        if (
            measure_plan.rewrite_strategy != "fanout_dedup"
            or measure_plan.aggregate_relation_id
            or routed != children
        ):
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"The child groups in 'where' cannot filter '{measure_plan.bound_measure.measure_id}'.",
                details={
                    "path": "where",
                    "measure": measure_plan.bound_measure.measure_id,
                    "why_invalid": (
                        "A child group filters a measure's rows with a semi-join on its child; "
                        "this measure's leaf is planned another way."
                    ),
                },
            )
