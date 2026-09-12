from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ..ast import normalize_query
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ColumnRefExpr,
    ComparisonExpr,
    ConversionExpr,
    CumulativeExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RatioExpr,
    RollingExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    expr_to_dict,
    expression_field,
)
from ..fanout import analyze_fanout, choose_path, package_hop_limit
from ..ir import (
    LogicalPlan,
    MeasurePlan,
    PathSelection,
    PerformancePlan,
    PhysicalPlan,
    PhysicalPlanNode,
)
from ..schema import AggregateRelationConfig, PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlOrderTerm,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
    build_filter_condition,
)
from .aliasing import AliasRegistry, alias_select
from .bind import (
    _aggregation_expr,
    _bind_scoped_aggregate,
    _bound_filter_clauses,
    _collect_conversion_exprs,
    _config_expr_to_sql,
    _expression_alias,
    _freeze_payload,
    _parse_public_expr,
)
from .conversion import _conversion_leaf_cte
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
from .post_aggregation import _compile_post_expr, _expr_requires_dense_series, _namespace_sql_select
from .predicate import (
    _all_metric_predicates,
    _predicate_ctes_and_join,
    _predicate_time_alias,
    _predicate_time_spec,
    _scoped_predicate_expr_payload,
)
from .temporal import _time_bound_relationship_ids


def _dialect(config: PackageConfig):
    return dialect_for_warehouse(config.package.warehouse)


def _measure_source_relation(measure, entity) -> str:
    """Return the FROM-clause relation name for a measure.

    Fact-model measures carry their own ``source_relation`` (the fact
    table) because their ``entity`` points at a time entity whose own
    ``.table`` is the calendar relation, not the fact's table. Regular
    measures fall back to the entity's table (current behavior).
    """
    src = getattr(measure, "source_relation", "") or ""
    return src if src else entity.table


def _measure_owned_relation(measure, entities) -> str:
    """Source relation for a column that belongs to a measure's own row.

    For fact measures (with ``source_relation``), columns of the
    measure's bound entity (typically the time entity whose ``.table``
    is the calendar relation) actually live on the fact table. For
    regular measures the bound entity's ``.table`` is correct.
    """
    src = getattr(measure, "source_relation", "") or ""
    return src if src else entities[measure.entity].table


def _aggregate_relation_index(config: PackageConfig) -> dict[str, AggregateRelationConfig]:
    return {row.id: row for row in config.aggregate_relations}


def _selected_aggregate_relation(
    measure_plan: MeasurePlan, config: PackageConfig
) -> AggregateRelationConfig | None:
    relation_id = str(getattr(measure_plan, "aggregate_relation_id", "") or "")
    if not relation_id:
        return None
    return _aggregate_relation_index(config).get(relation_id)


def _measure_dim_relation(measure, dim, entities) -> str:
    """Relation to qualify a dimension's column with, from the
    measure's source perspective.

    When the dimension's entity is the measure's own entity AND the
    measure has a ``source_relation`` (fact table), the column lives
    on the fact table — NOT on the entity's ``.table`` (calendar).
    Otherwise the dimension belongs to a foreign entity and its own
    ``.table`` is correct.
    """
    src = getattr(measure, "source_relation", "") or ""
    if src and dim.entity == measure.entity:
        return src
    return entities[dim.entity].table


def _measure_owned_key_columns(measure, entities, temporal_roles, dimensions) -> list[str]:
    """Columns that uniquely identify a row of the measure's source.

    For fact measures (``source_relation`` set), the row grain is the
    fact's own time column (declared via ``model.time_column`` and
    surfaced through the measure's ``default_temporal_role`` -> its
    bound time dimension's ``column``). The bound entity's
    ``primary_key`` points at the calendar relation's key (e.g.
    ``date_day``) and is NOT a valid column on the fact table.

    For regular measures the entity's ``key`` / ``primary_key`` is
    correct (the row lives on the entity's own table).
    """
    src = getattr(measure, "source_relation", "") or ""
    if src:
        role_id = getattr(measure, "default_temporal_role", "") or ""
        role = temporal_roles.get(role_id) if role_id else None
        if role is not None:
            dim = dimensions.get(getattr(role, "dimension", ""))
            if dim is not None and dim.column:
                return [dim.column]
    entity = entities[measure.entity]
    return list(entity.key or [entity.primary_key])


def _apply_role_timezone(raw_expr: Any, role: Any, config: PackageConfig) -> Any:
    """Wrap a temporal role's raw column expression with a timezone conversion
    when the role's ``column_timezone`` is set and differs from ``timezone``.

    The wrap is applied to ``raw_expr`` before any ``date_trunc`` and before
    use in WHERE filter comparisons, so truncation and filtering happen in
    the target zone.
    """
    column_tz = str(getattr(role, "column_timezone", "") or "").strip()
    target_tz = str(getattr(role, "timezone", "") or "").strip()
    if not column_tz or not target_tz or column_tz == target_tz:
        return raw_expr
    return _dialect(config).convert_timezone(column_tz, target_tz, raw_expr)


def _metric_filter_alias(index: int) -> str:
    return f"metric_filter__{index + 1}"


_ADDITIVE_ZERO_AGGREGATIONS = {"sum", "count", "count_distinct"}

# Measure classes whose value at "no contributing rows" is the additive
# identity (0). Semi-additive (e.g., snapshots) and distinct_population
# classes have different null semantics — leave them alone.
_ZERO_ON_MISSING_MEASURE_CLASSES = {"additive", "event_count", "entity_count", ""}


def _expr_zero_on_missing(expr: SemanticExpr, config: PackageConfig) -> bool:
    """Return True if an expression's natural value is 0 when no rows contribute.

    Used to decide whether NULL produced by a FULL OUTER JOIN combine should be
    coerced to 0 in metric_filter projections, so that the natural translation
    of "orders with no items" (`item_count = 0`) returns the right rows instead
    of silently returning empty.
    """
    if isinstance(expr, (MeasureRefExpr, AggregateExpr)):
        measure = _measure_index(config).get(expr.measure)
        if measure is None:
            return False
        aggregation = (expr.aggregation or measure.default_aggregation or "").lower()
        if aggregation not in _ADDITIVE_ZERO_AGGREGATIONS:
            return False
        return measure.measure_class in _ZERO_ON_MISSING_MEASURE_CLASSES
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            return False
        return _expr_zero_on_missing(recipe.expression, config)
    if isinstance(expr, ArithmeticExpr) and expr.op in {"add", "subtract"}:
        return _expr_zero_on_missing(expr.left, config) and _expr_zero_on_missing(
            expr.right, config
        )
    return False


def _dense_fill_zero_aliases(plan: LogicalPlan, config: PackageConfig) -> set[str]:
    """Measure aliases whose value really is 0 where a dense row has no data.

    Densifying a time series invents rows for periods the data never
    covered. `SUM` over no rows is 0, so filling is right there. `AVG`,
    `MIN`, `MAX` and semi-additive snapshots over no rows are *undefined* —
    filling them with 0 reports a fabricated measurement that is
    indistinguishable from a real one: a minimum below every observed
    value, an average dragged toward zero, an inventory snapshot claiming
    the warehouse was empty.

    Aliases absent from `plan.measure_plans` — conversion expressions —
    are excluded by construction. Those lower to
    `numerator / NULLIF(denominator, 0)`, so a fill would claim a 0%
    conversion rate for a period that simply had no sessions.

    Same predicate as :func:`_expr_zero_on_missing`, which has always
    gated the metric_filter projections.
    """
    measures = _measure_index(config)
    zero_filled: set[str] = set()
    for row in plan.measure_plans:
        bound = row.bound_measure
        measure = measures.get(bound.measure_id)
        if measure is None:
            continue
        aggregation = (bound.aggregation or measure.default_aggregation or "").lower()
        if (
            aggregation in _ADDITIVE_ZERO_AGGREGATIONS
            and measure.measure_class in _ZERO_ON_MISSING_MEASURE_CLASSES
        ):
            zero_filled.add(bound.alias)
    return zero_filled


def _query_key_aliases(plan: LogicalPlan) -> list[str]:
    aliases = list(plan.group_by)
    if plan.time:
        aliases.append(
            plan.time["temporal_role"]
            if not plan.time.get("grain")
            else f"{plan.time['temporal_role']}__{plan.time['grain']}"
        )
    return aliases


def _expr_contains_distribution(expr: SemanticExpr) -> bool:
    if isinstance(expr, DistributionExpr):
        return True
    if isinstance(expr, EntityValueExpr):
        return _expr_contains_distribution(expr.input)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_contains_distribution(expr.left) or _expr_contains_distribution(expr.right)
    if isinstance(expr, (BooleanExpr, CallExpr)):
        return any(_expr_contains_distribution(arg) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _expr_contains_distribution(item.when) or _expr_contains_distribution(item.then)
            for item in expr.whens
        ) or (expr.else_expr is not None and _expr_contains_distribution(expr.else_expr))
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        return _expr_contains_distribution(expr.input)
    return False


def _plan_requires_agent_dag_lowering(plan: LogicalPlan) -> bool:
    for expr_payload in plan.post_aggregation_exprs.values():
        if _expr_contains_distribution(_parse_public_expr(expr_payload)):
            return True
    return False


def _value_filter_condition(value_expr: Any, item: dict[str, Any], *, path: str = "where") -> Any:
    # Single shared op/value → condition mapping; see
    # ``sql_ast.build_filter_condition`` for the NULL / IN semantics.
    return build_filter_condition(value_expr, item.get("op", "="), item.get("value"), path=path)


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


def _slug(value: str, *, fallback: str = "item") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    parts = [part for part in raw.split("_") if part]
    return "_".join(parts) or fallback


def _last_token(value: str) -> str:
    return str(value or "").split(".")[-1]


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
        return expr
    if isinstance(expr, MeasureRefExpr):
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

    if isinstance(expr, MetricPredicateExpr):
        return _expression_scan_specs(expr.input, config)

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
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        return _expression_scan_specs(expr.input, config)

    if isinstance(expr, EntityValueExpr):
        return _expression_scan_specs(expr.input, config)

    if isinstance(expr, DistributionExpr):
        return _expression_scan_specs(expr.over, config)

    return []


def _query_metric_predicates(plan: LogicalPlan) -> list[MetricPredicateExpr]:
    predicates: list[MetricPredicateExpr] = []
    for item in list(plan.query.get("metric_filters", []) or []):
        raw_expr = dict(item.get("expression", {}) or {})
        if not raw_expr:
            continue
        expr = _parse_public_expr(raw_expr)
        if isinstance(expr, MetricPredicateExpr):
            predicates.append(expr)
    return predicates


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
    return _value_filter_condition(expr, item)


def _source_local_filter_conditions(
    measure_entity: str, items: Iterable[dict[str, Any]], config: PackageConfig
) -> list[Any] | None:
    conditions: list[Any] = []
    for item in items:
        dim_id = str(item.get("field", "") or "")
        if (
            _direct_dimension_source_expr(measure_entity, dim_id, config) is None
            and _dimension_index(config)[dim_id].entity != measure_entity
        ):
            return None
        conditions.append(_filter_condition_for_item(measure_entity, item, config))
    return conditions


def _time_alias_for_plan(plan: LogicalPlan) -> str:
    if not plan.time:
        return ""
    return (
        plan.time["temporal_role"]
        if not plan.time.get("grain")
        else f"{plan.time['temporal_role']}__{plan.time['grain']}"
    )


def _measure_time_components(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> tuple[Any | None, Any | None, str, bool]:
    if not plan.time:
        return None, None, "", True
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
    time_expr = (
        _dialect(config).date_trunc(plan.time["grain"], raw_expr)
        if plan.time.get("grain")
        else raw_expr
    )
    return raw_expr, time_expr, _time_alias_for_plan(plan), time_source_local


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
    if _plan_requires_agent_dag_lowering(plan):
        return None
    if len(plan.post_aggregation_exprs) != 1:
        return None
    if plan.query.get("metric_filters") or (plan.time and plan.time.get("fill")):
        return None

    output_alias, expr_payload = next(iter(plan.post_aggregation_exprs.items()))
    expr = _parse_public_expr(expr_payload)
    if not isinstance(expr, RatioExpr):
        return None
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

    raw_time_expr, _, _, time_source_local = _measure_time_components(
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
    )


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
    time_alias = (
        time["temporal_role"]
        if not time.get("grain")
        else f"{time['temporal_role']}__{time['grain']}"
    )
    return raw_expr, time_expr, time_alias


def _aggregate_relation_filter_expr(
    aggregate: AggregateRelationConfig, item: dict[str, Any], *, path: str = "where"
) -> Any:
    dim_id = str(item.get("field", "") or "")
    column = aggregate.dimension_columns.get(dim_id)
    if not column:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Aggregate relation '{aggregate.id}' cannot satisfy filter dimension '{dim_id}'",
            details={"aggregate_relation": aggregate.id, "dimension": dim_id},
        )
    return _value_filter_condition(_column_ref(aggregate.relation, column), item, path=path)


