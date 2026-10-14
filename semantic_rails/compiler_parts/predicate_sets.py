"""Predicate sets in SQL: each entity filter's population, an entity's value through its
anchor, and the snapshot reads they share.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..ast import (
    is_child_group,
    normalize_query,
    refuse_child_groups,
)
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    RatioExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    ShareExpr,
    expr_to_dict,
    expression_field,
)
from ..ir import (
    LogicalPlan,
    MeasurePlan,
    PathSelection,
    PhysicalPlanNode,
)
from ..naming import last_token as _last_token
from ..naming import slug as _slug
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlSelect,
    SqlTableRef,
)
from . import nested
from .bind import (
    _bind_scoped_aggregate,
    _bound_filter_clauses,
    _config_expr_to_sql,
    _expression_alias,
    _freeze_payload,
    _parse_public_expr,
    _scoped_predicate_expr_payload,
)
from .dependencies import (
    binding_cut,
    capture_objects,
    leaf_predicate_roles,
    project_is_cut,
    recipe_objects,
    record_ids,
    record_leaf_reference,
)
from .empty_groups import (
    observation_scope,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
)
from .lowering_common import (
    _and_conditions,
    _apply_role_timezone,
    _dialect,
    _measure_dim_relation,
    _measure_owned_relation,
    _plan_requires_agent_dag_lowering,
    _query_key_aliases,
    _snapshot_series_columns,
    _source_time_window,
    _time_alias_for_plan,
    _time_bucket_expr,
    _value_filter_condition,
)
from .paths import (
    _column_ref,
    _direct_dimension_source_expr,
    _direct_entity_key_source_expr,
    _entity_key_dimension_ids,
    _expression_root_entity,
    _join_condition,
    _joins_for_paths,
    _leaf_time_role,
    _resolve_dimension_expr,
)
from .post_aggregation import (
    _namespace_sql_select,
)
from .predicate import (
    _inline_threshold_cte_and_where,
    _predicate_includes_entities_without_rows,
    _predicate_time_alias,
    _predicate_time_spec,
    _public_time_spec,
    _require_predicate_over_rows,
)
from .snapshot_reads import SnapshotRead, closing_day, select_snapshots
from .time_series import _window_is_named


@dataclass(frozen=True)
class PredicateSetSql:
    ctes: list[SqlCte]
    set_name: str
    key_aliases: list[str]
    time_alias: str = ""
    source_relation: str = ""
    metric_label: str = ""


@dataclass(frozen=True)
class AnchoredEntitySetPlan:
    output_alias: str
    numerator: ScopedAggregateExpr
    denominator: ScopedAggregateExpr
    denominator_measure_plan: MeasurePlan
    anchor_entity: str
    extra_predicates: list[dict[str, Any]]
    base_predicates: list[dict[str, Any]]
    projection_objects: set[str]


def _semantic_set_name(predicate: MetricPredicateExpr, index: int) -> str:
    label = "predicate"
    if isinstance(predicate.input, MetricRecipeRefExpr):
        label = _last_token(predicate.input.metric_recipe)
    entity_label = _last_token(predicate.entity).replace("entity_", "")
    return f"{_slug(label, fallback='predicate')}_{_slug(entity_label, fallback='entity')}_set_{index + 1}"


def _scoped_aggregate_from_expr(
    expr: SemanticExpr, config: PackageConfig
) -> ScopedAggregateExpr | None:
    if isinstance(expr, ScopedAggregateExpr):
        record_leaf_reference(_expression_alias(expr, config))
        return expr
    if isinstance(expr, MeasureRefExpr):
        record_leaf_reference(_expression_alias(expr, config))
        return ScopedAggregateExpr(
            measure=expr.measure,
            aggregation=expr.aggregation,
            temporal_role=expr.temporal_role,
            parameters=dict(expr.parameters or {}),
        )
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            return None
        with recipe_objects(recipe.id):
            return _scoped_aggregate_from_expr(recipe.expression, config)
    return None


def _predicate_expr_from_payload(payload: dict[str, Any]) -> MetricPredicateExpr:
    expr = _parse_public_expr(_scoped_predicate_expr_payload(dict(payload)))
    if not isinstance(expr, MetricPredicateExpr):
        raise SemanticLayerError("INVALID_METRIC_PREDICATE", "Expected metric_predicate expression")
    return expr


def _predicate_key_aliases(predicate: MetricPredicateExpr, config: PackageConfig) -> list[str]:
    return _entity_key_dimension_ids(predicate.entity, config)


def _expression_scan_specs(expr: SemanticExpr, config: PackageConfig) -> list[dict[str, Any]]:
    entities = _entity_index(config)
    measures = _measure_index(config)
    recipes = _recipe_index(config)

    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None:
            return []
        specs = _expression_scan_specs(recipe.expression, config)
        for spec in specs:
            spec.setdefault("metric_recipes", []).append(expr.metric_recipe)
        return specs

    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        measure_id = expr.measure
        measure = measures.get(measure_id)
        if measure is None:
            return []
        relation = _measure_owned_relation(measure, entities)
        return [
            {
                "source_entity": measure.entity,
                "relation": relation,
                "selected_relation": relation,
                "selected_relation_type": "raw",
                "measures": [
                    {
                        "measure_id": measure_id,
                        "aggregation": getattr(expr, "aggregation", "")
                        or measure.default_aggregation,
                    }
                ],
            }
        ]

    if isinstance(expr, ConversionExpr):
        specs = []
        for role, child in (
            ("conversion_base", expr.base),
            ("conversion_converted", expr.converted),
        ):
            for spec in _expression_scan_specs(child, config):
                spec.setdefault("conversion_roles", []).append(role)
                specs.append(spec)
        return specs

    if isinstance(expr, (ArithmeticExpr, ComparisonExpr, RatioExpr)):
        specs = _expression_scan_specs(
            expr.left if hasattr(expr, "left") else expr.numerator, config
        )
        specs.extend(
            _expression_scan_specs(
                expr.right if hasattr(expr, "right") else expr.denominator, config
            )
        )
        return specs

    if isinstance(expr, (BooleanExpr, CallExpr)):
        specs = []
        for arg in expr.args:
            specs.extend(_expression_scan_specs(arg, config))
        return specs

    if isinstance(expr, CaseExpr):
        specs = []
        for item in expr.whens:
            specs.extend(_expression_scan_specs(item.when, config))
            specs.extend(_expression_scan_specs(item.then, config))
        if expr.else_expr is not None:
            specs.extend(_expression_scan_specs(expr.else_expr, config))
        return specs

    if isinstance(
        expr,
        (
            OffsetWindowExpr,
            ShareExpr,
            EntityValueExpr,
            MetricPredicateExpr,
        ),
    ):
        return _expression_scan_specs(expr.input, config)

    if isinstance(expr, DistributionExpr):
        return _expression_scan_specs(expr.over, config)

    return []


def _predicate_physical_nodes(
    predicates: list[MetricPredicateExpr],
    *,
    prefix: str,
    plan: LogicalPlan,
    config: PackageConfig,
) -> tuple[list[PhysicalPlanNode], list[str]]:
    nodes: list[PhysicalPlanNode] = []
    predicate_set_ids: list[str] = []
    query = normalize_query(plan.query)

    for predicate_index, predicate in enumerate(predicates, start=1):
        scan_specs = _expression_scan_specs(predicate.input, config)
        source_ids: list[str] = []
        time_filter = dict(_predicate_time_spec(predicate, query, config) or {})
        for source_index, spec in enumerate(scan_specs, start=1):
            scan_id = f"{prefix}_predicate_{predicate_index}_scan_{source_index}"
            filter_id = f"{prefix}_predicate_{predicate_index}_filter_{source_index}"
            relation = str(spec.get("selected_relation") or spec.get("relation") or "")
            nodes.append(
                PhysicalPlanNode(
                    id=scan_id,
                    kind="Scan",
                    label=f"Scan predicate source {relation}",
                    details={
                        "source_entity": spec.get("source_entity", ""),
                        "relation": relation,
                        "selected_relation": relation,
                        "selected_relation_type": spec.get("selected_relation_type", "raw"),
                        "measures": list(spec.get("measures", []) or []),
                        "predicate": expr_to_dict(predicate),
                        "predicate_input": expr_to_dict(predicate.input),
                        "metric_recipes": list(spec.get("metric_recipes", []) or []),
                        "conversion_roles": list(spec.get("conversion_roles", []) or []),
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=filter_id,
                    kind="Filter",
                    inputs=[scan_id],
                    details={
                        "query_filters": [],
                        "measure_filters": [],
                        "time_filter": time_filter,
                        "pushed_to_scan": bool(time_filter),
                    },
                )
            )
            source_ids.append(filter_id)

        set_id = f"{prefix}_predicate_set_{predicate_index}"
        nodes.append(
            PhysicalPlanNode(
                id=set_id,
                kind="PredicateSet",
                inputs=source_ids,
                details={
                    "entity": predicate.entity,
                    "keys": _predicate_key_aliases(predicate, config),
                    "op": predicate.op,
                    "value": predicate.value,
                    "scope_mode": predicate.scope_mode,
                    "source_scan_count": len(scan_specs),
                    **(
                        {"anti_join": True}
                        if _predicate_includes_entities_without_rows(predicate, config)
                        else {}
                    ),
                },
            )
        )
        predicate_set_ids.append(set_id)

    return nodes, predicate_set_ids


def _filter_condition_for_item(
    measure_entity: str, item: dict[str, Any], config: PackageConfig
) -> Any:
    expr, _ = _direct_dimension_source_expr(
        measure_entity, str(item["field"]), config
    ) or _resolve_dimension_expr(str(item["field"]), config)
    return _value_filter_condition(expr, item, config)


def _source_local_filter_conditions(
    measure_entity: str, items: Iterable[dict[str, Any]], config: PackageConfig
) -> list[Any] | None:
    conditions: list[Any] = []
    for item in items:
        if is_child_group(item):
            return None
        dim_id = str(item.get("field", "") or "")
        if (
            _direct_dimension_source_expr(measure_entity, dim_id, config) is None
            and _dimension_index(config)[dim_id].entity != measure_entity
        ):
            return None
        conditions.append(_filter_condition_for_item(measure_entity, item, config))
    return conditions


def _measure_time_components(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> tuple[Any | None, Any | None, str, bool, str]:
    if not plan.time:
        return None, None, "", True, ""
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    leaf_time_role = _leaf_time_role(
        measure_plan.bound_measure, normalize_query(plan.query), config
    )
    role = temporal_roles[leaf_time_role]
    dim = dimensions[role.dimension]
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    direct_time = _direct_dimension_source_expr(
        measure.entity,
        role.dimension,
        config,
        source_relation_override=getattr(measure, "source_relation", "") or "",
    )
    time_source_local = dim.entity == measure.entity or direct_time is not None
    raw_expr = (
        direct_time[0]
        if direct_time is not None
        else _column_ref(_measure_dim_relation(measure, dim, entities), dim.column)
    )
    raw_expr = _apply_role_timezone(raw_expr, role, config)
    time_expr = _time_bucket_expr(plan.time, raw_expr, config)
    return raw_expr, time_expr, _time_alias_for_plan(plan), time_source_local, leaf_time_role


def _matching_measure_plan(
    plan: LogicalPlan, expr: ScopedAggregateExpr, config: PackageConfig
) -> MeasurePlan | None:
    query = normalize_query(plan.query)
    bound = _bind_scoped_aggregate(expr, config, query)
    for measure_plan in plan.measure_plans:
        if measure_plan.bound_measure.alias == bound.alias:
            return measure_plan
    return None


def _predicate_payload_set(predicates: list[dict[str, Any]]) -> set[str]:
    return {_freeze_payload(_scoped_predicate_expr_payload(dict(item))) for item in predicates}


def _anchored_entity_set_plan(
    plan: LogicalPlan, config: PackageConfig
) -> AnchoredEntitySetPlan | None:
    if _plan_requires_agent_dag_lowering(plan, config):
        return None
    if len(plan.post_aggregation_exprs) != 1:
        return None
    # An explicit fill has no anchored form; a named window keeps the periods with rows here.
    if plan.query.get("metric_filters") or (
        plan.time and plan.time.get("fill") and not _window_is_named(plan.time)
    ):
        return None

    output_alias, expr_payload = next(iter(plan.post_aggregation_exprs.items()))
    expr = _parse_public_expr(expr_payload)
    if not isinstance(expr, RatioExpr):
        return None
    projection_objects: set[str] = set()
    with capture_objects(projection_objects):
        numerator = _scoped_aggregate_from_expr(expr.numerator, config)
        denominator = _scoped_aggregate_from_expr(expr.denominator, config)
    if numerator is None or denominator is None:
        return None
    if numerator.measure != denominator.measure:
        return None
    if (numerator.aggregation or "").lower() != (denominator.aggregation or "").lower():
        return None
    if str(numerator.temporal_role or "") != str(denominator.temporal_role or ""):
        return None
    if dict(numerator.parameters or {}) != dict(denominator.parameters or {}):
        return None
    if [dict(item) for item in list(numerator.where or [])] != [
        dict(item) for item in list(denominator.where or [])
    ]:
        return None

    numerator_predicates = [dict(item) for item in list(numerator.predicates or [])]
    denominator_predicates = [dict(item) for item in list(denominator.predicates or [])]
    denominator_predicate_keys = _predicate_payload_set(denominator_predicates)
    extra_predicates = [
        item
        for item in numerator_predicates
        if _freeze_payload(_scoped_predicate_expr_payload(dict(item)))
        not in denominator_predicate_keys
    ]
    if not extra_predicates:
        return None

    denominator_plan = _matching_measure_plan(plan, denominator, config)
    if denominator_plan is None:
        return None
    measure = _measure_index(config)[denominator.measure]
    aggregation = str(denominator_plan.bound_measure.aggregation or "").lower()
    safe_anchor_shape = (aggregation == "sum" and measure.measure_class == "semi_additive") or (
        aggregation == "count_distinct"
        and measure.measure_class == "distinct_population"
        and plan.time
    )
    if not safe_anchor_shape:
        return None
    # A balance that closes on a day closes within each operand's own predicates, which one
    # shared choice of snapshots can't do for both: the share is refused, never answered from
    # another scope's closing day.
    role = _temporal_role_index(config)[denominator_plan.bound_measure.temporal_role]
    if closing_day(measure, role, config) is not None and (
        measure.accumulation.snapshot != "start_of_period"
    ):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"A share of the balance {measure.id} filtered by entity sets isn't supported yet: "
            "ask for the part and the whole as separate measures.",
            details={"reason": "entity_set_ratio_closing_day", "measure": measure.id},
        )

    raw_time_expr, _, _, time_source_local, _ = _measure_time_components(
        plan, denominator_plan, config
    )
    if plan.time and (raw_time_expr is None or not time_source_local):
        return None

    source_filters = _source_local_filter_conditions(
        measure.entity, list(plan.query.get("where", []) or []), config
    )
    if source_filters is None:
        return None
    bound_filters = _source_local_filter_conditions(
        measure.entity, _bound_filter_clauses(denominator_plan.bound_measure, config), config
    )
    if bound_filters is None:
        return None

    predicate_entities = []
    for item in [*denominator_predicates, *extra_predicates]:
        predicate = _predicate_expr_from_payload(item)
        predicate_entities.append(predicate.entity)
        entity_cfg = _entity_index(config)[predicate.entity]
        if not all(
            _direct_entity_key_source_expr(measure.entity, predicate.entity, key_col, config)
            is not None
            for key_col in entity_cfg.key
        ):
            return None

    anchor_entity = predicate_entities[0] if predicate_entities else measure.entity
    if any(entity_id != anchor_entity for entity_id in predicate_entities):
        return None
    return AnchoredEntitySetPlan(
        output_alias=output_alias,
        numerator=numerator,
        denominator=denominator,
        denominator_measure_plan=denominator_plan,
        anchor_entity=anchor_entity,
        extra_predicates=extra_predicates,
        base_predicates=denominator_predicates,
        projection_objects=projection_objects,
    )


def _requested_order(plan: LogicalPlan, source: str) -> list[SqlOrder]:
    """The query's requested order_by terms over ``source``'s output columns.

    Every requested term sorts NULLs last in both directions on every backend, rather
    than by the backend's default, which puts NULLs first under DESC on Postgres and
    Snowflake and would rank a NULL value first in a limited ranking.
    """

    def _field(item: Any) -> str:
        field = str(expression_field(item, "field", expression_position="order_by"))
        if field != "time":
            return field
        if not plan.time:
            raise SemanticLayerError(
                "INVALID_ORDER_BY", "'time' order_by requires a query time axis"
            )
        return _query_key_aliases(plan)[-1]

    return [
        SqlOrder(
            SqlIdentifier(parts=[source, _field(item)]),
            str(item.get("direction", "ASC")) if isinstance(item, dict) else "ASC",
            nulls_last=True,
        )
        for item in list(plan.query.get("order_by", []) or [])
    ]


def _stock_period_split(temporal_role_id: str, dim_id: str, config: PackageConfig) -> bool:
    """Only the stock's clock (same entity and column) or a calendar splits its periods."""
    dimensions = _dimension_index(config)
    dimension = dimensions.get(dim_id)
    if dimension is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND", f"Unknown dimension '{dim_id}'", details={"dimension": dim_id}
        )
    role = _temporal_role_index(config).get(temporal_role_id)
    clock = dimensions.get(role.dimension) if role is not None else None
    return _entity_index(config)[dimension.entity].kind == "time" or (
        clock is not None and (dimension.entity, dimension.column) == (clock.entity, clock.column)
    )