def _aggregate_relation_leaf_select(
    plan: LogicalPlan,
    measure_plans: list[MeasurePlan],
    aggregate: AggregateRelationConfig,
    config: PackageConfig,
) -> SqlSelect:
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
    for item in list(plan.query.get("where", []) or []):
        where_clauses.append(_aggregate_relation_filter_expr(aggregate, dict(item)))
    if raw_time_expr is not None and plan.time:
        time = dict(plan.time)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_time_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_time_expr, "<", SqlLiteral(time["end"])))

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
                _aggregate_relation_filter_expr(aggregate, dict(item), path="measure_filter")
            )
        aggregation = str(aggregate.measure_aggregations.get(measure_id, "") or "sum")
        select_fields.append(
            SqlField(
                _aggregation_expr(
                    _column_ref(aggregate.relation, column),
                    aggregation,
                    parameters=measure_plan.bound_measure.aggregation_params,
                    dialect=_dialect(config),
                ),
                measure_plan.bound_measure.alias,
            )
        )

    return SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=aggregate.relation),
        where=where_clauses,
        group_by=group_fields,
    )


def _branch_context_query(
    plan: LogicalPlan,
    expression: dict[str, Any],
    alias: str,
    *,
    extra_group_by: list[str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "version": int(plan.query.get("version", 2) or 2),
        "select": [{"expression": expression, "as": alias}],
        "group_by": [*list(plan.group_by), *list(extra_group_by or [])],
    }
    if plan.time:
        query["time"] = dict(plan.time)
    if plan.query.get("where"):
        query["where"] = list(plan.query.get("where") or [])
    if plan.query.get("metric_filters"):
        query["metric_filters"] = list(plan.query.get("metric_filters") or [])
    if plan.query.get("temporal_role_overrides"):
        query["temporal_role_overrides"] = dict(plan.query.get("temporal_role_overrides") or {})
    if plan.query.get("path_policy"):
        query["path_policy"] = dict(plan.query.get("path_policy") or {})
    return query


def _distribution_select(
    expr: DistributionExpr, *, alias: str, plan: LogicalPlan, config: PackageConfig
) -> SqlSelect:
    from ..compiler import compile_query

    entity_key_dims = _entity_key_dimension_ids(expr.over.entity, config)
    value_alias = "__entity_value"
    entity_value_query = _branch_context_query(
        plan,
        expr_to_dict(expr.over.input),
        value_alias,
        extra_group_by=entity_key_dims,
    )
    compiled = compile_query(config, None, entity_value_query)
    source_name = f"{alias}__entity_values"
    key_aliases = _query_key_aliases(plan)
    value_ref = SqlIdentifier(parts=[source_name, value_alias])
    function = str(expr.function or "").strip().lower()
    parameters = dict(expr.parameters or {})
    if function == "percentile":
        if expr.p is not None:
            parameters.setdefault("p", expr.p)
        aggregate_expr = _aggregation_expr(
            value_ref,
            "percentile",
            parameters=parameters,
            dialect=dialect_for_warehouse(config.package.warehouse),
        )
    elif function in {"median", "avg", "sum", "min", "max"}:
        aggregate_expr = _aggregation_expr(
            value_ref,
            function,
            parameters=parameters,
            dialect=dialect_for_warehouse(config.package.warehouse),
        )
    else:
        raise SemanticLayerError(
            "UNSUPPORTED_AGGREGATION", f"Unsupported distribution function '{expr.function}'"
        )
    return SqlSelect(
        ctes=[
            SqlCte(
                name=source_name,
                query=_namespace_sql_select(compiled["sql_ast"], f"{source_name}__"),
            )
        ],
        select=[
            *[SqlField(SqlIdentifier(parts=[source_name, key]), key) for key in key_aliases],
            SqlField(aggregate_expr, alias),
        ],
        from_table=SqlTableRef(name=source_name),
        where=[
            _value_filter_condition(value_ref, item)
            for item in list(expr.over.where or [])
            if str(item.get("kind", "value_filter")) == "value_filter"
        ],
        group_by=[SqlIdentifier(parts=[source_name, key]) for key in key_aliases],
    )


def _single_expression_branch_select(
    expr: SemanticExpr, *, alias: str, plan: LogicalPlan, config: PackageConfig
) -> SqlSelect:
    from ..compiler import compile_query

    compiled = compile_query(config, None, _branch_context_query(plan, expr_to_dict(expr), alias))
    return compiled["sql_ast"]


def _lower_agent_dag_to_sql(plan: LogicalPlan, config: PackageConfig) -> SqlSelect:
    key_aliases = _query_key_aliases(plan)
    branch_ctes: list[SqlCte] = []
    output_aliases: list[str] = []
    for index, (alias, expr_payload) in enumerate(plan.post_aggregation_exprs.items(), start=1):
        expr = _parse_public_expr(expr_payload)
        branch_name = f"agent_branch_{index}"
        branch_query = (
            _distribution_select(expr, alias=alias, plan=plan, config=config)
            if isinstance(expr, DistributionExpr)
            else _single_expression_branch_select(expr, alias=alias, plan=plan, config=config)
        )
        branch_ctes.append(
            SqlCte(name=branch_name, query=_namespace_sql_select(branch_query, f"{branch_name}__"))
        )
        output_aliases.append(alias)

    if not branch_ctes:
        raise SemanticLayerError(
            "INVALID_QUERY", "Agent DAG lowering requires at least one projected expression"
        )

    combined_name = branch_ctes[0].name
    combine_ctes: list[SqlCte] = []
    available_aliases = [output_aliases[0]]
    for index, branch in enumerate(branch_ctes[1:], start=2):
        left_alias = "left_side"
        right_alias = "right_side"
        next_name = f"agent_combined_{index}"
        select_fields = [
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
            for key in key_aliases
        ]
        select_fields.extend(
            SqlField(SqlIdentifier(parts=[left_alias, item]), item) for item in available_aliases
        )
        select_fields.append(
            SqlField(
                SqlIdentifier(parts=[right_alias, output_aliases[index - 1]]),
                output_aliases[index - 1],
            )
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
                            table=SqlTableRef(name=branch.name, alias=right_alias),
                            on=_join_condition(
                                key_aliases, left_alias, right_alias, dialect=_dialect(config)
                            ),
                        )
                    ],
                ),
            )
        )
        combined_name = next_name
        available_aliases.append(output_aliases[index - 1])

    final_source = "agent_projected"
    projected = SqlSelect(
        ctes=[*branch_ctes, *combine_ctes],
        select=[
            *[SqlField(SqlIdentifier(parts=["base", key]), key) for key in key_aliases],
            *[SqlField(SqlIdentifier(parts=["base", alias]), alias) for alias in output_aliases],
        ],
        from_table=SqlTableRef(name=combined_name, alias="base"),
    )

    def _final_order_field(field: str) -> str:
        if field == "time":
            if not plan.time:
                raise SemanticLayerError(
                    "INVALID_ORDER_BY", "'time' order_by requires a query time axis"
                )
            return key_aliases[-1]
        return field

    return SqlSelect(
        ctes=[SqlCte(name=final_source, query=projected)],
        select=[
            *[SqlField(SqlIdentifier(parts=[final_source, key]), key) for key in key_aliases],
            *[
                SqlField(SqlIdentifier(parts=[final_source, alias]), alias)
                for alias in output_aliases
            ],
        ],
        from_table=SqlTableRef(name=final_source),
        order_by=[
            SqlOrder(
                SqlIdentifier(
                    parts=[
                        final_source,
                        _final_order_field(
                            str(expression_field(item, "field", expression_position="order_by"))
                        ),
                    ]
                ),
                str(item.get("direction", "ASC")) if isinstance(item, dict) else "ASC",
            )
            for item in list(plan.query.get("order_by", []) or [])
        ],
        limit=plan.query.get("limit"),
    )


def _and_conditions(conditions: list[Any]) -> Any:
    current = conditions[0]
    for condition in conditions[1:]:
        current = SqlBinary(current, "AND", condition)
    return current