def _reads_chosen_snapshot(
    measure, temporal_role_id: str, dim_id: str, config: PackageConfig
) -> bool:
    """Read attributes after choosing a stock snapshot; ambiguous date attributes refuse."""
    if measure.measure_class != "semi_additive":
        return False
    if _stock_period_split(temporal_role_id, dim_id, config):
        return False
    if _dimension_index(config)[dim_id].data_type in {"date", "timestamp"}:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "Filtering a stock by a date or timestamp attribute is ambiguous. "
            "Filter by the stock's clock or a calendar dimension instead.",
            details={"reason": "stock_filtered_by_date_attribute", "dimension": dim_id},
        )
    return True


def _source_join_columns_for_paths(
    source_entity: str, path_selections: Iterable[PathSelection], config: PackageConfig
) -> list[str]:
    relationships = _relationship_index(config)
    columns: list[str] = []
    for selection in path_selections:
        current_entity = source_entity
        for rel_id in selection.chosen_path[:1]:
            rel = relationships[rel_id]
            if rel.source_entity == current_entity:
                columns.extend(list(rel.source_columns or [rel.source_column]))
            elif rel.target_entity == current_entity:
                columns.extend(list(rel.target_columns or [rel.target_column]))
    return list(dict.fromkeys(column for column in columns if column))