def _semi_additive_leaf_select(
    *,
    key_fields: list[SqlField],
    row_grain_exprs: list[Any],
    order_expr: Any,
    value_expr: Any,
    leaf_alias: str,
    from_table: SqlTableRef,
    joins: list[SqlJoin],
    where_clauses: list[Any],
    window_choice: str,
    final_aggregation: str,
    aggregation_params: dict[str, Any],
    warehouse: str,
    ctes: list[SqlCte] | None = None,
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
    order_function = "MIN" if window_choice == "first_value" else "MAX"

    base_fields = [
        *key_fields,
        *[
            SqlField(expr, alias)
            for expr, alias in zip(row_grain_exprs, row_key_aliases, strict=True)
        ],
        SqlField(order_expr, "__snapshot_order"),
        SqlField(value_expr, "__snapshot_value"),
    ]
    complete_fields = [
        *[
            SqlField(SqlIdentifier(parts=["snapshot_base", alias]), alias)
            for alias in [*key_aliases, *row_key_aliases]
        ],
        SqlField(
            SqlCall(order_function, [SqlIdentifier(parts=["snapshot_base", "__snapshot_order"])]),
            "__snapshot_order",
        ),
    ]
    complete_group_by: list[SqlExpr] = [
        SqlIdentifier(parts=["snapshot_base", alias]) for alias in [*key_aliases, *row_key_aliases]
    ]

    dialect = dialect_for_warehouse(warehouse)
    if dialect.capabilities().get("qualify"):
        direction = "ASC" if window_choice == "first_value" else "DESC"
        snapshot_name = "snapshot_rows"
        row_number = SqlWindow(
            function=SqlCall("ROW_NUMBER", []),
            partition_by=[field.expression for field in key_fields] + list(row_grain_exprs),
            order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
        )
        snapshot_value = SqlIdentifier(parts=[snapshot_name, "__snapshot_value"])
        final_expr = (
            SqlCall("SUM", [snapshot_value])
            if final_aggregation in {"last_value", "first_value"}
            else _aggregation_expr(
                snapshot_value,
                final_aggregation,
                parameters=aggregation_params,
                dialect=dialect,
            )
        )
        return SqlSelect(
            ctes=[
                *list(ctes or []),
                SqlCte(
                    name=snapshot_name,
                    query=SqlSelect(
                        select=base_fields,
                        from_table=from_table,
                        joins=joins,
                        where=where_clauses,
                        qualify=[SqlBinary(row_number, "=", SqlLiteral(1))],
                    ),
                ),
            ],
            select=[
                *[
                    SqlField(SqlIdentifier(parts=[snapshot_name, alias]), alias)
                    for alias in key_aliases
                ],
                SqlField(final_expr, leaf_alias),
            ],
            from_table=SqlTableRef(name=snapshot_name),
            group_by=[SqlIdentifier(parts=[snapshot_name, alias]) for alias in key_aliases],
        )

    join_conditions = [
        dialect.null_safe_eq(
            SqlIdentifier(parts=["snapshot_base", alias]),
            SqlIdentifier(parts=["snapshot_complete", alias]),
        )
        for alias in [*key_aliases, *row_key_aliases]
    ]
    join_conditions.append(
        SqlBinary(
            SqlIdentifier(parts=["snapshot_base", "__snapshot_order"]),
            "=",
            SqlIdentifier(parts=["snapshot_complete", "__snapshot_order"]),
        )
    )
    final_key_exprs: list[SqlExpr] = [
        SqlIdentifier(parts=["snapshot_base", alias]) for alias in key_aliases
    ]
    snapshot_value = SqlIdentifier(parts=["snapshot_base", "__snapshot_value"])
    final_expr = (
        SqlCall("SUM", [snapshot_value])
        if final_aggregation in {"last_value", "first_value"}
        else _aggregation_expr(
            snapshot_value,
            final_aggregation,
            parameters=aggregation_params,
            dialect=dialect,
        )
    )
    return SqlSelect(
        ctes=[
            *list(ctes or []),
            SqlCte(
                name="snapshot_base",
                query=SqlSelect(
                    select=base_fields,
                    from_table=from_table,
                    joins=joins,
                    where=where_clauses,
                ),
            ),
            SqlCte(
                name="snapshot_complete",
                query=SqlSelect(
                    select=complete_fields,
                    from_table=SqlTableRef(name="snapshot_base"),
                    group_by=complete_group_by,
                ),
            ),
        ],
        select=[
            *[
                SqlField(expr, alias)
                for expr, alias in zip(final_key_exprs, key_aliases, strict=True)
            ],
            SqlField(final_expr, leaf_alias),
        ],
        from_table=SqlTableRef(name="snapshot_base"),
        joins=[
            SqlJoin(
                join_type="INNER",
                table=SqlTableRef(name="snapshot_complete"),
                on=_and_conditions(join_conditions),
            )
        ],
        group_by=final_key_exprs,
    )


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


def _filter_dimensions_are_source_local(
    measure_entity: str, items: Iterable[dict[str, Any]], config: PackageConfig
) -> bool:
    dimensions = _dimension_index(config)
    for item in items:
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


def _preferred_path(
    config: PackageConfig, *, start: str, target: str, preference: str
) -> tuple[list[str], list[list[str]]]:
    explicit = get_package_analysis(config).path_preferences.get((start, target))
    if explicit is not None:
        return list(explicit), [list(explicit)]
    return choose_path(
        config,
        start=start,
        target=target,
        hop_limit=package_hop_limit(config),
        preference=preference,
    )


def _anchor_path_selection(
    *,
    config: PackageConfig,
    plan: LogicalPlan,
    start_entity: str,
    target_entity: str,
    purpose: str,
) -> PathSelection | None:
    if start_entity == target_entity:
        return None
    query = normalize_query(plan.query)
    chosen, candidates = _preferred_path(
        config, start=start_entity, target=target_entity, preference=query.path_policy.preference
    )
    analysis = analyze_fanout(
        config,
        start_entity,
        chosen,
        time_bound_relationships=_time_bound_relationship_ids(query, config),
    )
    if analysis.get("status") != "ok":
        return None
    return PathSelection(
        target_entity=target_entity,
        purpose=purpose,
        chosen_path=list(chosen),
        candidate_paths=[list(path) for path in candidates],
        analysis=analysis,
    )


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
    measure_key_columns = list(
        entities[measure.entity].key or [entities[measure.entity].primary_key]
    )
    if measure.expr.column not in set(measure_key_columns):
        return None
    if _all_metric_predicates(plan, measure_plan):
        return None

    anchor_entity = ""
    anchor_key_columns: list[str] = []
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
                remaining_path = list(selection.chosen_path[index + 1 :])
            current_entity = rel.source_entity
        if not selected_anchor:
            return None
        if anchor_entity and selected_anchor != anchor_entity:
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
    required_dimension_ids = list(plan.group_by)
    required_dimension_ids.extend(
        str(item.get("field"))
        for item in list(plan.query.get("where", []) or [])
        if item.get("field")
    )
    required_dimension_ids.extend(
        str(item.get("field"))
        for item in _bound_filter_clauses(measure_plan.bound_measure, config)
        if item.get("field")
    )
    if plan.time:
        role = _temporal_role_index(config)[
            _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config)
        ]
        required_dimension_ids.append(role.dimension)

    covered_targets = {selection.target_entity for selection in transformed_selections}
    for dim_id in dict.fromkeys(required_dimension_ids):
        dim = dimensions.get(dim_id)
        if dim is None:
            return None
        if (
            dim.entity == anchor_entity
            or _direct_dimension_source_expr(anchor_entity, dim_id, config) is not None
        ):
            continue
        if dim.entity in covered_targets:
            continue
        new_selection = _anchor_path_selection(
            config=config,
            plan=plan,
            start_entity=anchor_entity,
            target_entity=dim.entity,
            purpose="entity_in_terms_of_lookup",
        )
        if new_selection is None:
            return None
        supplemental_selections.append(new_selection)
        covered_targets.add(dim.entity)
    return {
        "anchor_entity": anchor_entity,
        "anchor_key_columns": anchor_key_columns,
        "path_selections": [*supplemental_selections, *transformed_selections],
        "rewritten_path_selections": transformed_selections,
        "supplemental_path_selections": supplemental_selections,
        "omitted_root_entity": measure.entity,
    }


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
        time_expr = (
            _dialect(config).date_trunc(time["grain"], raw_expr) if time.get("grain") else raw_expr
        )
        time_alias = (
            time["temporal_role"]
            if not time.get("grain")
            else f"{time['temporal_role']}__{time['grain']}"
        )
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_expr, "<", SqlLiteral(time["end"])))
    for item in list(plan.query.get("where", []) or []):
        expr, _ = _direct_dimension_source_expr(
            anchor_entity, str(item["field"]), config
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        expr, _ = _direct_dimension_source_expr(
            anchor_entity, str(item["field"]), config
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
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
        joins=_joins_for_paths(anchor_entity, path_selections, config, time_spec=plan.time),
        where=where_clauses,
        group_by=group_fields,
    )


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

    source_table = _measure_owned_relation(measure, entities)
    rollup_name = f"{measure_plan.cte_name}_source_rollup"
    preagg_fields: dict[str, Any] = {
        column: _column_ref(source_table, column) for column in source_join_columns
    }
    local_group_aliases: list[str] = []
    measure_source_override = getattr(measure, "source_relation", "") or ""
    for dim_id in plan.group_by:
        dim = dimensions[dim_id]
        direct = _direct_dimension_source_expr(
            measure.entity,
            dim_id,
            config,
            source_relation_override=measure_source_override,
        )
        if dim.entity == measure.entity:
            preagg_fields[dim_id] = _column_ref(source_table, dim.column)
            local_group_aliases.append(dim_id)
        elif direct is not None:
            preagg_fields[dim_id] = direct[0]
            local_group_aliases.append(dim_id)
    if time_alias and time_expr is not None:
        preagg_fields[time_alias] = time_expr

    preagg_select_fields = [SqlField(expr, alias) for alias, expr in preagg_fields.items()]
    preagg_select_fields.append(
        SqlField(
            _aggregation_expr(
                leaf_value_expr,
                aggregation,
                parameters=measure_plan.bound_measure.aggregation_params,
                dialect=dialect_for_warehouse(config.package.warehouse),
            ),
            "__source_value",
        )
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
        SqlField(SqlCall("SUM", [SqlIdentifier(parts=[rollup_name, "__source_value"])]), leaf_alias)
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

    predicate_ctes: list[SqlCte] = []
    predicate_joins: list[SqlJoin] = []
    for index, predicate in enumerate(_all_metric_predicates(plan, measure_plan)):
        ctes, join = _predicate_ctes_and_join(
            predicate, index=index, plan=plan, measure_plan=measure_plan, config=config
        )
        predicate_ctes.extend(ctes)
        predicate_joins.append(join)

    select_fields: list[SqlField] = []
    group_fields: list[Any] = []
    time_alias = ""
    time_expr: Any | None = None
    time_source_local = True
    leaf_time_role = _leaf_time_role(
        measure_plan.bound_measure, normalize_query(plan.query), config
    )
    measure_source_override = getattr(measure, "source_relation", "") or ""
    for dim_id in plan.group_by:
        expr, alias = _direct_dimension_source_expr(
            measure.entity, dim_id, config, source_relation_override=measure_source_override
        ) or _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)
    leaf_calendar_join: SqlJoin | None = None
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(
            measure.entity,
            role.dimension,
            config,
            source_relation_override=measure_source_override,
        )
        time_source_local = dim.entity == measure.entity or direct_time is not None
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(_measure_dim_relation(measure, dim, entities), dim.column)
        )
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        cal_binding = _leaf_calendar_binding(plan, config)
        if cal_binding is not None and time.get("grain"):
            cal_table, cal_grain_column, cal_join_column = cal_binding
            time_expr = _column_ref(cal_table, cal_grain_column)
            leaf_calendar_join = _calendar_join_for_leaf(raw_expr, cal_table, cal_join_column)
        else:
            time_expr = (
                _dialect(config).date_trunc(time["grain"], raw_expr)
                if time.get("grain")
                else raw_expr
            )
        time_alias = (
            time["temporal_role"]
            if not time.get("grain")
            else f"{time['temporal_role']}__{time['grain']}"
        )
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    for item in list(query.get("where", []) or []):
        expr, _ = _direct_dimension_source_expr(
            measure.entity,
            str(item["field"]),
            config,
            source_relation_override=measure_source_override,
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        expr, _ = _direct_dimension_source_expr(
            measure.entity,
            str(item["field"]),
            config,
            source_relation_override=measure_source_override,
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(
            measure.entity,
            role.dimension,
            config,
            source_relation_override=measure_source_override,
        )
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(_measure_dim_relation(measure, dim, entities), dim.column)
        )
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_expr, "<", SqlLiteral(time["end"])))

    order_expr = None
    if measure_plan.bound_measure.temporal_role:
        role = temporal_roles[measure_plan.bound_measure.temporal_role]
        time_dim = dimensions[role.dimension]
        order_expr = _column_ref(
            _measure_dim_relation(measure, time_dim, entities), time_dim.column
        )

    leaf_alias = measure_plan.bound_measure.alias
    leaf_value_expr = _config_expr_to_sql(measure.expr, measure, config)
    entity_in_terms_of = _entity_in_terms_of_leaf_select(plan, measure_plan, config)
    if entity_in_terms_of is not None:
        return entity_in_terms_of
    source_rollup = _source_rollup_leaf_select(
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
    if source_rollup is not None:
        return source_rollup
    joins = [
        *_joins_for_paths(
            measure.entity, measure_plan.path_selections, config, time_spec=plan.time
        ),
        *predicate_joins,
    ]
    if leaf_calendar_join is not None:
        joins.append(leaf_calendar_join)
    if measure.measure_class == "semi_additive":
        if order_expr is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE",
                f"{measure_plan.bound_measure.aggregation} requires an ordering temporal role",
            )
        order_column = ""
        if measure_plan.bound_measure.temporal_role:
            order_role = temporal_roles[measure_plan.bound_measure.temporal_role]
            order_column = dimensions[order_role.dimension].column
        declared_columns = [column for column in list(measure.row_grain or []) if column]
        fallback_columns = _measure_owned_key_columns(measure, entities, temporal_roles, dimensions)
        # The ordering time column is never part of the snapshot row key:
        # partitioning "last snapshot per key" by the snapshot's own time
        # makes every snapshot its own group, so the final combine sums a
        # stock across periods. An empty result means the series is keyed
        # by time alone (one snapshot per day) — a valid singleton series.
        row_grain_columns = [column for column in declared_columns if column != order_column]
        if not row_grain_columns:
            row_grain_columns = [column for column in fallback_columns if column != order_column]
        row_grain_exprs = [
            _column_ref(_measure_owned_relation(measure, entities), column)
            for column in row_grain_columns
            if column
        ]
        if not row_grain_exprs and not declared_columns and not fallback_columns:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"Semi-additive measure '{measure.id}' requires a row grain"
            )
        window_choice = measure_plan.bound_measure.aggregation
        if window_choice not in {"first_value", "last_value"}:
            window_choice = (
                "first_value"
                if measure.accumulation.snapshot == "start_of_period"
                else "last_value"
            )
        return _semi_additive_leaf_select(
            key_fields=list(select_fields),
            row_grain_exprs=row_grain_exprs,
            order_expr=order_expr,
            value_expr=leaf_value_expr,
            leaf_alias=leaf_alias,
            from_table=SqlTableRef(
                name=_measure_source_relation(measure, entities[measure.entity])
            ),
            joins=joins,
            where_clauses=where_clauses,
            window_choice=window_choice,
            final_aggregation=measure_plan.bound_measure.aggregation,
            aggregation_params=dict(measure_plan.bound_measure.aggregation_params),
            warehouse=config.package.warehouse,
            ctes=predicate_ctes,
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
    return SqlSelect(
        ctes=predicate_ctes,
        select=select_fields,
        from_table=SqlTableRef(name=_measure_source_relation(measure, entities[measure.entity])),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )


def _minimal_predicate_set_ctes(
    predicate_payload: dict[str, Any],
    *,
    index: int,
    plan: LogicalPlan,
    config: PackageConfig,
) -> PredicateSetSql:
    from ..compiler import _compile_query_sql_ast

    query = normalize_query(plan.query)
    predicate = _predicate_expr_from_payload(predicate_payload)
    group_by_dims = _predicate_key_aliases(predicate, config)
    time_spec = _predicate_time_spec(predicate, query, config)
    mini_query: dict[str, Any] = {
        "version": 2,
        "select": [{"expression": expr_to_dict(predicate.input), "as": "__predicate_value"}],
        "group_by": list(group_by_dims),
    }
    time_alias = ""
    source_time_alias = ""
    if time_spec is not None:
        mini_query["time"] = {
            key: value
            for key, value in dict(time_spec).items()
            if key in {"temporal_role", "grain", "start", "end", "fill", "calendar_id"}
        }
        time_alias = _predicate_time_alias(
            str(time_spec.get("output_temporal_role") or time_spec["temporal_role"]),
            str(time_spec["grain"]),
        )
        source_time_alias = _predicate_time_alias(
            str(time_spec["temporal_role"]), str(time_spec["grain"])
        )

    predicate_sql = _compile_query_sql_ast(config, mini_query)
    source_name = f"{_semantic_set_name(predicate, index)}_source"
    set_name = _semantic_set_name(predicate, index)
    source_cte = SqlCte(
        name=source_name, query=_namespace_sql_select(predicate_sql, f"{source_name}__")
    )

    select_fields = [
        SqlField(SqlIdentifier(parts=["predicate_source", dim_id]), dim_id)
        for dim_id in group_by_dims
    ]
    group_keys = list(group_by_dims)
    if time_alias:
        select_fields.append(
            SqlField(SqlIdentifier(parts=["predicate_source", source_time_alias]), time_alias)
        )
        group_keys.append(source_time_alias)
    # Inline-threshold path (see ``_inline_threshold_cte_and_where`` in
    # compiler.py): when ``predicate.value`` is the percentile-expr dict
    # validated at parse time, emit a threshold CTE + CROSS JOIN.
    from ..compiler import _inline_threshold_cte_and_where

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
    _ = group_keys  # retained for naming clarity; no GROUP BY needed
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
) -> tuple[list[SqlField], list[Any], Any, Any, str, list[str]]:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    measure_plan = anchored.denominator_measure_plan
    measure = _measure_index(config)[measure_plan.bound_measure.measure_id]
    source_table = _measure_owned_relation(measure, entities)
    raw_time_expr, time_expr, time_alias, time_source_local = _measure_time_components(
        plan, measure_plan, config
    )
    if plan.time and (raw_time_expr is None or time_expr is None or not time_source_local):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED", "Anchored entity-set optimization requires source-local time"
        )

    order_role = temporal_roles[measure_plan.bound_measure.temporal_role]
    order_dim = dimensions[order_role.dimension]
    order_expr = _column_ref(_measure_dim_relation(measure, order_dim, entities), order_dim.column)
    order_column = order_dim.column
    row_grain_columns = [
        column for column in list(measure.row_grain or []) if column and column != order_column
    ]
    if not row_grain_columns:
        # Exclude the ordering time column from the fallback too: a series
        # keyed by time alone is a singleton snapshot series, and keeping
        # the time column in the partition would defeat the last/first
        # snapshot reduction (see _semi_additive_leaf_select).
        row_grain_columns = [
            column
            for column in _measure_owned_key_columns(measure, entities, temporal_roles, dimensions)
            if column and column != order_column
        ]
    row_grain_exprs = [_column_ref(source_table, column) for column in row_grain_columns]

    fields_by_alias: dict[str, Any] = {}
    if time_alias and time_expr is not None:
        fields_by_alias[time_alias] = time_expr
    for column, expr in zip(row_grain_columns, row_grain_exprs, strict=True):
        fields_by_alias[f"__row_key_{_slug(column)}"] = expr
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
    partition_exprs = []
    if time_expr is not None:
        partition_exprs.append(time_expr)
    partition_exprs.extend(row_grain_exprs)
    partition_aliases = [
        *([time_alias] if time_alias else []),
        *[f"__row_key_{_slug(column)}" for column in row_grain_columns],
    ]
    return fields, partition_exprs, order_expr, raw_time_expr, time_alias, partition_aliases