def _minimal_predicate_set_ctes(
    predicate_payload: dict[str, Any],
    *,
    index: int,
    plan: LogicalPlan,
    config: PackageConfig,
) -> PredicateSetSql:
    query = normalize_query(plan.query)
    predicate = _predicate_expr_from_payload(predicate_payload)
    _require_predicate_over_rows(predicate, config, shape="an anchored entity-set ratio")
    group_by_dims = _predicate_key_aliases(predicate, config)
    time_spec = _predicate_time_spec(predicate, query, config)
    mini_query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": expr_to_dict(predicate.input), "as": "__predicate_value"}],
        "group_by": list(group_by_dims),
    }
    time_alias = ""
    source_time_alias = ""
    if time_spec is not None:
        mini_query["time"] = _public_time_spec(time_spec)
        time_alias = _predicate_time_alias(
            str(time_spec.get("output_temporal_role") or time_spec["temporal_role"]),
            str(time_spec["grain"]),
        )
        source_time_alias = _predicate_time_alias(
            str(time_spec["temporal_role"]), str(time_spec["grain"])
        )

    with binding_cut():
        _entity_index(config)[predicate.entity]
    predicate_sql = nested.predicate_source_sql(config, mini_query)
    source_name = f"{_semantic_set_name(predicate, index)}_source"
    set_name = _semantic_set_name(predicate, index)
    source_cte = SqlCte(
        name=source_name, query=_namespace_sql_select(predicate_sql, f"{source_name}__")
    )

    select_fields = [
        SqlField(SqlIdentifier(parts=["predicate_source", dim_id]), dim_id)
        for dim_id in group_by_dims
    ]
    if time_alias:
        select_fields.append(
            SqlField(SqlIdentifier(parts=["predicate_source", source_time_alias]), time_alias)
        )
    # Inline-threshold path (see ``_inline_threshold_cte_and_where``): when
    # ``predicate.value`` is the percentile-expr dict validated at parse time, emit a
    # threshold CTE + CROSS JOIN.
    extra_ctes, extra_joins, where_condition = _inline_threshold_cte_and_where(
        predicate=predicate,
        set_name=set_name,
        source_name=source_name,
        config=config,
    )
    # The mini-query feeding `source_cte` already produces one row per key, so
    # the outer GROUP BY would just re-group identical rows. SELECT DISTINCT
    # expresses "set of qualifying ids" idiomatically and is a no-op when the
    # rows are already distinct.
    set_cte = SqlCte(
        name=set_name,
        query=SqlSelect(
            select=select_fields,
            from_table=SqlTableRef(name=source_name, alias="predicate_source"),
            joins=extra_joins,
            where=[where_condition],
            distinct=True,
        ),
    )
    root_entity = _expression_root_entity(predicate.input, config)
    return PredicateSetSql(
        ctes=[source_cte, *extra_ctes, set_cte],
        set_name=set_name,
        key_aliases=group_by_dims,
        time_alias=time_alias,
        source_relation=_entity_index(config)[root_entity].table,
        metric_label=_last_token(expr_to_dict(predicate.input).get("metric", "predicate")),
    )


def _snapshot_select_fields(
    plan: LogicalPlan,
    anchored: AnchoredEntitySetPlan,
    config: PackageConfig,
) -> tuple[list[SqlField], list[str], Any, Any, str, str]:
    """The fields each chosen snapshot carries, its series aliases, the clock, the query's
    raw time, its time alias and the clock it is read on."""
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    measure_plan = anchored.denominator_measure_plan
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    source_table = _measure_owned_relation(measure, entities)
    raw_time_expr, time_expr, time_alias, time_source_local, leaf_time_role = (
        _measure_time_components(plan, measure_plan, config)
    )
    if plan.time and (raw_time_expr is None or time_expr is None or not time_source_local):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED", "Anchored entity-set optimization requires source-local time"
        )

    order_role = temporal_roles[measure_plan.bound_measure.temporal_role]
    order_dim = dimensions[order_role.dimension]
    order_expr = _column_ref(_measure_dim_relation(measure, order_dim, entities), order_dim.column)
    row_grain_columns = _snapshot_series_columns(
        measure, measure_plan.bound_measure.temporal_role, config
    )
    series = [f"__row_key_{_slug(column, fallback='item')}" for column in row_grain_columns]

    fields_by_alias: dict[str, Any] = {}
    if time_alias and time_expr is not None:
        fields_by_alias[time_alias] = time_expr
    for column, alias in zip(row_grain_columns, series, strict=True):
        fields_by_alias[alias] = _column_ref(source_table, column)
    for column in _source_join_columns_for_paths(
        measure.entity, measure_plan.path_selections, config
    ):
        fields_by_alias[column] = _column_ref(source_table, column)

    for predicate_payload in [*anchored.base_predicates, *anchored.extra_predicates]:
        predicate = _predicate_expr_from_payload(predicate_payload)
        entity_cfg = entities[predicate.entity]
        for key_col, dim_id in zip(
            entity_cfg.key,
            _entity_key_dimension_ids(predicate.entity, config),
            strict=True,
        ):
            key_expr = _direct_entity_key_source_expr(
                measure.entity, predicate.entity, key_col, config
            )
            if key_expr is not None:
                fields_by_alias[dim_id] = key_expr

    for dim_id in plan.group_by:
        direct = _direct_dimension_source_expr(measure.entity, dim_id, config)
        if direct is not None:
            fields_by_alias[dim_id] = direct[0]

    fields_by_alias["__anchor_value"] = _config_expr_to_sql(measure.expr, measure, config)
    fields = [SqlField(expr, alias) for alias, expr in fields_by_alias.items()]
    return fields, series, order_expr, raw_time_expr, time_alias, leaf_time_role