def _anchored_snapshot_ctes(
    plan: LogicalPlan,
    anchored: AnchoredEntitySetPlan,
    config: PackageConfig,
) -> tuple[list[SqlCte], str, str]:
    entities = _entity_index(config)
    measure = _measure_index(config)[anchored.denominator_measure_plan.bound_measure.measure_id]
    source_table = _measure_owned_relation(measure, entities)
    fields, partition_exprs, order_expr, raw_time_expr, time_alias, partition_aliases = (
        _snapshot_select_fields(plan, anchored, config)
    )
    where_clauses: list[Any] = []
    source_filters = _source_local_filter_conditions(
        measure.entity, list(plan.query.get("where", []) or []), config
    )
    bound_filters = _source_local_filter_conditions(
        measure.entity,
        _bound_filter_clauses(anchored.denominator_measure_plan.bound_measure, config),
        config,
    )
    where_clauses.extend(source_filters or [])
    where_clauses.extend(bound_filters or [])
    if plan.time and raw_time_expr is not None:
        if plan.time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_time_expr, ">=", SqlLiteral(plan.time["start"])))
        if plan.time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_time_expr, "<", SqlLiteral(plan.time["end"])))

    snapshot_name = f"latest_{_slug(_last_token(measure.entity).replace('entity_', ''), fallback='entity')}_snapshot"
    window_choice = anchored.denominator_measure_plan.bound_measure.aggregation
    if window_choice not in {"first_value", "last_value"}:
        window_choice = (
            "first_value" if measure.accumulation.snapshot == "start_of_period" else "last_value"
        )
    direction = "ASC" if window_choice == "first_value" else "DESC"
    dialect = _dialect(config)
    if dialect.capabilities().get("qualify"):
        row_number = SqlWindow(
            function=SqlCall("ROW_NUMBER", []),
            partition_by=partition_exprs,
            order_by=[SqlOrderTerm(expr=order_expr, direction=direction)],
        )
        return (
            [
                SqlCte(
                    name=snapshot_name,
                    query=SqlSelect(
                        select=fields,
                        from_table=SqlTableRef(name=source_table),
                        where=where_clauses,
                        qualify=[SqlBinary(row_number, "=", SqlLiteral(1))],
                    ),
                )
            ],
            snapshot_name,
            time_alias,
        )

    base_name = f"{snapshot_name}_base"
    complete_name = f"{snapshot_name}_complete"
    order_function = "MIN" if window_choice == "first_value" else "MAX"
    base_fields = [*fields, SqlField(order_expr, "__snapshot_order")]
    complete_fields = [
        *[SqlField(SqlIdentifier(parts=[base_name, alias]), alias) for alias in partition_aliases],
        SqlField(
            SqlCall(order_function, [SqlIdentifier(parts=[base_name, "__snapshot_order"])]),
            "__snapshot_order",
        ),
    ]
    complete_group_by: list[SqlExpr] = [
        SqlIdentifier(parts=[base_name, alias]) for alias in partition_aliases
    ]
    join_conditions = [
        dialect.null_safe_eq(
            SqlIdentifier(parts=[base_name, alias]), SqlIdentifier(parts=[complete_name, alias])
        )
        for alias in partition_aliases
    ]
    join_conditions.append(
        SqlBinary(
            SqlIdentifier(parts=[base_name, "__snapshot_order"]),
            "=",
            SqlIdentifier(parts=[complete_name, "__snapshot_order"]),
        )
    )
    return (
        [
            SqlCte(
                name=base_name,
                query=SqlSelect(
                    select=base_fields,
                    from_table=SqlTableRef(name=source_table),
                    where=where_clauses,
                ),
            ),
            SqlCte(
                name=complete_name,
                query=SqlSelect(
                    select=complete_fields,
                    from_table=SqlTableRef(name=base_name),
                    group_by=complete_group_by,
                ),
            ),
            SqlCte(
                name=snapshot_name,
                query=SqlSelect(
                    select=[
                        SqlField(SqlIdentifier(parts=[base_name, field.alias]), field.alias)
                        for field in fields
                    ],
                    from_table=SqlTableRef(name=base_name),
                    joins=[
                        SqlJoin(
                            join_type="INNER",
                            table=SqlTableRef(name=complete_name),
                            on=_and_conditions(join_conditions),
                        )
                    ],
                ),
            ),
        ],
        snapshot_name,
        time_alias,
    )


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

    predicate_sets = [
        _minimal_predicate_set_ctes(predicate, index=index, plan=plan, config=config)
        for index, predicate in enumerate([*anchored.base_predicates, *anchored.extra_predicates])
    ]
    base_predicate_count = len(anchored.base_predicates)
    base_predicate_sets = predicate_sets[:base_predicate_count]
    extra_predicate_sets = predicate_sets[base_predicate_count:]

    snapshot_ctes, snapshot_name, time_alias = _anchored_snapshot_ctes(plan, anchored, config)
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

    joins = _joins_for_paths(
        measure.entity,
        measure_plan.path_selections,
        config,
        time_spec=plan.time,
        table_overrides={measure.entity: "snapshot"},
    )
    anchor_where: list[Any] = []
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
    ratio_expr = SqlBinary(
        numerator_expr, "/", SqlCall("NULLIF", [denominator_expr, SqlLiteral(0)])
    )

    key_aliases = _query_key_aliases(plan)
    final_fields = [
        SqlField(SqlIdentifier(parts=["anchor", alias]), alias) for alias in key_aliases
    ]
    final_fields.append(SqlField(ratio_expr, anchored.output_alias))
    group_by: list[SqlExpr] = [SqlIdentifier(parts=["anchor", alias]) for alias in key_aliases]

    def _final_order_field(field: str) -> str:
        if field == "time":
            if not plan.time:
                raise SemanticLayerError(
                    "INVALID_ORDER_BY", "'time' order_by requires a query time axis"
                )
            return key_aliases[-1]
        return field

    order_by = [
        SqlOrder(
            SqlIdentifier(
                parts=[
                    "anchor",
                    _final_order_field(
                        str(expression_field(item, "field", expression_position="order_by"))
                    ),
                ]
            ),
            str(item.get("direction", "ASC")) if isinstance(item, dict) else "ASC",
        )
        for item in list(plan.query.get("order_by", []) or [])
    ]
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


def _distinct_value_path_selections(plan: LogicalPlan) -> list[PathSelection]:
    return [
        PathSelection(
            target_entity=target_entity,
            purpose="distinct_values",
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

    leaf_calendar_join: SqlJoin | None = None
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[str(time["temporal_role"])]
        dim = dimensions[role.dimension]
        raw_expr = _column_ref(entities[dim.entity].table, dim.column)
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        cal_binding = _leaf_calendar_binding(plan, config)
        if cal_binding is not None and time.get("grain"):
            cal_table, cal_grain_column, cal_join_column = cal_binding
            time_expr = _column_ref(cal_table, cal_grain_column)
            leaf_calendar_join = _calendar_join_for_leaf(raw_expr, cal_table, cal_join_column)
        else:
            time_expr = (
                _dialect(config).date_trunc(time["grain"], raw_expr)
                if time.get("grain")
                else raw_expr
            )
        time_alias = (
            time["temporal_role"]
            if not time.get("grain")
            else f"{time['temporal_role']}__{time['grain']}"
        )
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    for item in list(query.where or []):
        expr, _ = _resolve_dimension_expr(str(item.field), config)
        where_clauses.append(build_filter_condition(expr, item.op, item.value))

    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[str(time["temporal_role"])]
        dim = dimensions[role.dimension]
        raw_expr = _column_ref(entities[dim.entity].table, dim.column)
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_expr, "<", SqlLiteral(time["end"])))

    joins = list(
        _joins_for_paths(
            plan.root_entity, _distinct_value_path_selections(plan), config, time_spec=plan.time
        )
    )
    if leaf_calendar_join is not None:
        joins.append(leaf_calendar_join)
    return SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=entities[plan.root_entity].table),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )


def _path_signature(path_selections: Iterable[PathSelection]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(selection.chosen_path) for selection in path_selections)