def _anchored_snapshot_ctes(
    plan: LogicalPlan,
    anchored: AnchoredEntitySetPlan,
    config: PackageConfig,
) -> tuple[list[SqlCte], str, str, list[Any]]:
    """The chosen snapshots, their CTE and time alias, and the condition that keeps one."""
    entities = _entity_index(config)
    bound = anchored.denominator_measure_plan.bound_measure
    measure = _measure_index(config)[bound.measure_id]
    source_table = _measure_owned_relation(measure, entities)
    # One snapshot is chosen per series and time bucket only, so a grouped clock, calendar
    # or other date would be read from that one snapshot, dropping the bucket's other periods.
    for dim_id in plan.group_by:
        period = _stock_period_split(bound.temporal_role, dim_id, config)
        if period or _dimension_index(config)[dim_id].data_type in {"date", "timestamp"}:
            raise SemanticLayerError(
                "REWRITE_NOT_SUPPORTED",
                "An entity-set ratio can't be grouped by a date, time or calendar dimension. "
                "Use the query's time grain instead.",
                details={"reason": "entity_set_ratio_grouped_by_period", "dimension": dim_id},
            )
    fields, series, order_expr, raw_time_expr, time_alias, leaf_time_role = _snapshot_select_fields(
        plan, anchored, config
    )
    filters = [
        *refuse_child_groups(plan.query.get("where"), "in an entity-set ratio"),
        *_bound_filter_clauses(bound, config),
    ]
    # As in a stock's own leaf (_semi_additive_leaf_select), a filter on an attribute keeps
    # or drops the chosen snapshot; only one bounding time applies before the choice.
    chosen = [
        item
        for item in filters
        if _reads_chosen_snapshot(measure, bound.temporal_role, str(item["field"]), config)
    ]
    where_clauses: list[Any] = (
        _source_local_filter_conditions(
            measure.entity, [item for item in filters if item not in chosen], config
        )
        or []
    )
    kept_rows: list[Any] = []
    if chosen_conditions := _source_local_filter_conditions(measure.entity, chosen, config):
        kept = SqlCase(
            [SqlCaseWhen(_and_conditions(chosen_conditions), SqlLiteral(1))], SqlLiteral(0)
        )
        fields = [*fields, SqlField(kept, "__snapshot_kept")]
        kept_rows = [SqlBinary(SqlIdentifier(["snapshot", "__snapshot_kept"]), "=", SqlLiteral(1))]
    if plan.time and raw_time_expr is not None:
        where_clauses.extend(
            _source_time_window(raw_time_expr, plan.time, config, role_id=leaf_time_role)
        )

    snapshot_name = f"latest_{_slug(_last_token(measure.entity).replace('entity_', ''), fallback='entity')}_snapshot"
    window_choice = anchored.denominator_measure_plan.bound_measure.aggregation
    if window_choice not in {"first_value", "last_value"}:
        window_choice = (
            "first_value" if measure.accumulation.snapshot == "start_of_period" else "last_value"
        )
    # Chosen as a stock's own leaf chooses them (snapshot_reads): each series' last (or first)
    # snapshot per period, which no entity scope changes. Never on a closing day: a share of
    # a balance that closes on one is refused (_anchored_entity_set_plan).
    read = SnapshotRead(
        fields=fields,
        periods=[time_alias] if time_alias in {item.alias for item in fields} else [],
        series=series,
        order=order_expr,
        day=None,
        first=window_choice == "first_value",
        source=SqlTableRef(name=source_table),
        where=where_clauses,
    )
    ctes, snapshots = select_snapshots(
        read, name=snapshot_name, prefix=snapshot_name, dialect=_dialect(config), measure_id=""
    )
    return ctes, snapshots, time_alias, kept_rows


def _dimension_expr_from_anchor(
    dim_id: str, *, anchor_name: str, measure_entity: str, config: PackageConfig
) -> Any:
    direct = _direct_dimension_source_expr(measure_entity, dim_id, config)
    if direct is not None:
        return SqlIdentifier(parts=[anchor_name, dim_id])
    return _resolve_dimension_expr(dim_id, config)[0]


def _anchored_entity_set_select(plan: LogicalPlan, config: PackageConfig) -> SqlSelect | None:
    anchored = _anchored_entity_set_plan(plan, config)
    if anchored is None:
        return None
    measure_plan = anchored.denominator_measure_plan
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]

    base_predicate_count = len(anchored.base_predicates)
    predicate_sets = []
    for index, predicate in enumerate([*anchored.base_predicates, *anchored.extra_predicates]):
        owners = [_expression_alias(anchored.numerator, config)]
        if index < base_predicate_count:
            owners.append(_expression_alias(anchored.denominator, config))
        with leaf_predicate_roles(*owners):
            predicate_sets.append(
                _minimal_predicate_set_ctes(predicate, index=index, plan=plan, config=config)
            )
    base_predicate_sets = predicate_sets[:base_predicate_count]
    extra_predicate_sets = predicate_sets[base_predicate_count:]

    snapshot_ctes, snapshot_name, time_alias, kept_rows = _anchored_snapshot_ctes(
        plan, anchored, config
    )
    if project_is_cut():
        # This path returns before the normal projection loop. Both operands
        # consume the snapshot value; their predicates record separate cuts.
        with binding_cut():
            record_ids(anchored.projection_objects)
            record_leaf_reference(measure_plan.bound_measure.alias)
    anchor_name = f"{_slug(_last_token(anchored.anchor_entity).replace('entity_', ''), fallback='entity')}_entity_set"
    anchor_select_fields: dict[str, Any] = {}
    for predicate_set in predicate_sets:
        for key_alias in predicate_set.key_aliases:
            anchor_select_fields[key_alias] = SqlIdentifier(parts=["snapshot", key_alias])
    for dim_id in plan.group_by:
        anchor_select_fields[dim_id] = _dimension_expr_from_anchor(
            dim_id, anchor_name="snapshot", measure_entity=measure.entity, config=config
        )
    if time_alias:
        anchor_select_fields[time_alias] = SqlIdentifier(parts=["snapshot", time_alias])
    anchor_select_fields["__anchor_value"] = SqlIdentifier(parts=["snapshot", "__anchor_value"])
    # Keep observed periods when every chosen snapshot fails an attribute filter, just as
    # the stock leaf does. Base metric predicates still select the series measured first.
    # Grouped only by periods means ungrouped here: _anchored_snapshot_ctes refuses the rest.
    settles = (
        bool(kept_rows)
        and measure.additive
        and measure_plan.bound_measure.aggregation == "sum"
        and observation_scope(plan.query, config) == "dataset"
        and not plan.group_by
    )
    if settles:
        anchor_select_fields["__snapshot_kept"] = SqlIdentifier(
            parts=["snapshot", "__snapshot_kept"]
        )
        anchor_select_fields["__anchor_value"] = SqlCase(
            [SqlCaseWhen(_and_conditions(kept_rows), anchor_select_fields["__anchor_value"])]
        )

    joins = _joins_for_paths(
        measure.entity,
        measure_plan.path_selections,
        config,
        measure_entity=measure.entity,
        time_spec=plan.time,
        table_overrides={measure.entity: "snapshot"},
    )
    anchor_where: list[Any] = [] if settles else list(kept_rows)
    for predicate_set in base_predicate_sets:
        join_condition = _join_condition(
            predicate_set.key_aliases
            + ([predicate_set.time_alias] if predicate_set.time_alias else []),
            "snapshot",
            predicate_set.set_name,
            dialect=_dialect(config),
        )
        joins.append(
            SqlJoin(
                join_type="INNER", table=SqlTableRef(name=predicate_set.set_name), on=join_condition
            )
        )

    anchor_cte = SqlCte(
        name=anchor_name,
        query=SqlSelect(
            select=[SqlField(expr, alias) for alias, expr in anchor_select_fields.items()],
            from_table=SqlTableRef(name=snapshot_name, alias="snapshot"),
            joins=joins,
            where=anchor_where,
        ),
    )

    final_joins: list[SqlJoin] = []
    numerator_conditions: list[Any] = []
    for predicate_set in extra_predicate_sets:
        keys = predicate_set.key_aliases + (
            [predicate_set.time_alias] if predicate_set.time_alias else []
        )
        final_joins.append(
            SqlJoin(
                join_type="LEFT",
                table=SqlTableRef(name=predicate_set.set_name),
                on=_join_condition(
                    keys, "anchor", predicate_set.set_name, dialect=_dialect(config)
                ),
            )
        )
        numerator_conditions.append(
            SqlBinary(
                SqlIdentifier(parts=[predicate_set.set_name, predicate_set.key_aliases[0]]),
                "IS NOT",
                SqlLiteral(None),
            )
        )
    numerator_condition = (
        _and_conditions(numerator_conditions) if numerator_conditions else SqlLiteral(True)
    )
    value_expr = SqlIdentifier(parts=["anchor", "__anchor_value"])
    aggregation = str(measure_plan.bound_measure.aggregation or "").lower()
    if aggregation == "count_distinct":
        numerator_expr = SqlCall(
            "COUNT",
            [
                SqlCase(
                    whens=[SqlCaseWhen(condition=numerator_condition, result=value_expr)],
                    else_expr=SqlLiteral(None),
                )
            ],
            distinct=True,
        )
        denominator_expr = SqlCall("COUNT", [value_expr], distinct=True)
    else:
        numerator_expr = SqlCall(
            "SUM",
            [
                SqlCase(
                    whens=[SqlCaseWhen(condition=numerator_condition, result=value_expr)],
                    else_expr=SqlLiteral(0),
                )
            ],
        )
        denominator_expr = SqlCall("SUM", [value_expr])
    ratio_expr: SqlExpr = SqlBinary(
        numerator_expr, "/", SqlCall("NULLIF", [denominator_expr, SqlLiteral(0)])
    )
    if settles:
        none_kept = SqlBinary(
            SqlCall("MAX", [SqlIdentifier(parts=["anchor", "__snapshot_kept"])]),
            "=",
            SqlLiteral(0),
        )
        ratio_expr = SqlCase([SqlCaseWhen(none_kept, SqlLiteral(0))], ratio_expr)

    key_aliases = _query_key_aliases(plan)
    final_fields = [
        SqlField(SqlIdentifier(parts=["anchor", alias]), alias) for alias in key_aliases
    ]
    final_fields.append(SqlField(ratio_expr, anchored.output_alias))
    group_by: list[SqlExpr] = [SqlIdentifier(parts=["anchor", alias]) for alias in key_aliases]
    order_by = _requested_order(plan, "anchor")
    ctes: list[SqlCte] = []
    for predicate_set in predicate_sets:
        ctes.extend(predicate_set.ctes)
    ctes.extend(snapshot_ctes)
    ctes.append(anchor_cte)
    return SqlSelect(
        ctes=ctes,
        select=final_fields,
        from_table=SqlTableRef(name=anchor_name, alias="anchor"),
        joins=final_joins,
        group_by=group_by,
        order_by=order_by,
        limit=plan.query.get("limit"),
    )