def _foldable_leaf_signature(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> tuple[Any, ...] | None:
    measures = _measure_index(config)
    measure = measures[measure_plan.bound_measure.measure_id]
    if measure.measure_class == "semi_additive":
        return None
    if _all_metric_predicates(plan, measure_plan):
        return None
    if measure_plan.path_selections and not _paths_are_single_hop_safe(
        measure_plan.path_selections, config
    ):
        return None
    return (
        "foldable_leaf",
        getattr(measure_plan, "aggregate_relation_id", "") or "",
        measure.entity,
        getattr(measure, "source_relation", "") or "",
        _path_signature(measure_plan.path_selections),
        tuple(plan.group_by),
        _freeze_payload(plan.time),
        _leaf_time_role(measure_plan.bound_measure, normalize_query(plan.query), config),
        _freeze_payload(plan.query.get("where", []) or []),
        _freeze_payload(measure_plan.bound_measure.filter_spec),
    )


def _measure_plan_groups(plan: LogicalPlan, config: PackageConfig) -> list[list[MeasurePlan]]:
    grouped: dict[tuple[Any, ...], list[MeasurePlan]] = {}
    ordered: list[tuple[Any, ...]] = []
    for measure_plan in plan.measure_plans:
        signature = _foldable_leaf_signature(plan, measure_plan, config)
        if signature is None:
            signature = ("single_leaf", measure_plan.cte_name)
        if signature not in grouped:
            grouped[signature] = []
            ordered.append(signature)
        grouped[signature].append(measure_plan)
    return [grouped[key] for key in ordered]


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
        if not _dimension_is_local_or_direct(source_entity, str(item.get("field", "")), config):
            return False
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        if not _dimension_is_local_or_direct(source_entity, str(item.get("field", "")), config):
            return False
    return True


def build_physical_plan(plan: LogicalPlan, config: PackageConfig) -> PhysicalPlan:
    entities = _entity_index(config)
    measures = _measure_index(config)
    relationships = _relationship_index(config)
    nodes: list[PhysicalPlanNode] = []
    outputs: dict[str, str] = {}
    optimizations: list[dict[str, Any]] = []
    key_aliases = _query_key_aliases(plan)

    anchored = _anchored_entity_set_plan(plan, config)
    if anchored is not None:
        measure = measures[anchored.denominator_measure_plan.bound_measure.measure_id]
        source_relation = _measure_owned_relation(measure, entities)
        scan_id = "anchor_scan"
        nodes.append(
            PhysicalPlanNode(
                id=scan_id,
                kind="Scan",
                label=f"Scan anchor relation {source_relation}",
                details={
                    "source_entity": measure.entity,
                    "relation": source_relation,
                    "selected_relation": source_relation,
                    "selected_relation_type": "raw",
                    "measures": [
                        {
                            "measure_id": anchored.denominator_measure_plan.bound_measure.measure_id,
                            "aggregation": anchored.denominator_measure_plan.bound_measure.aggregation,
                            "alias": anchored.denominator_measure_plan.bound_measure.alias,
                        }
                    ],
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="anchor_filter",
                kind="Filter",
                inputs=[scan_id],
                details={
                    "query_filters": list(plan.query.get("where", []) or []),
                    "measure_filters": _bound_filter_clauses(
                        anchored.denominator_measure_plan.bound_measure, config
                    ),
                    "time_filter": dict(plan.time or {}),
                    "pushed_to_scan": True,
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="snapshot_entity_set",
                kind="SnapshotEntitySet",
                inputs=["anchor_filter"],
                details={
                    "anchor_entity": anchored.anchor_entity,
                    "snapshot_policy": measure.accumulation.snapshot,
                    "strategy": "qualify_row_number"
                    if _dialect(config).capabilities().get("qualify")
                    else "aggregate_join",
                    "grain": list(key_aliases),
                },
            )
        )
        predicate_node_ids: list[str] = []
        for index, predicate_payload in enumerate(
            [*anchored.base_predicates, *anchored.extra_predicates]
        ):
            predicate = _predicate_expr_from_payload(predicate_payload)
            root_entity = _expression_root_entity(predicate.input, config)
            relation = entities[root_entity].table
            predicate_scan_id = f"predicate_{index + 1}_scan"
            predicate_id = f"predicate_set_{index + 1}"
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_scan_id,
                    kind="Scan",
                    label=f"Scan predicate relation {relation}",
                    details={
                        "source_entity": root_entity,
                        "relation": relation,
                        "selected_relation": relation,
                        "selected_relation_type": "raw",
                        "measures": [
                            {
                                "predicate": expr_to_dict(predicate.input),
                                "op": predicate.op,
                                "value": predicate.value,
                            }
                        ],
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=f"predicate_{index + 1}_filter",
                    kind="Filter",
                    inputs=[predicate_scan_id],
                    details={
                        "query_filters": [],
                        "measure_filters": [],
                        "time_filter": dict(
                            _predicate_time_spec(predicate, normalize_query(plan.query), config)
                            or {}
                        ),
                        "pushed_to_scan": True,
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_id,
                    kind="PredicateSet",
                    inputs=[f"predicate_{index + 1}_filter"],
                    details={
                        "entity": predicate.entity,
                        "keys": _predicate_key_aliases(predicate, config),
                        "scope": "minimal_entity_period",
                        "dimension_joins_pruned": True,
                    },
                )
            )
            predicate_node_ids.append(predicate_id)
        if anchored.denominator_measure_plan.path_selections:
            nodes.append(
                PhysicalPlanNode(
                    id="dimension_decorate",
                    kind="DimensionDecorate",
                    inputs=["snapshot_entity_set"],
                    details={
                        "paths": [
                            asdict(selection)
                            for selection in anchored.denominator_measure_plan.path_selections
                        ],
                        "delayed_until_after_entity_set": True,
                    },
                )
            )
            aggregate_inputs = ["dimension_decorate", *predicate_node_ids]
        else:
            aggregate_inputs = ["snapshot_entity_set", *predicate_node_ids]
        nodes.append(
            PhysicalPlanNode(
                id="final_aggregate",
                kind="FinalAggregate",
                inputs=aggregate_inputs,
                details={
                    "anchor": anchored.anchor_entity,
                    "outputs": [anchored.output_alias],
                    "key_aliases": list(key_aliases),
                    "join_strategy": "anchor_left_join_predicate_sets",
                    "full_outer_alignments": 0,
                },
            )
        )
        optimizations.extend(
            [
                {
                    "kind": "anchored_entity_set",
                    "status": "applied",
                    "target": "final_aggregate",
                    "anchor_entity": anchored.anchor_entity,
                    "semantic_preconditions": {
                        "shared_denominator": True,
                        "predicate_entity": anchored.anchor_entity,
                        "aggregation": anchored.denominator_measure_plan.bound_measure.aggregation,
                    },
                },
                {
                    "kind": "predicate_dimension_pruning",
                    "status": "applied",
                    "target": "predicate_sets",
                },
                {
                    "kind": "snapshot_row_selection",
                    "status": "applied",
                    "target": "snapshot_entity_set",
                    "strategy": "qualify_row_number"
                    if _dialect(config).capabilities().get("qualify")
                    else "aggregate_join",
                },
            ]
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={anchored.output_alias: "final_aggregate"},
            optimizations=optimizations,
        )

    if _plan_requires_agent_dag_lowering(plan):
        nodes.append(
            PhysicalPlanNode(
                id="agent_dag",
                kind="FinalProject",
                label="Agent DAG branch lowering",
                details={
                    "lowering": "branch_queries",
                    "reason": "distribution_or_entity_value_expression",
                    "semantic_dag_nodes": [node.kind for node in plan.semantic_dag.nodes],
                },
            )
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "agent_dag" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    conversion_exprs = _conversion_exprs_for_plan(plan, config)
    if conversion_exprs and not plan.measure_plans:
        query_predicates = _query_metric_predicates(plan)
        conversion_node_ids: list[str] = []
        for index, expr in enumerate(conversion_exprs, start=1):
            base_entity = _expression_root_entity(expr.base, config)
            converted_entity = _expression_root_entity(expr.converted, config)
            base_scan_id = f"conversion_{index}_base_scan"
            converted_scan_id = f"conversion_{index}_converted_scan"
            match_id = f"conversion_{index}_match"
            aggregate_id = f"conversion_{index}_aggregate"
            nodes.append(
                PhysicalPlanNode(
                    id=base_scan_id,
                    kind="Scan",
                    label=f"Scan conversion base relation {entities[base_entity].table}",
                    details={
                        "source_entity": base_entity,
                        "relation": entities[base_entity].table,
                        "selected_relation": entities[base_entity].table,
                        "selected_relation_type": "raw",
                        "measures": [{"conversion_base": expr_to_dict(expr.base)}],
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=converted_scan_id,
                    kind="Scan",
                    label=f"Scan conversion converted relation {entities[converted_entity].table}",
                    details={
                        "source_entity": converted_entity,
                        "relation": entities[converted_entity].table,
                        "selected_relation": entities[converted_entity].table,
                        "selected_relation_type": "raw",
                        "measures": [{"conversion_converted": expr_to_dict(expr.converted)}],
                    },
                )
            )
            predicate_nodes, predicate_set_ids = _predicate_physical_nodes(
                query_predicates,
                prefix=f"conversion_{index}",
                plan=plan,
                config=config,
            )
            nodes.extend(predicate_nodes)
            nodes.append(
                PhysicalPlanNode(
                    id=match_id,
                    kind="ConversionMatch",
                    inputs=[base_scan_id, converted_scan_id, *predicate_set_ids],
                    details={
                        "entity": expr.entity,
                        "window": {"unit": expr.window_unit, "value": expr.window_value},
                        "matching_mode": expr.matching_mode,
                        "constant_properties": list(expr.constant_properties or []),
                        "metric_predicate_filters": [
                            expr_to_dict(predicate) for predicate in query_predicates
                        ],
                        "predicate_sets_joined_to_base": list(predicate_set_ids),
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=aggregate_id,
                    kind="ConversionAggregate",
                    inputs=[match_id],
                    details={
                        "grain": list(key_aliases),
                        "output_alias": _expression_alias(expr, config),
                    },
                )
            )
            conversion_node_ids.append(aggregate_id)
            if predicate_set_ids:
                optimizations.append(
                    {
                        "kind": "conversion_metric_predicate_pushdown",
                        "status": "applied",
                        "target": match_id,
                        "predicate_sets": list(predicate_set_ids),
                    }
                )
        if len(conversion_node_ids) > 1:
            nodes.append(
                PhysicalPlanNode(
                    id="align_conversions",
                    kind="AlignFacts",
                    inputs=conversion_node_ids,
                    details={
                        "keys": list(key_aliases),
                        "join_type": "FULL OUTER",
                        "reason": "multiple_independent_conversion_leaves",
                    },
                )
            )
            final_inputs = ["align_conversions"]
        else:
            final_inputs = list(conversion_node_ids)
        nodes.append(
            PhysicalPlanNode(
                id="final_project",
                kind="FinalProject",
                inputs=final_inputs,
                details={
                    "outputs": list(plan.post_aggregation_exprs),
                    "key_aliases": list(key_aliases),
                    "projection_wrappers": "pruned_when_no_metric_filters_or_order_by",
                },
            )
        )
        optimizations.append(
            {"kind": "conversion_leaf_lowering", "status": "applied", "target": "final_project"}
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "final_project" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    if not plan.measure_plans:
        nodes.append(
            PhysicalPlanNode(
                id="distinct_scan",
                kind="Scan",
                label="Distinct value scan",
                details={
                    "root_entity": plan.root_entity,
                    "relation": entities[plan.root_entity].table,
                    "group_by": list(plan.group_by),
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="final_project",
                kind="FinalProject",
                inputs=["distinct_scan"],
                details={"outputs": list(plan.post_aggregation_exprs)},
            )
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "final_project" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    aggregate_node_ids: list[str] = []
    for group in _measure_plan_groups(plan, config):
        first_plan = group[0]
        measure = measures[first_plan.bound_measure.measure_id]
        selected_aggregate = _selected_aggregate_relation(first_plan, config)
        entity_terms_anchor = _entity_in_terms_of_anchor_plan(plan, first_plan, config)
        scan_entity = (
            str(entity_terms_anchor["anchor_entity"])
            if entity_terms_anchor is not None and selected_aggregate is None
            else measure.entity
        )
        scan_relation = (
            selected_aggregate.relation
            if selected_aggregate is not None
            else (
                entities[scan_entity].table
                if entity_terms_anchor is not None
                else _measure_owned_relation(measure, entities)
            )
        )
        scan_id = f"{first_plan.cte_name}_scan"
        filter_id = f"{first_plan.cte_name}_filter"
        join_id = f"{first_plan.cte_name}_joins"
        aggregate_id = first_plan.cte_name
        measure_payloads = [
            {
                "measure_id": item.bound_measure.measure_id,
                "aggregation": item.bound_measure.aggregation,
                "alias": item.bound_measure.alias,
            }
            for item in group
        ]
        nodes.append(
            PhysicalPlanNode(
                id=scan_id,
                kind="Scan",
                label=f"Scan {scan_relation}",
                details={
                    "source_entity": measure.entity,
                    "physical_anchor_entity": scan_entity,
                    "omitted_semantic_root_entity": measure.entity
                    if entity_terms_anchor is not None and scan_entity != measure.entity
                    else "",
                    "relation": scan_relation,
                    "selected_relation": scan_relation,
                    "selected_relation_type": "aggregate_relation"
                    if selected_aggregate is not None
                    else "raw",
                    "aggregate_relation_id": selected_aggregate.id
                    if selected_aggregate is not None
                    else "",
                    "semantic_equivalence": selected_aggregate.equivalence_kind
                    if selected_aggregate is not None
                    else "",
                    "time_grain": selected_aggregate.grain
                    if selected_aggregate is not None
                    else "",
                    "measures": measure_payloads,
                },
            )
        )
        filter_details = {
            "query_filters": list(plan.query.get("where", []) or []),
            "measure_filters": _bound_filter_clauses(first_plan.bound_measure, config),
            "time_filter": dict(plan.time or {}),
            "pushed_to_scan": selected_aggregate is not None
            or entity_terms_anchor is None
            or _filters_pushable_to_entity_scan(scan_entity, plan, first_plan, config),
        }
        has_filter = bool(
            filter_details["query_filters"]
            or filter_details["measure_filters"]
            or filter_details["time_filter"]
        )
        if has_filter and filter_details["pushed_to_scan"]:
            nodes.append(
                PhysicalPlanNode(
                    id=filter_id, kind="Filter", inputs=[scan_id], details=filter_details
                )
            )
            input_id = filter_id
            optimizations.append(
                {"kind": "filter_pushdown", "status": "applied", "target": scan_id}
            )
        else:
            input_id = scan_id
        display_path_selections = (
            []
            if selected_aggregate is not None
            else (
                list(entity_terms_anchor["path_selections"])
                if entity_terms_anchor is not None
                else list(first_plan.path_selections)
            )
        )
        if display_path_selections:
            contract_payloads = []
            for selection in display_path_selections:
                for rel_id in selection.chosen_path:
                    rel = relationships.get(rel_id)
                    if rel is not None:
                        contract_payloads.append(
                            {
                                "relationship_id": rel.id,
                                "cardinality": rel.cardinality,
                                "safety": rel.safety,
                                "temporal_validity": bool(rel.temporal_validity),
                            }
                        )
            nodes.append(
                PhysicalPlanNode(
                    id=join_id,
                    kind="Join",
                    inputs=[input_id],
                    details={
                        "paths": [asdict(selection) for selection in display_path_selections],
                        "contracts": contract_payloads,
                        "rewrite_reason": "entity_in_terms_of"
                        if entity_terms_anchor is not None
                        else "",
                        "physical_anchor_entity": scan_entity
                        if entity_terms_anchor is not None
                        else "",
                        "omitted_semantic_root_entity": str(
                            entity_terms_anchor["omitted_root_entity"]
                        )
                        if entity_terms_anchor is not None
                        else "",
                        "linked_dimensions": [
                            {
                                "dimension_id": dim_id,
                                "target_entity": _dimension_index(config)[dim_id].entity,
                                "physical_anchor_entity": scan_entity,
                                "rewrite_reason": "entity_in_terms_of",
                            }
                            for dim_id in plan.group_by
                        ]
                        if entity_terms_anchor is not None
                        else [],
                    },
                )
            )
            input_id = join_id
        if has_filter and not filter_details["pushed_to_scan"]:
            nodes.append(
                PhysicalPlanNode(
                    id=filter_id, kind="Filter", inputs=[input_id], details=filter_details
                )
            )
            input_id = filter_id
        metric_predicates = _all_metric_predicates(plan, first_plan)
        if metric_predicates:
            predicate_nodes, predicate_set_ids = _predicate_physical_nodes(
                metric_predicates,
                prefix=aggregate_id,
                plan=plan,
                config=config,
            )
            nodes.extend(predicate_nodes)
            predicate_join_id = f"{aggregate_id}_predicate_joins"
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_join_id,
                    kind="Join",
                    inputs=[input_id, *predicate_set_ids],
                    details={
                        "join_type": "INNER",
                        "reason": "metric_predicate_entity_sets",
                        "predicate_sets": list(predicate_set_ids),
                        "predicates": [expr_to_dict(predicate) for predicate in metric_predicates],
                        "contracts": [],
                    },
                )
            )
            input_id = predicate_join_id
            optimizations.append(
                {
                    "kind": "metric_predicate_source_accounting",
                    "status": "applied",
                    "target": predicate_join_id,
                    "predicate_sets": list(predicate_set_ids),
                }
            )
        rollup_strategy = (
            {
                "strategy": "aggregate_relation",
                "reason": "selected_lossless_aggregate_relation",
                "aggregate_relation_id": selected_aggregate.id,
            }
            if selected_aggregate is not None
            else _source_rollup_strategy(plan, first_plan, config)
        )
        if len(group) > 1:
            optimizations.append(
                {
                    "kind": "same_source_measure_folding",
                    "status": "applied",
                    "target": aggregate_id,
                    "measure_aliases": [item["alias"] for item in measure_payloads],
                }
            )
        optimizations.append(
            {
                "kind": "source_aggregate_placement",
                "status": rollup_strategy["strategy"],
                "target": aggregate_id,
                "reason": rollup_strategy["reason"],
            }
        )
        nodes.append(
            PhysicalPlanNode(
                id=aggregate_id,
                kind="Aggregate",
                inputs=[input_id],
                details={
                    "grain": list(key_aliases),
                    "measures": measure_payloads,
                    "folded_measure_count": len(group),
                    "aggregate_placement": rollup_strategy,
                    "aggregate_relation_id": selected_aggregate.id
                    if selected_aggregate is not None
                    else "",
                },
            )
        )
        aggregate_node_ids.append(aggregate_id)
        for item in group:
            outputs[item.bound_measure.alias] = aggregate_id

    if len(aggregate_node_ids) > 1:
        nodes.append(
            PhysicalPlanNode(
                id="align_facts",
                kind="AlignFacts",
                inputs=aggregate_node_ids,
                details={
                    "keys": list(key_aliases),
                    "join_type": "FULL OUTER",
                    "reason": "multiple_independent_metric_leaves",
                },
            )
        )
        final_inputs = ["align_facts"]
    else:
        final_inputs = list(aggregate_node_ids)

    nodes.append(
        PhysicalPlanNode(
            id="final_project",
            kind="FinalProject",
            inputs=final_inputs,
            details={
                "outputs": list(plan.post_aggregation_exprs),
                "key_aliases": list(key_aliases),
                "projection_wrappers": "pruned_when_no_metric_filters_or_order_by",
            },
        )
    )
    for alias in plan.post_aggregation_exprs:
        outputs[alias] = "final_project"
    return PhysicalPlan(version=1, nodes=nodes, outputs=outputs, optimizations=optimizations)


_LARGE_PRIMITIVE_RELATION_HINTS = {
    "COHORTED_EMAILS",
    "COHORTED_SMS_DETAIL",
    "COHORTED_PUSH",
    "COHORTED_MESSAGES",
    "RPT_DAILY_ACCOUNT_PRODUCT_SUBSCRIPTIONS_METRICS_WIDE",
    "GMV_FEED_POWDERHORN",
}


def _physical_node_index(physical_plan: PhysicalPlan) -> dict[str, PhysicalPlanNode]:
    return {node.id: node for node in physical_plan.nodes}


def _upstream_nodes(
    node_id: str, nodes: dict[str, PhysicalPlanNode], *, seen: set[str] | None = None
) -> list[PhysicalPlanNode]:
    seen = set(seen or set())
    if node_id in seen:
        return []
    seen.add(node_id)
    node = nodes.get(node_id)
    if node is None:
        return []
    out = [node]
    for input_id in list(node.inputs or []):
        out.extend(_upstream_nodes(input_id, nodes, seen=seen))
    return out


def _time_window_days(time_spec: dict[str, Any]) -> int | None:
    start = time_spec.get("start")
    end = time_spec.get("end")
    if start is None or end is None:
        return None

    def _parse(value: Any) -> datetime:
        text = str(value)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromisoformat(text[:10])

    try:
        return max(0, (_parse(end) - _parse(start)).days)
    except Exception:
        return None


def _relation_has_large_hint(relation: str) -> bool:
    normalized = relation.upper()
    return any(hint in normalized for hint in _LARGE_PRIMITIVE_RELATION_HINTS)


def _path_hop_count(join_node: PhysicalPlanNode) -> int:
    max_hops = 0
    for selection in list(join_node.details.get("paths", []) or []):
        max_hops = max(max_hops, len(list(selection.get("chosen_path", []) or [])))
    return max_hops


def _join_payload(
    aggregate_node: PhysicalPlanNode, join_node: PhysicalPlanNode, strategy: dict[str, Any]
) -> dict[str, Any]:
    contracts = list(join_node.details.get("contracts", []) or [])
    safe_contracts = bool(contracts) and all(
        str(row.get("safety", "safe")).lower() == "safe"
        and str(row.get("cardinality", "")) in {"1:1", "N:1"}
        for row in contracts
    )
    return {
        "aggregate_id": aggregate_node.id,
        "join_node": join_node.id,
        "strategy": str(strategy.get("strategy", "")),
        "reason": str(strategy.get("reason", "")),
        "max_path_hops": _path_hop_count(join_node),
        "safe_before_aggregate": safe_contracts,
        "paths": list(join_node.details.get("paths", []) or []),
        "contracts": contracts,
    }


def build_performance_plan(
    plan: LogicalPlan, config: PackageConfig, physical_plan: PhysicalPlan, rendered_sql: str
) -> PerformancePlan:
    nodes = _physical_node_index(physical_plan)
    scans = [node for node in physical_plan.nodes if node.kind == "Scan"]
    filters_by_scan = {
        str(node.inputs[0]): node
        for node in physical_plan.nodes
        if node.kind == "Filter" and node.inputs
    }
    estimated_scan_relations: list[dict[str, Any]] = []
    date_filter_pushdown: list[dict[str, Any]] = []
    for scan in scans:
        details = dict(scan.details or {})
        relation = str(details.get("selected_relation") or details.get("relation") or "")
        filter_node = filters_by_scan.get(scan.id)
        time_filter = dict(
            (filter_node.details if filter_node else {}).get("time_filter", {}) or {}
        )
        has_start = time_filter.get("start") is not None
        has_end = time_filter.get("end") is not None
        pushed = bool(filter_node and filter_node.details.get("pushed_to_scan"))
        scan_payload = {
            "scan_id": scan.id,
            "relation": relation,
            "source_entity": details.get("source_entity") or details.get("root_entity") or "",
            "selected_relation_type": str(details.get("selected_relation_type", "raw") or "raw"),
            "measures": list(details.get("measures", []) or []),
            "estimated_rows": "unknown_compile_only",
            "has_time_filter": bool(time_filter),
            "time_filter_has_start": has_start,
            "time_filter_has_end": has_end,
            "has_source_filter": bool(
                (filter_node.details if filter_node else {}).get("query_filters")
                or (filter_node.details if filter_node else {}).get("measure_filters")
            ),
            "large_relation_hint": _relation_has_large_hint(relation),
        }
        estimated_scan_relations.append(scan_payload)
        date_filter_pushdown.append(
            {
                "scan_id": scan.id,
                "relation": relation,
                "pushed_to_scan": pushed,
                "has_time_filter": bool(time_filter),
                "has_start": has_start,
                "has_end": has_end,
                "time_filter": time_filter,
            }
        )

    pre_aggregation_grain: dict[str, list[str]] = {}
    joins_before_aggregate: list[dict[str, Any]] = []
    joins_after_aggregate: list[dict[str, Any]] = []
    for aggregate in [node for node in physical_plan.nodes if node.kind == "Aggregate"]:
        pre_aggregation_grain[aggregate.id] = list(aggregate.details.get("grain", []) or [])
        strategy = dict(aggregate.details.get("aggregate_placement", {}) or {})
        upstream_joins = [
            node for node in _upstream_nodes(aggregate.id, nodes) if node.kind == "Join"
        ]
        for join_node in upstream_joins:
            payload = _join_payload(aggregate, join_node, strategy)
            if strategy.get("strategy") == "source_preaggregate":
                joins_after_aggregate.append(payload)
            else:
                joins_before_aggregate.append(payload)

    full_outer_alignments = 0
    for align in [node for node in physical_plan.nodes if node.kind == "AlignFacts"]:
        full_outer_alignments += max(1, len(list(align.inputs or [])) - 1)
    if full_outer_alignments == 0 and rendered_sql:
        full_outer_alignments = rendered_sql.upper().count("FULL OUTER JOIN")

    raw_scans = [row for row in estimated_scan_relations if row["selected_relation_type"] == "raw"]
    raw_fact_count = len(raw_scans)
    bounded_raw_scans = [
        row
        for row in date_filter_pushdown
        if any(
            scan["scan_id"] == row["scan_id"] and scan["selected_relation_type"] == "raw"
            for scan in estimated_scan_relations
        )
        and row["has_start"]
        and row["has_end"]
    ]
    all_raw_bounded = bool(raw_scans) and len(bounded_raw_scans) == raw_fact_count
    window_days = _time_window_days(dict(plan.time or {}))
    large_raw_relations = [row["relation"] for row in raw_scans if row.get("large_relation_hint")]
    multi_hop_after_aggregate = any(
        int(row.get("max_path_hops", 0) or 0) > 1 for row in joins_after_aggregate
    )
    source_preaggregates = [
        node.id
        for node in physical_plan.nodes
        if node.kind == "Aggregate"
        and dict(node.details.get("aggregate_placement", {}) or {}).get("strategy")
        == "source_preaggregate"
    ]
    routed_aggregates = [
        str(node.details.get("aggregate_relation_id") or "")
        for node in physical_plan.nodes
        if node.kind == "Scan"
        and str(node.details.get("selected_relation_type") or "") == "aggregate_relation"
    ]
    routed_aggregates = [item for item in routed_aggregates if item]
    multi_fact_raw_alignment = raw_fact_count >= 2 and full_outer_alignments > 0
    expensive_parent_or_multihop = multi_hop_after_aggregate or any(
        "parent" in dim.lower() or "geo" in dim.lower() for dim in plan.group_by
    )
    q3_like_primitive_shape = (
        raw_fact_count >= 3 and full_outer_alignments >= 2 and expensive_parent_or_multihop
    )

    risk_reasons: list[str] = []
    if raw_scans and not all_raw_bounded:
        risk_reasons.append("unbounded_raw_primitive_scan")
    if large_raw_relations:
        risk_reasons.append("known_large_primitive_relation")
    if multi_fact_raw_alignment:
        risk_reasons.append("multi_fact_raw_alignment")
    if source_preaggregates:
        risk_reasons.append("requires_source_preaggregation_before_dimension_joins")
    if q3_like_primitive_shape:
        risk_reasons.append("primitive_exact_but_expensive")

    risk_level = "low"
    execution_recommendation = "safe_to_run"
    if (
        raw_scans
        and not all_raw_bounded
        and (large_raw_relations or raw_fact_count >= 2)
        or q3_like_primitive_shape
        and (window_days is None or window_days > 45)
    ):
        risk_level = "high"
        execution_recommendation = "needs_acceleration_layer"
    elif multi_fact_raw_alignment or source_preaggregates or large_raw_relations:
        risk_level = "medium"
        execution_recommendation = "run_with_small_window"

    notes: list[str] = []
    if q3_like_primitive_shape:
        notes.append("primitive_exact_but_expensive")
    if source_preaggregates:
        notes.append(
            "source filters are pushed before primitive pre-aggregation; dimension joins occur after source rollup"
        )
    if not scans and any(node.kind == "DistributionAggregate" for node in plan.semantic_dag.nodes):
        notes.append(
            "distribution branch is compiled through branch lowering; inspect rendered SQL for branch-local scan details"
        )

    return PerformancePlan(
        estimated_scan_relations=estimated_scan_relations,
        raw_fact_count=raw_fact_count,
        date_filter_pushdown=date_filter_pushdown,
        pre_aggregation_grain=pre_aggregation_grain,
        joins_before_aggregate=joins_before_aggregate,
        joins_after_aggregate=joins_after_aggregate,
        full_outer_alignments=full_outer_alignments,
        large_scan_risk={
            "risk": risk_level != "low",
            "reasons": risk_reasons,
            "large_raw_relations": large_raw_relations,
            "time_window_days": window_days,
            "all_raw_scans_bounded": all_raw_bounded,
            "source_preaggregates": source_preaggregates,
        },
        risk_level=risk_level,
        execution_recommendation=execution_recommendation,
        semantic_equivalence="exact_primitives",
        notes=notes,
        aggregate_routing={
            "selected": sorted(set(routed_aggregates)),
            "selected_count": len(routed_aggregates),
        },
    )


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
    leaf_calendar_join: SqlJoin | None = None
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
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(_measure_dim_relation(first_measure, dim, entities), dim.column)
        )
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        cal_binding = _leaf_calendar_binding(plan, config)
        if cal_binding is not None and time.get("grain"):
            cal_table, cal_grain_column, cal_join_column = cal_binding
            time_expr = _column_ref(cal_table, cal_grain_column)
            leaf_calendar_join = _calendar_join_for_leaf(raw_expr, cal_table, cal_join_column)
        else:
            time_expr = (
                _dialect(config).date_trunc(time["grain"], raw_expr)
                if time.get("grain")
                else raw_expr
            )
        time_alias = (
            time["temporal_role"]
            if not time.get("grain")
            else f"{time['temporal_role']}__{time['grain']}"
        )
        select_fields.append(SqlField(time_expr, time_alias))
        group_fields.append(time_expr)

    where_clauses: list[Any] = []
    for item in list(query.get("where", []) or []):
        expr, _ = _direct_dimension_source_expr(
            first_measure.entity, str(item["field"]), config
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
    for item in _bound_filter_clauses(first_plan.bound_measure, config):
        expr, _ = _direct_dimension_source_expr(
            first_measure.entity, str(item["field"]), config
        ) or _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(_value_filter_condition(expr, item))
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        direct_time = _direct_dimension_source_expr(first_measure.entity, role.dimension, config)
        raw_expr = (
            direct_time[0]
            if direct_time is not None
            else _column_ref(_measure_dim_relation(first_measure, dim, entities), dim.column)
        )
        raw_expr = _apply_role_timezone(raw_expr, role, config)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_expr, "<", SqlLiteral(time["end"])))

    for measure_plan in measure_plans:
        measure = measures[measure_plan.bound_measure.measure_id]
        order_expr = None
        if measure_plan.bound_measure.temporal_role:
            role = temporal_roles[measure_plan.bound_measure.temporal_role]
            time_dim = dimensions[role.dimension]
            order_expr = _column_ref(
                _measure_dim_relation(measure, time_dim, entities), time_dim.column
            )
        select_fields.append(
            SqlField(
                _aggregation_expr(
                    _config_expr_to_sql(measure.expr, measure, config),
                    measure_plan.bound_measure.aggregation,
                    order_expr=order_expr,
                    parameters=measure_plan.bound_measure.aggregation_params,
                    dialect=_dialect(config),
                ),
                measure_plan.bound_measure.alias,
            )
        )

    joins = list(
        _joins_for_paths(
            first_measure.entity, first_plan.path_selections, config, time_spec=plan.time
        )
    )
    if leaf_calendar_join is not None:
        joins.append(leaf_calendar_join)
    return SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=_measure_owned_relation(first_measure, entities)),
        joins=joins,
        where=where_clauses,
        group_by=group_fields,
    )


def _query_requires_dense_series(plan: LogicalPlan, config: PackageConfig) -> bool:
    if plan.time and plan.time.get("fill"):
        return True
    if any(
        _expr_requires_dense_series(_parse_public_expr(expr), config)
        for expr in plan.post_aggregation_exprs.values()
    ):
        return True
    for item in list(plan.query.get("metric_filters", []) or []):
        expr = _parse_public_expr(dict(item["expression"]))
        if isinstance(expr, MetricPredicateExpr):
            continue
        if _expr_requires_dense_series(expr, config):
            return True
    return False


def _calendar_join_for_leaf(
    raw_role_expr: Any,
    calendar_table: str,
    calendar_join_column: str,
) -> SqlJoin:
    """Build the LEFT JOIN that aligns a Gregorian-frame role timestamp
    with a non-default calendar's date_day. The cast to DATE is required
    because role columns may be timestamp/datetime while calendar tables
    key on DATE.
    """
    return SqlJoin(
        join_type="LEFT",
        table=SqlTableRef(name=calendar_table),
        on=SqlBinary(
            SqlIdentifier(parts=[calendar_table, calendar_join_column]),
            "=",
            SqlCast(expr=raw_role_expr, type_name="DATE"),
        ),
    )


def _leaf_calendar_binding(plan: LogicalPlan, config: PackageConfig) -> tuple[str, str, str] | None:
    """When the query requests a non-default calendar (e.g. fiscal) on a
    role whose underlying entity is bound to a different calendar, the
    leaf SQL must bucket by the calendar's grain column — NOT by
    DATE_TRUNC of the role's source column.

    The validate-time `_validate_calendar_id` check already rejects this
    shape unless `time.fill: true` is set, which routes through the
    calendar entity. This helper returns the calendar table, the grain
    column to GROUP BY, and the calendar's date_day column to JOIN on.
    Returns None when no rewrite is needed (default calendar, or the
    role is itself bound to the requested calendar).

    Without this binding, the leaf groups by Gregorian DATE_TRUNC while
    the dense_time scaffold returns fiscal grain anchors — the equality
    join misses every bucket and COALESCE fills 0.0, producing a
    canonical fiscal-quarterly query whose every value is silently 0.
    """
    if plan.time is None:
        return None
    requested_calendar = str(plan.time.get("calendar_id", "default") or "default").strip().lower()
    if requested_calendar in {"", "default"}:
        return None
    grain = str(plan.time.get("grain", "") or "").lower()
    column_by_grain = {
        "day": "date_day",
        "week": "week_start",
        "month": "month_start",
        "quarter": "quarter_start",
        "year": "year_start",
    }
    if grain not in column_by_grain:
        return None
    # Only rewrite when the role's underlying entity is on a different
    # calendar — otherwise the source column is already in the requested
    # calendar's frame and DATE_TRUNC is correct.
    temporal_roles = _temporal_role_index(config)
    role = temporal_roles.get(str(plan.time.get("temporal_role") or ""))
    if role is None:
        return None
    dim = _dimension_index(config).get(role.dimension)
    if dim is None:
        return None
    role_entity = next((row for row in config.entities if row.id == dim.entity), None)
    role_calendar = (role_entity.calendar_id if role_entity else "").strip().lower() or "default"
    if role_calendar == requested_calendar:
        return None
    # Find the calendar entity bound to the requested calendar.
    calendar_candidates = [row for row in config.entities if row.kind == "time"]
    calendar_entity = next(
        (
            row
            for row in calendar_candidates
            if (row.calendar_id or "default").strip().lower() == requested_calendar
        ),
        None,
    )
    if calendar_entity is None:
        return None
    grain_column = column_by_grain[grain]
    grain_dim = next(
        (
            row
            for row in config.dimensions
            if row.entity == calendar_entity.id and row.column == grain_column
        ),
        None,
    )
    if grain_dim is None:
        return None
    return calendar_entity.table, grain_dim.column, "date_day"


def _calendar_fill_binding(
    plan: LogicalPlan, config: PackageConfig, *, force: bool = False
) -> tuple[str, str] | None:
    if not plan.time or (not plan.time.get("fill") and not force):
        return None
    grain = str(plan.time.get("grain", "") or "").lower()
    if not grain:
        raise SemanticLayerError("INVALID_QUERY", "time.fill requires query.time.grain")

    column_by_grain = {
        "day": "date_day",
        "week": "week_start",
        "month": "month_start",
        "quarter": "quarter_start",
        "year": "year_start",
    }
    if grain not in column_by_grain:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED", f"time.fill does not support grain '{grain}'"
        )

    requested_calendar = str(plan.time.get("calendar_id", "default") or "default")
    calendar_candidates = [row for row in config.entities if row.kind == "time"]
    calendar_entity = next(
        (
            row
            for row in calendar_candidates
            if (row.calendar_id or "default") == requested_calendar
        ),
        None,
    )
    if calendar_entity is None:
        if requested_calendar != "default":
            raise SemanticLayerError(
                "REWRITE_NOT_SUPPORTED",
                f"Calendar '{requested_calendar}' is not available in the package",
                details={"calendar_id": requested_calendar},
            )
        calendar_entity = next(iter(calendar_candidates), None)
    if calendar_entity is None:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED", "time.fill requires a calendar entity in the package"
        )

    calendar_column = column_by_grain[grain]
    dimension = next(
        (
            row
            for row in config.dimensions
            if row.entity == calendar_entity.id and row.column == calendar_column
        ),
        None,
    )
    if dimension is None:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"time.fill requires calendar dimension '{calendar_column}' on '{calendar_entity.id}'",
        )
    return calendar_entity.table, dimension.column


def _tier_internal_aliases(
    plan: LogicalPlan,
    select: SqlSelect,
    *,
    measure_aliases: Iterable[str],
    metric_filter_aliases: Iterable[str] = (),
) -> SqlSelect:
    registry = AliasRegistry.for_plan(
        plan, measure_aliases=measure_aliases, metric_filter_aliases=metric_filter_aliases
    )
    if not registry.aliases:
        return select
    return alias_select(select, registry, preserve_output_aliases=True)


def _conversion_exprs_for_plan(plan: LogicalPlan, config: PackageConfig) -> list[ConversionExpr]:
    conversions: dict[str, ConversionExpr] = {}
    for expr_payload in plan.post_aggregation_exprs.values():
        out: list[ConversionExpr] = []
        _collect_conversion_exprs(_parse_public_expr(expr_payload), config, out)
        for expr in out:
            conversions[_freeze_payload(expr_to_dict(expr))] = expr
    for item in list(plan.query.get("metric_filters", []) or []):
        raw_expr = dict(item.get("expression", {}) or {})
        if not raw_expr:
            continue
        parsed = _parse_public_expr(raw_expr)
        if isinstance(parsed, MetricPredicateExpr):
            continue
        _collect_conversion_exprs(parsed, config, out := [])
        for expr in out:
            conversions[_freeze_payload(expr_to_dict(expr))] = expr
    return list(conversions.values())


def lower_to_sql(plan: LogicalPlan, config: PackageConfig) -> SqlSelect:
    anchored_select = _anchored_entity_set_select(plan, config)
    if anchored_select is not None:
        return _tier_internal_aliases(plan, anchored_select, measure_aliases=[])
    if _plan_requires_agent_dag_lowering(plan):
        return _lower_agent_dag_to_sql(plan, config)

    key_aliases = _query_key_aliases(plan)
    measure_aliases: list[str] = []
    conversion_exprs = _conversion_exprs_for_plan(plan, config)
    if plan.measure_plans or conversion_exprs:
        leaf_ctes: list[SqlCte] = []
        measure_groups = _measure_plan_groups(plan, config)
        for measure_group in measure_groups:
            cte_name = measure_group[0].cte_name
            leaf_ctes.append(
                SqlCte(
                    name=cte_name,
                    query=_namespace_sql_select(
                        _measure_group_leaf_select(plan, measure_group, config), f"{cte_name}__"
                    ),
                )
            )

        conversion_aliases: list[str] = []
        for index, expr in enumerate(conversion_exprs):
            conversion_cte = _conversion_leaf_cte(expr, index=index, plan=plan, config=config)
            leaf_ctes.append(
                SqlCte(
                    name=conversion_cte.name,
                    query=_namespace_sql_select(conversion_cte.query, f"{conversion_cte.name}__"),
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
            [row.bound_measure.alias for row in group] for group in measure_groups
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
                for alias in [*key_aliases, *measure_aliases]
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
    # When no calendar fill is needed, the `leaf_base` CTE is a passthrough
    # rename of the underlying leaf (`SELECT base.X AS X FROM <leaf> AS base`)
    # — pure noise. Skip it whenever we have leaf CTEs to reference directly,
    # whether they came from measures or from conversion metrics.
    if fill_binding is None and (plan.measure_plans or conversion_exprs):
        ctes: list[SqlCte] = list(base_leaf_select.ctes)
        assert isinstance(base_leaf_select.from_table, SqlTableRef)  # leaf select uses table ref
        base_table = SqlTableRef(name=base_leaf_select.from_table.name, alias="base")
        base_name = base_table.alias
    else:
        ctes = [SqlCte(name="leaf_base", query=base_leaf_select)]
        base_table = SqlTableRef(name="leaf_base", alias="base")
        base_name = "leaf_base"

    if fill_binding is not None:
        calendar_table, calendar_column = fill_binding
        calendar_expr = _column_ref(calendar_table, calendar_column)
        dense_time_where: list[Any] = []
        dense_time_joins: list[SqlJoin] = []
        if plan.time.get("start") is not None and plan.time.get("end") is not None:
            dense_time_where = [
                SqlBinary(calendar_expr, ">=", SqlLiteral(plan.time["start"])),
                SqlBinary(calendar_expr, "<", SqlLiteral(plan.time["end"])),
            ]
        else:
            ctes.append(
                SqlCte(
                    name="dense_bounds",
                    query=SqlSelect(
                        select=[
                            SqlField(
                                SqlCall("MIN", [SqlIdentifier(parts=["leaf_base", time_alias])]),
                                "range_start",
                            ),
                            SqlField(
                                SqlCall("MAX", [SqlIdentifier(parts=["leaf_base", time_alias])]),
                                "range_end",
                            ),
                        ],
                        from_table=SqlTableRef(name="leaf_base"),
                    ),
                )
            )
            # CROSS JOIN with a 1-row scalar source reads more honestly than
            # `INNER JOIN ... ON TRUE`, which looks like a bailout.
            dense_time_joins.append(
                SqlJoin(join_type="CROSS", table=SqlTableRef(name="dense_bounds"), on=None)
            )
            dense_time_where = [
                SqlBinary(
                    calendar_expr, ">=", SqlIdentifier(parts=["dense_bounds", "range_start"])
                ),
                SqlBinary(calendar_expr, "<=", SqlIdentifier(parts=["dense_bounds", "range_end"])),
            ]
        # Inner ORDER BY in a CTE without LIMIT is ignored by Snowflake/DuckDB
        # (relations have no inherent ordering). The final SELECT carries its
        # own ORDER BY, so an extra one inside the CTE is just noise.
        ctes.append(
            SqlCte(
                name="dense_time",
                query=SqlSelect(
                    select=[SqlField(calendar_expr, time_alias)],
                    from_table=SqlTableRef(name=calendar_table),
                    joins=dense_time_joins,
                    where=dense_time_where,
                    group_by=[calendar_expr],
                ),
            )
        )

        joins: list[SqlJoin] = []
        filled_fields: list[SqlField] = []
        order_fields: list[SqlOrder] = []
        from_table = SqlTableRef(name="dense_time")
        if group_aliases:
            ctes.append(
                SqlCte(
                    name="dense_groups",
                    query=SqlSelect(
                        select=[
                            SqlField(SqlIdentifier(parts=["leaf_base", alias]), alias)
                            for alias in group_aliases
                        ],
                        from_table=SqlTableRef(name="leaf_base"),
                        group_by=[
                            SqlIdentifier(parts=["leaf_base", alias]) for alias in group_aliases
                        ],
                    ),
                )
            )
            from_table = SqlTableRef(name="dense_groups")
            # `dense_time` and `dense_groups` are independent enumerations; emit
            # the cross-product as CROSS JOIN rather than `INNER JOIN ... ON TRUE`.
            joins.append(SqlJoin(join_type="CROSS", table=SqlTableRef(name="dense_time"), on=None))
            for alias in group_aliases:
                filled_fields.append(SqlField(SqlIdentifier(parts=["dense_groups", alias]), alias))
                order_fields.append(SqlOrder(SqlIdentifier(parts=["dense_groups", alias]), "ASC"))
        filled_fields.append(SqlField(SqlIdentifier(parts=["dense_time", time_alias]), time_alias))
        order_fields.append(SqlOrder(SqlIdentifier(parts=["dense_time", time_alias]), "ASC"))
        join_condition: Any = SqlBinary(
            SqlIdentifier(parts=["dense_time", time_alias]),
            "=",
            SqlIdentifier(parts=["leaf_base", time_alias]),
        )
        for alias in group_aliases:
            join_condition = SqlBinary(
                join_condition,
                "AND",
                _dialect(config).null_safe_eq(
                    SqlIdentifier(parts=["dense_groups", alias]),
                    SqlIdentifier(parts=["leaf_base", alias]),
                ),
            )
        joins.append(
            SqlJoin(join_type="LEFT", table=SqlTableRef(name="leaf_base"), on=join_condition)
        )
        zero_fill_aliases = _dense_fill_zero_aliases(plan, config)
        for alias in measure_aliases:
            leaf_value: Any = SqlIdentifier(parts=["leaf_base", alias])
            if alias in zero_fill_aliases:
                leaf_value = SqlCall("COALESCE", [leaf_value, SqlLiteral(0)])
            filled_fields.append(SqlField(leaf_value, alias))
        ctes.append(
            SqlCte(
                name="series_base",
                query=SqlSelect(
                    select=filled_fields,
                    from_table=from_table,
                    joins=joins,
                    # No inner ORDER BY: the outer SELECT controls final ordering.
                ),
            )
        )
        base_name = "series_base"
        base_table = SqlTableRef(name=base_name, alias="base")

    projected_fields: list[SqlField] = [
        SqlField(SqlIdentifier(parts=["base", alias]), alias) for alias in key_aliases
    ]
    for alias, post_expr in plan.post_aggregation_exprs.items():
        projected_fields.append(
            SqlField(
                _compile_post_expr(
                    _parse_public_expr(post_expr),
                    config,
                    time_alias=time_alias,
                    group_aliases=group_aliases,
                    query_grain=str(plan.time.get("grain", "") if plan.time else ""),
                    table_alias="base",
                ),
                alias,
            )
        )

    metric_filter_aliases: list[str] = []
    for index, item in enumerate(list(plan.query.get("metric_filters", []) or [])):
        filter_expr = _parse_public_expr(dict(item["expression"]))
        if isinstance(filter_expr, MetricPredicateExpr):
            continue
        alias = _metric_filter_alias(index)
        metric_filter_aliases.append(alias)
        compiled_expr = _compile_post_expr(
            filter_expr,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=str(plan.time.get("grain", "") if plan.time else ""),
            table_alias="base",
        )
        # When a metric_filter targets an additive measure that may live in a
        # different leaf than other selected measures, the FULL OUTER JOIN
        # combine yields NULL for groups where that measure has no rows. Without
        # COALESCE, the natural form `op: "=" value: 0` (e.g., "orders with no
        # items") silently returns zero rows because `NULL = 0` is FALSE. Coerce
        # NULL to the additive identity (0) so zero-count semantics work.
        if _expr_zero_on_missing(filter_expr, config):
            compiled_expr = SqlCall("COALESCE", [compiled_expr, SqlLiteral(0)])
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

    def _final_order_field(field: str) -> str:
        if field == "time":
            if not plan.time:
                raise SemanticLayerError(
                    "INVALID_ORDER_BY", "'time' order_by requires a query time axis"
                )
            return key_aliases[-1]
        return field

    order_by = [
        SqlOrder(
            SqlIdentifier(
                parts=[
                    "projected",
                    _final_order_field(
                        str(expression_field(item, "field", expression_position="order_by"))
                    ),
                ]
            ),
            str(item.get("direction", "ASC")) if isinstance(item, dict) else "ASC",
        )
        for item in list(plan.query.get("order_by", []) or [])
    ]

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
        for alias in [*key_aliases, *plan.post_aggregation_exprs.keys()]
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
