"""Query IR -> logical plan -> SQL AST compilation pipeline.

The compiler is the runtime's biggest module. It takes a normalized
``NormalizedQuery``, resolves measures/metrics/dimensions against the
package config, validates path safety (fanout, rollup, predicate
scope), applies the registered rewrites, and emits both a logical plan
(:mod:`semantic_rails.ir`) and a SQL AST
(:mod:`semantic_rails.sql_ast`). Exposes :func:`compile_query` (the
runtime entry point) and :func:`plan_comparison_bundle` (used by older
comparison-bundle helpers). Heavy lifting lives in
``semantic_rails/compiler_parts/`` — this module is the orchestration
layer.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from .ast import NormalizedQuery, _time_output_alias, normalize_query
from .compiler_parts.bind import (
    _aggregation_expr,
    _bind_measure,
    _bind_scoped_aggregate,
    _bound_filter_clauses,
    _collect_conversion_exprs,
    _collect_measure_refs,
    _config_expr_to_sql,
    _expression_alias,
    _freeze_payload,
    _measure_required_entities,
    _parse_public_expr,
    _resolve_expr_entity,
    _resolve_filter_dimension,
    _scoped_aggregate_filter_spec,
    _scoped_predicate_expr_payload,
    lift_conditional_aggregates,
)
from .compiler_parts.grain_recovery import mixed_grain_pairing_enrichment
from .compiler_parts.indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
)
from .compiler_parts.paths import (
    _column_ref,
    _direct_dimension_source_expr,
    _direct_entity_key_source_expr,
    _entity_key_dimension_ids,
    _expression_root_entity,
    _join_condition,
    _joins_for_paths,
    _leaf_time_role,
    _resolve_dimension_expr,
    _split_column_ref,
)
from .compiler_parts.post_aggregation import (
    _compile_post_expr,
    _expr_requires_dense_series,
    _namespace_sql_select,
)
from .compiler_parts.temporal import (
    _combine_temporal_roles,
    _expr_compatible_temporal_roles,
    _expr_contains_cumulative,
    _expr_leaf_temporal_role_sets,
    _recipe_compatible_temporal_roles,
    _requires_query_time,
    _time_bound_relationship_ids,
    _validate_query_temporal_bindings,
    _validate_restrictive_time_semantics,
)
from .diagnostics import relationship_contract_payload
from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError
from .expressions import (
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
    LiteralExpr,
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
    expr_kind,
    expr_to_dict,
)
from .fanout import analyze_fanout, choose_path, package_hop_limit
from .ir import (
    BoundMeasure,
    ExplainArtifact,
    LogicalPlan,
    MeasurePlan,
    PathSelection,
    RewriteStep,
    SemanticDag,
    SemanticDagNode,
)
from .registry import Registry
from .relation_pipelines import attach_relation_ctes
from .renderer import render_select_for_profile
from .schema import (
    AggregateRelationConfig,
    DimensionConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    RelationshipConfig,
    TemporalRoleConfig,
)
from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlOrderTerm,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
    SqlWithinGroup,
    build_filter_condition,
)

__all__ = [
    "AggregateExpr",
    "ArithmeticExpr",
    "BooleanExpr",
    "BoundMeasure",
    "CallExpr",
    "CaseExpr",
    "ColumnRefExpr",
    "ComparisonExpr",
    "ConversionExpr",
    "CumulativeExpr",
    "DimensionConfig",
    "DistributionExpr",
    "EntityValueExpr",
    "ExplainArtifact",
    "LiteralExpr",
    "LogicalPlan",
    "MeasureConfig",
    "MeasurePlan",
    "MeasureRefExpr",
    "MetricConfig",
    "MetricPredicateExpr",
    "MetricRecipeRefExpr",
    "NormalizedQuery",
    "OffsetWindowExpr",
    "PackageConfig",
    "PathSelection",
    "PeriodToDateExpr",
    "PriorPeriodExpr",
    "RatioExpr",
    "Registry",
    "RelationshipConfig",
    "RewriteStep",
    "RollingExpr",
    "ScopedAggregateExpr",
    "SemanticDag",
    "SemanticDagNode",
    "SemanticExpr",
    "SemanticLayerError",
    "SqlBinary",
    "SqlCall",
    "SqlCase",
    "SqlCaseWhen",
    "SqlCast",
    "SqlCte",
    "SqlField",
    "SqlIdentifier",
    "SqlIn",
    "SqlIsNull",
    "SqlJoin",
    "SqlLiteral",
    "SqlOrder",
    "SqlOrderTerm",
    "SqlSelect",
    "SqlTableRef",
    "SqlWindow",
    "SqlWithinGroup",
    "TemporalRoleConfig",
    "_add_grain",
    "_aggregation_expr",
    "_all_metric_predicates",
    "_bind_measure",
    "_bind_scoped_aggregate",
    "_bound_filter_clauses",
    "_bound_metric_predicates",
    "_calendar_fill_binding",
    "_can_project_entity_key_from_source",
    "_candidate_root_summary",
    "_collect_conversion_exprs",
    "_collect_measure_refs",
    "_column_ref",
    "_combine_temporal_roles",
    "_compile_post_expr",
    "_compile_query_sql_ast",
    "_config_expr_to_sql",
    "_conversion_dimension_binding",
    "_conversion_dimension_paths",
    "_conversion_dimension_requires_binding",
    "_conversion_entity_key_fields",
    "_conversion_event_cte",
    "_conversion_leaf_cte",
    "_conversion_predicate_set_ctes",
    "_conversion_supported",
    "_converted_side_group_dimensions",
    "_date_key",
    "_default_temporal_role",
    "_deterministic_ancestor_grain",
    "_dimension_index",
    "_direct_dimension_source_expr",
    "_direct_entity_key_source_expr",
    "_entity_determines",
    "_entity_in_terms_of_rewrite_supported",
    "_entity_index",
    "_entity_key_dimension_ids",
    "_entity_key_expr",
    "_expanded_predicate_range",
    "_expr_compatible_temporal_roles",
    "_expr_contains_cumulative",
    "_expr_leaf_temporal_role_sets",
    "_expr_requires_dense_series",
    "_expression_alias",
    "_expression_root_entity",
    "_floor_to_grain",
    "_format_time_literal",
    "_freeze_payload",
    "_infer_root_entity_for_distinct_query",
    "_is_grain_boundary",
    "_join_condition",
    "_joins_for_paths",
    "_last_token",
    "_leaf_path_selections",
    "_leaf_time_role",
    "_measure_count_distinct_key_columns",
    "_measure_index",
    "_measure_leaf_select",
    "_measure_required_entities",
    "_metric_filter_alias",
    "_namespace_sql_select",
    "_object_comparison_metadata",
    "_object_default_query_temporal_role",
    "_object_kind",
    "_object_label",
    "_parse_public_expr",
    "_parse_time_literal",
    "_path_can_project_count_key_from_rewrite_anchor",
    "_path_has_temporal_validity",
    "_path_reverse_count_distinct_safe",
    "_path_selection",
    "_plural",
    "_predicate_context_entity_candidates",
    "_predicate_ctes_and_join",
    "_predicate_input_root_entity",
    "_predicate_metric_label",
    "_predicate_scope",
    "_predicate_sql_names",
    "_predicate_time_alias",
    "_predicate_time_join_expr",
    "_predicate_time_spec",
    "_predicate_where_condition",
    "_preferred_path",
    "_preferred_root_order",
    "_public_time_spec",
    "_query_key_aliases",
    "_query_metric_predicates",
    "_query_requires_dense_series",
    "_query_target_entities",
    "_range_crosses_window_boundary",
    "_recipe_compatible_temporal_roles",
    "_recipe_index",
    "_reduced_context_entities",
    "_relationship_index",
    "_requires_query_time",
    "_resolve_conversion_source",
    "_resolve_dimension_expr",
    "_resolve_expr_entity",
    "_root_path_summary",
    "_scoped_aggregate_filter_spec",
    "_scoped_predicate_expr_payload",
    "_semantic_dag_for_query",
    "_semantic_token",
    "_slug",
    "_split_column_ref",
    "_temporal_role_index",
    "_time_bound_relationship_ids",
    "_time_output_alias",
    "_unique_path_selections",
    "_unsupported_conversion",
    "_validate_measure_validity_windows",
    "_validate_metric_predicate_filter_envelope",
    "_validate_query_temporal_bindings",
    "_validate_restrictive_time_semantics",
    "_validate_rollup_safety",
    "_validate_where_value_type",
    "analyze_fanout",
    "attach_relation_ctes",
    "choose_path",
    "compile_query",
    "dialect_for_warehouse",
    "expr_kind",
    "expr_to_dict",
    "get_package_analysis",
    "lower_to_sql",
    "normalize_query",
    "plan_comparison_bundle",
    "plan_query",
    "relationship_contract_payload",
    "render_select_for_profile",
]


def _slug(value: str, *, fallback: str = "item") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    parts = [part for part in raw.split("_") if part]
    return "_".join(parts) or fallback


def _last_token(value: str) -> str:
    return str(value or "").split(".")[-1]


def _semantic_token(value: str, *, fallback: str = "item") -> str:
    raw_value = str(value or "")
    token = _last_token(value)
    if raw_value.startswith("entity.") and "_" in token:
        token = token.split("_", 1)[1]
    for prefix in ("jaffle_", "entity_", "metric_recipe_", "measure_"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
    return _slug(token, fallback=fallback)


def _plural(value: str) -> str:
    return value if value.endswith("s") else f"{value}s"


def _predicate_metric_label(predicate: MetricPredicateExpr) -> str:
    if isinstance(predicate.input, MetricRecipeRefExpr):
        return _semantic_token(predicate.input.metric_recipe, fallback="metric")
    payload = expr_to_dict(predicate.input)
    return _semantic_token(
        str(payload.get("metric", payload.get("measure", "metric"))), fallback="metric"
    )


def _predicate_sql_names(
    predicate: MetricPredicateExpr, index: int, scope: dict[str, Any]
) -> tuple[str, str]:
    metric_label = _predicate_metric_label(predicate)
    entity_label = _semantic_token(predicate.entity, fallback="entity")
    grain = _slug(str(dict(scope.get("time_spec") or {}).get("grain", "")), fallback="")
    grain_suffix = f"_{grain}" if grain else ""
    ordinal = f"_{index + 1}"
    source_name = f"{metric_label}_{entity_label}{grain_suffix}_source{ordinal}"
    set_name = f"qualified_{_plural(entity_label)}{grain_suffix}_by_{metric_label}{ordinal}"
    return source_name, set_name


def _object_kind(config: PackageConfig, object_id: str) -> str:
    if object_id in _measure_index(config):
        return "measure"
    if object_id in _recipe_index(config):
        return "metric"
    raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown semantic object '{object_id}'")


def _object_label(config: PackageConfig, object_id: str) -> str:
    measures = _measure_index(config)
    recipes = _recipe_index(config)
    if object_id in measures:
        return measures[object_id].label
    if object_id in recipes:
        return recipes[object_id].label
    raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown semantic object '{object_id}'")


def _object_default_query_temporal_role(config: PackageConfig, object_id: str) -> str:
    measures = _measure_index(config)
    recipes = _recipe_index(config)
    if object_id in measures:
        return _default_temporal_role(measures[object_id])
    recipe = recipes.get(object_id)
    if recipe is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown semantic object '{object_id}'")
    if recipe.temporal_role:
        return recipe.temporal_role
    if recipe.compatible_temporal_roles:
        return recipe.compatible_temporal_roles[0]
    query = normalize_query(
        {"version": 1, "select": [{"expression": {"metric": object_id}, "as": recipe.label}]}
    )
    compatible = sorted(_expr_compatible_temporal_roles(recipe.expression, config, query))
    return compatible[0] if compatible else ""


def _object_comparison_metadata(config: PackageConfig, object_id: str) -> dict[str, Any]:
    measures = _measure_index(config)
    recipes = _recipe_index(config)
    obj: Any
    if object_id in measures:
        obj = measures[object_id]
    elif object_id in recipes:
        obj = recipes[object_id]
    else:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown semantic object '{object_id}'")
    return {
        "comparison_family": str(getattr(obj, "comparison_family", "") or ""),
        "comparison_mode": str(getattr(obj, "comparison_mode", "") or ""),
        "comparison_peers": list(getattr(obj, "comparison_peers", []) or []),
        "clock_variants": list(getattr(obj, "clock_variants", []) or []),
        "preferred_companion_metrics": list(getattr(obj, "preferred_companion_metrics", []) or []),
    }


def _can_project_entity_key_from_source(
    source_entity: str, target_entity: str, config: PackageConfig
) -> bool:
    target = _entity_index(config).get(target_entity)
    if target is None:
        return False
    return all(
        _direct_entity_key_source_expr(source_entity, target_entity, key_col, config) is not None
        for key_col in list(target.key or [target.primary_key])
    )


def _validate_rollup_safety(bound_measures: Iterable[BoundMeasure], config: PackageConfig) -> None:
    measures = _measure_index(config)
    unsafe_aggregations = {"avg", "median", "percentile", "count_distinct"}
    rollup_hints_by_pair: dict[tuple[str, str], list[str]] = {}
    for rel in config.relationships:
        if not rel.rollup_safe_aggregations:
            continue
        rollup_hints_by_pair[(rel.source_entity, rel.target_entity)] = list(
            rel.rollup_safe_aggregations
        )
    for bound in bound_measures:
        measure = measures[bound.measure_id]
        if not measure.aggregation_entity or measure.aggregation_entity == measure.entity:
            continue
        aggregation = str(bound.aggregation or measure.default_aggregation or "").lower()
        allowed_raw = rollup_hints_by_pair.get((measure.entity, measure.aggregation_entity))
        allowed = {str(item).lower() for item in (allowed_raw or [])}
        sketch_safe = bool(measure.meta.get("rollup_sketch") or measure.meta.get("sketch"))
        if (aggregation in unsafe_aggregations and not sketch_safe) or (
            allowed and aggregation not in allowed and not sketch_safe
        ):
            raise SemanticLayerError(
                "ROLLUP_UNSAFE",
                f"Measure '{measure.id}' cannot be rolled from '{measure.entity}' to '{measure.aggregation_entity}' with aggregation '{aggregation}'",
                details={
                    "measure_id": measure.id,
                    "source_entity": measure.entity,
                    "aggregation_entity": measure.aggregation_entity,
                    "aggregation": aggregation,
                    "unsupported_construct": "non_additive_parent_rollup",
                    "why_invalid": "Non-additive aggregations cannot be safely re-aggregated across parent entities without sketch or rollup metadata.",
                    "missing_metadata_or_capability": "rollup_sketch or additive primitive",
                },
            )


def _measure_count_distinct_key_columns(measure: MeasureConfig, config: PackageConfig) -> list[str]:
    if measure.measure_class not in {"event_count", "distinct_population"}:
        return []
    if not isinstance(measure.expr, ColumnRefExpr):
        return []
    if measure.expr.entity or measure.expr.table:
        return []
    entity = _entity_index(config).get(measure.entity)
    if entity is None:
        return []
    key_cols = list(entity.key or [entity.primary_key])
    return key_cols if measure.expr.column in set(key_cols) else []


def _path_can_project_count_key_from_rewrite_anchor(
    *,
    start_entity: str,
    path: list[str],
    key_columns: list[str],
    config: PackageConfig,
) -> bool:
    relationships = _relationship_index(config)
    current = start_entity
    for rel_id in path:
        rel = relationships[rel_id]
        if current == rel.target_entity:
            target_to_source = dict(
                zip(
                    list(rel.target_columns or [rel.target_column]),
                    list(rel.source_columns or [rel.source_column]),
                    strict=True,
                )
            )
            return all(column in target_to_source for column in key_columns)
        if current == rel.source_entity:
            current = rel.target_entity
            continue
        return False
    return False


def _path_reverse_count_distinct_safe(
    *, start_entity: str, path: list[str], config: PackageConfig
) -> bool:
    relationships = _relationship_index(config)
    current = start_entity
    saw_reverse = False
    for rel_id in path:
        rel = relationships[rel_id]
        if current == rel.source_entity:
            current = rel.target_entity
            continue
        if current == rel.target_entity:
            saw_reverse = True
            reverse_safe = {
                str(item).lower() for item in list(rel.rollup_safe_aggregations_reverse or [])
            }
            if "count_distinct" not in reverse_safe:
                return False
            current = rel.source_entity
            continue
        return False
    return saw_reverse


def _entity_in_terms_of_rewrite_supported(
    *,
    measure: MeasureConfig,
    bound: BoundMeasure,
    selection: PathSelection,
    config: PackageConfig,
    query: NormalizedQuery,
) -> bool:
    if selection.purpose != "group_by":
        return False
    if selection.analysis.get("status") != "rewrite_required":
        return False
    if str(bound.aggregation or measure.default_aggregation or "").lower() != "count_distinct":
        return False
    if _bound_metric_predicates(bound):
        return False
    key_columns = _measure_count_distinct_key_columns(measure, config)
    if not key_columns:
        return False
    path = list(selection.chosen_path)
    return _path_can_project_count_key_from_rewrite_anchor(
        start_entity=measure.entity,
        path=path,
        key_columns=key_columns,
        config=config,
    ) and _path_reverse_count_distinct_safe(
        start_entity=measure.entity,
        path=path,
        config=config,
    )


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0].split(" ", 1)[0]


def _range_crosses_window_boundary(
    *, start: str, end: str, window_from: str, window_to: str
) -> bool:
    if window_from and start and start < window_from and (not end or end > window_from):
        return True
    return bool(window_to and (not start or start < window_to) and end and end > window_to)


def _validate_measure_validity_windows(
    bound_measures: Iterable[BoundMeasure], config: PackageConfig, query: NormalizedQuery
) -> None:
    if query.time is None:
        return
    start = _date_key(query.time.start)
    end = _date_key(query.time.end)
    if not start and not end:
        return
    measures = _measure_index(config)
    for bound in bound_measures:
        measure = measures[bound.measure_id]
        if str(measure.cross_window_policy or "caveat").lower() != "refuse":
            continue
        for window in measure.validity_windows:
            if _range_crosses_window_boundary(
                start=start,
                end=end,
                window_from=_date_key(window.from_),
                window_to=_date_key(window.to),
            ):
                raise SemanticLayerError(
                    "MEASURE_VALIDITY_BOUNDARY",
                    f"Measure '{measure.id}' crosses declared validity window '{window.semantics or window.from_ or window.to}'",
                    details={
                        "measure_id": measure.id,
                        "start": start,
                        "end": end,
                        "window": {
                            "from": window.from_,
                            "to": window.to,
                            "semantics": window.semantics,
                        },
                        "cross_window_policy": measure.cross_window_policy,
                        "recovery_hints": [
                            {
                                "kind": "split_time_window",
                                "message": "Run separate queries for each declared measure-validity window.",
                                "windows": [
                                    {
                                        "start": window.from_,
                                        "end": window.to,
                                        "semantics": window.semantics,
                                    },
                                ],
                            }
                        ],
                    },
                )


def _semantic_dag_for_query(query: NormalizedQuery, config: PackageConfig) -> SemanticDag:
    nodes: list[SemanticDagNode] = []
    outputs: dict[str, str] = {}
    counter = 0

    def _next_id(kind: str) -> str:
        nonlocal counter
        counter += 1
        return f"{kind}_{counter}"

    def _walk(expr: SemanticExpr, *, label: str = "") -> str:
        if isinstance(expr, MeasureRefExpr):
            node_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="SourceLeaf",
                    label=label or expr.measure,
                    details={
                        "measure": expr.measure,
                        "aggregation": expr.aggregation
                        or (measure.default_aggregation if measure else ""),
                        "entity": measure.entity if measure else "",
                        "temporal_roles": list(
                            measure.compatible_temporal_roles if measure else []
                        ),
                    },
                )
            )
            return node_id
        if isinstance(expr, AggregateExpr):
            node_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            details = {
                "measure": expr.measure,
                "aggregation": expr.aggregation or (measure.default_aggregation if measure else ""),
                "entity": measure.entity if measure else "",
                "temporal_roles": list(measure.compatible_temporal_roles if measure else []),
                "filter": dict(expr.filter),
            }
            nodes.append(
                SemanticDagNode(
                    id=node_id, kind="SourceLeaf", label=label or expr.measure, details=details
                )
            )
            return node_id
        if isinstance(expr, ScopedAggregateExpr):
            source_id = _next_id("source_leaf")
            measure = _measure_index(config).get(expr.measure)
            nodes.append(
                SemanticDagNode(
                    id=source_id,
                    kind="SourceLeaf",
                    label=label or expr.measure,
                    details={
                        "measure": expr.measure,
                        "aggregation": expr.aggregation
                        or (measure.default_aggregation if measure else ""),
                        "entity": measure.entity if measure else "",
                        "where": [dict(item) for item in expr.where],
                    },
                )
            )
            current = source_id
            for predicate in expr.predicates:
                predicate_id = _next_id("predicate_set")
                nodes.append(
                    SemanticDagNode(
                        id=predicate_id,
                        kind="PredicateSet",
                        label=str(
                            predicate.get("metric", predicate.get("metric", "metric_predicate"))
                        ),
                        inputs=[],
                        details=dict(predicate),
                    )
                )
                semi_id = _next_id("scoped_semijoin")
                nodes.append(
                    SemanticDagNode(
                        id=semi_id,
                        kind="ScopedSemiJoin",
                        inputs=[current, predicate_id],
                        details={
                            "entity": predicate.get("entity", ""),
                            "time_alignment": predicate.get("time_alignment", ""),
                        },
                    )
                )
                current = semi_id
            return current
        if isinstance(expr, MetricRecipeRefExpr):
            recipe = _recipe_index(config).get(expr.metric_recipe)
            if recipe is not None:
                child = _walk(recipe.expression, label=expr.metric_recipe)
                node_id = _next_id("metric_ref")
                nodes.append(
                    SemanticDagNode(
                        id=node_id,
                        kind="MetricRef",
                        label=expr.metric_recipe,
                        inputs=[child],
                        details={"metric": expr.metric_recipe},
                    )
                )
                return node_id
            node_id = _next_id("metric_ref")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="MetricRef",
                    label=expr.metric_recipe,
                    details={"metric": expr.metric_recipe},
                )
            )
            return node_id
        if isinstance(expr, RatioExpr):
            left = _walk(expr.numerator, label=f"{label}_numerator")
            right = _walk(expr.denominator, label=f"{label}_denominator")
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=[left, right],
                    details={"op": "ratio"},
                )
            )
            return node_id
        if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
            left = _walk(expr.left, label=label)
            right = _walk(expr.right, label=label)
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=[left, right],
                    details={"op": expr.op},
                )
            )
            return node_id
        if isinstance(expr, EntityValueExpr):
            child = _walk(expr.input, label=label)
            node_id = _next_id("entity_rollup")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="EntityRollup",
                    label=label,
                    inputs=[child],
                    details={
                        "entity": expr.entity,
                        "value_filters": [dict(item) for item in expr.where],
                    },
                )
            )
            return node_id
        if isinstance(expr, DistributionExpr):
            child = _walk(expr.over, label=label)
            node_id = _next_id("distribution")
            dist_details: dict[str, Any] = {
                "function": expr.function,
                "parameters": dict(expr.parameters),
            }
            if expr.p is not None:
                dist_details["p"] = expr.p
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="DistributionAggregate",
                    label=label,
                    inputs=[child],
                    details=dist_details,
                )
            )
            return node_id
        if isinstance(expr, (BooleanExpr, CallExpr)):
            child_ids = [_walk(arg, label=label) for arg in expr.args]
            node_id = _next_id("post_calc")
            nodes.append(
                SemanticDagNode(
                    id=node_id,
                    kind="PostAggregateCalc",
                    label=label,
                    inputs=child_ids,
                    details={"op": getattr(expr, "op", getattr(expr, "name", ""))},
                )
            )
            return node_id
        node_id = _next_id(expr_kind(expr).lower())
        nodes.append(
            SemanticDagNode(
                id=node_id,
                kind=expr_kind(expr),
                label=label,
                details={"expression": expr_to_dict(expr)},
            )
        )
        return node_id

    for item in query.select:
        if item.expression is not None:
            outputs[item.as_] = _walk(item.expression, label=item.as_)
    if len(outputs) > 1:
        nodes.append(
            SemanticDagNode(
                id="multi_fact_align",
                kind="MultiFactAlign",
                inputs=list(outputs.values()),
                details={"keys": [*query.group_by, _time_output_alias(query.time)]},
            )
        )
    nodes.append(
        SemanticDagNode(
            id="final_project",
            kind="FinalProject",
            inputs=list(outputs.values()),
            details={"outputs": dict(outputs)},
        )
    )
    return SemanticDag(version=1, nodes=nodes, outputs=outputs)


def _unsupported_conversion(expr: ConversionExpr) -> SemanticLayerError:
    return SemanticLayerError(
        "CONVERSION_NOT_SUPPORTED",
        "Event-pair conversion expressions are recognized but SQL lowering is not implemented yet",
        details={
            "entity": expr.entity,
            "window": {"unit": expr.window_unit, "value": expr.window_value},
            "matching_mode": expr.matching_mode,
            "constant_properties": list(expr.constant_properties),
            "dimension_bindings": {
                key: dict(value) for key, value in dict(expr.dimension_bindings or {}).items()
            },
        },
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


def _bound_metric_predicates(bound: BoundMeasure) -> list[MetricPredicateExpr]:
    filter_spec = dict(bound.filter_spec or {})
    clauses = list(filter_spec.get("all", []) or [])
    predicates: list[MetricPredicateExpr] = []
    for clause in clauses:
        raw_expr = clause.get("expression")
        if raw_expr is None:
            continue
        expr = _parse_public_expr(dict(raw_expr))
        if not isinstance(expr, MetricPredicateExpr):
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "Only metric_predicate expressions are supported in aggregate filters",
            )
        predicates.append(expr)
    return predicates


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


_NUMERIC_DATA_TYPES = {"integer", "number"}


def _validate_where_value_type(dim, item) -> None:
    """Catch obvious type mismatches in `where` clauses at validate time
    so users see a structured error instead of a raw warehouse conversion
    error. Only the most common mismatches are checked: numeric value
    against string dim, string value against numeric dim.

    Per-dim `data_type` may be empty when the catalog hasn't classified
    the column; in that case skip the check (nothing to enforce).
    """
    op = str(item.op or "").upper()
    values = list(item.value or []) if op in {"IN", "NOT IN"} else [item.value]
    data_type = str(getattr(dim, "data_type", "") or "").lower()
    if not data_type:
        return
    for value in values:
        if value is None:
            continue
        if data_type in _NUMERIC_DATA_TYPES:
            if isinstance(value, (bool, int, float)):
                continue
            # A digit-string is acceptable; warehouses cast it cleanly.
            if isinstance(value, str) and value.strip().lstrip("-").replace(".", "", 1).isdigit():
                continue
            raise SemanticLayerError(
                "INVALID_QUERY",
                (
                    f"where filter on dimension {dim.id!r} expects a numeric value but "
                    f"got {value!r} (type {type(value).__name__})."
                ),
                details={
                    "dimension": dim.id,
                    "data_type": data_type,
                    "value": value,
                    "op": item.op,
                },
            )
        if data_type == "string":
            if isinstance(value, (str,)):
                continue
            if isinstance(value, (bool, int, float)):
                raise SemanticLayerError(
                    "INVALID_QUERY",
                    (
                        f"where filter on dimension {dim.id!r} expects a string value but "
                        f"got {value!r} (type {type(value).__name__})."
                    ),
                    details={
                        "dimension": dim.id,
                        "data_type": data_type,
                        "value": value,
                        "op": item.op,
                    },
                )
        if data_type == "boolean":
            if isinstance(value, bool):
                continue
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                continue
            raise SemanticLayerError(
                "INVALID_QUERY",
                (
                    f"where filter on dimension {dim.id!r} expects a boolean value but "
                    f"got {value!r} (type {type(value).__name__})."
                ),
                details={
                    "dimension": dim.id,
                    "data_type": data_type,
                    "value": value,
                    "op": item.op,
                },
            )


def _validate_metric_predicate_filter_envelope(item: dict[str, Any]) -> None:
    if str(item.get("op", "=")).strip() == "=" and item.get("value") is True:
        return
    raise SemanticLayerError(
        "INVALID_METRIC_FILTER",
        "metric_predicate filters must use op '=' and value true; put the threshold inside expression.op/expression.value",
        details={"op": item.get("op", "="), "value": item.get("value")},
    )


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


def _path_selection(
    *,
    config: PackageConfig,
    query: NormalizedQuery,
    start_entity: str,
    target_entity: str,
    preference: str,
    purpose: str,
) -> PathSelection | None:
    if target_entity == start_entity:
        return None
    chosen, candidates = _preferred_path(
        config, start=start_entity, target=target_entity, preference=preference
    )
    analysis = analyze_fanout(
        config,
        start_entity,
        chosen,
        time_bound_relationships=_time_bound_relationship_ids(query, config),
    )
    return PathSelection(
        target_entity=target_entity,
        purpose=purpose,
        chosen_path=list(chosen),
        candidate_paths=[list(path) for path in candidates],
        analysis=analysis,
    )


def _predicate_where_condition(op: str, value: Any) -> Any:
    alias_ref = SqlIdentifier(parts=["predicate_source", "__predicate_value"])
    return build_filter_condition(alias_ref, op, value, path="metric_predicate")


def _inline_threshold_cte_and_where(
    *,
    predicate: Any,
    set_name: str,
    source_name: str,
    config: PackageConfig,
) -> tuple[list[Any], list[Any], Any]:
    """Inline-threshold materialisation for metric_predicate.

    When ``predicate.value`` is a ``{kind: "percentile", p, measure?}``
    dict (validated at parse time in ``expressions.py``), this helper
    builds a one-row threshold CTE alongside the predicate-source CTE
    and a CROSS JOIN so the WHERE clause references the threshold
    column instead of a literal scalar. Both predicate-CTE builders
    (``compiler._predicate_ctes_and_join`` and
    ``sql_lowering._minimal_predicate_set_ctes``) call this so the
    behaviour stays consistent across both paths.

    Returns ``(extra_ctes, extra_joins, where_condition)``. When
    ``predicate.value`` is a literal the helper falls back to the
    existing ``_predicate_where_condition`` and returns empty lists
    for the CTEs / joins.
    """
    value = predicate.value
    if not (isinstance(value, dict) and str(value.get("kind", "") or "") == "percentile"):
        return [], [], _predicate_where_condition(predicate.op, value)

    # Validate measure compatibility: the optional ``measure`` key in
    # the threshold dict must either be absent or match the predicate's
    # input measure. A cross-measure threshold would require a
    # different source CTE — defer that case with a structured error
    # rather than emit subtly-wrong SQL.
    input_payload = expr_to_dict(predicate.input)
    input_measure = str(input_payload.get("measure", "") or "")
    requested_measure = str(value.get("measure", "") or "")
    if requested_measure and input_measure and requested_measure != input_measure:
        raise SemanticLayerError(
            "INVALID_METRIC_PREDICATE",
            (
                f"metric_predicate inline percentile threshold measure "
                f"{requested_measure!r} differs from the predicate's "
                f"input measure {input_measure!r}. Inline thresholds "
                "must share the predicate's measure today. Compute the "
                "cross-measure threshold separately and pass the result "
                "as a literal."
            ),
            details={
                "predicate_input_measure": input_measure,
                "threshold_measure": requested_measure,
            },
        )
    threshold_p = float(value["p"])
    threshold_name = f"{set_name}_threshold"
    dialect = dialect_for_warehouse(config.package.warehouse)
    threshold_expr = dialect.percentile_cont(
        SqlIdentifier(parts=["predicate_source", "__predicate_value"]),
        threshold_p,
    )
    threshold_cte = SqlCte(
        name=threshold_name,
        query=SqlSelect(
            select=[SqlField(threshold_expr, "__threshold")],
            from_table=SqlTableRef(name=source_name, alias="predicate_source"),
        ),
    )
    cross_join = SqlJoin(
        join_type="CROSS",
        table=SqlTableRef(name=threshold_name, alias="predicate_threshold"),
        on=None,
    )
    where_condition = SqlBinary(
        SqlIdentifier(parts=["predicate_source", "__predicate_value"]),
        str(predicate.op),
        SqlIdentifier(parts=["predicate_threshold", "__threshold"]),
    )
    return [threshold_cte], [cross_join], where_condition


def _predicate_time_alias(temporal_role: str, grain: str) -> str:
    return temporal_role if not grain else f"{temporal_role}__{grain}"


def _deterministic_ancestor_grain(outer_grain: str, predicate_grain: str) -> bool:
    outer = str(outer_grain or "").lower()
    predicate = str(predicate_grain or "").lower()
    if not outer or not predicate:
        return False
    if outer == predicate:
        return True
    return (outer, predicate) in {
        ("day", "week"),
        ("day", "month"),
        ("day", "quarter"),
        ("day", "year"),
        ("month", "quarter"),
        ("month", "year"),
        ("quarter", "year"),
    }


def _parse_time_literal(value: Any) -> datetime:
    text = str(value).strip()
    if not text:
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE", "Predicate time range requires a non-empty timestamp"
        )
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE", f"Unsupported predicate time literal '{value}'"
        ) from exc


def _floor_to_grain(dt: datetime, grain: str) -> datetime:
    grain_norm = str(grain).lower()
    if grain_norm == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain_norm == "week":
        start = dt - timedelta(days=dt.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain_norm == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if grain_norm == "quarter":
        month = ((dt.month - 1) // 3) * 3 + 1
        return dt.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if grain_norm == "year":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise SemanticLayerError("PREDICATE_GRAIN_UNSAFE", f"Unsupported predicate grain '{grain}'")


def _add_grain(dt: datetime, grain: str) -> datetime:
    grain_norm = str(grain).lower()
    if grain_norm == "day":
        return dt + timedelta(days=1)
    if grain_norm == "week":
        return dt + timedelta(weeks=1)
    if grain_norm == "month":
        month = dt.month + 1
        year = dt.year
        if month > 12:
            month = 1
            year += 1
        return dt.replace(year=year, month=month, day=1)
    if grain_norm == "quarter":
        month = dt.month + 3
        year = dt.year
        while month > 12:
            month -= 12
            year += 1
        return dt.replace(year=year, month=month, day=1)
    if grain_norm == "year":
        return dt.replace(year=dt.year + 1, month=1, day=1)
    raise SemanticLayerError("PREDICATE_GRAIN_UNSAFE", f"Unsupported predicate grain '{grain}'")


def _is_grain_boundary(dt: datetime, grain: str) -> bool:
    return dt == _floor_to_grain(dt, grain)


def _format_time_literal(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def _expanded_predicate_range(start: Any, end: Any, grain: str) -> tuple[Any, Any]:
    expanded_start = start
    expanded_end = end
    if start is not None:
        expanded_start = _format_time_literal(_floor_to_grain(_parse_time_literal(start), grain))
    if end is not None:
        end_dt = _parse_time_literal(end)
        expanded_end = _format_time_literal(
            end_dt
            if _is_grain_boundary(end_dt, grain)
            else _add_grain(_floor_to_grain(end_dt, grain), grain)
        )
    return expanded_start, expanded_end


def _predicate_input_root_entity(predicate: MetricPredicateExpr, config: PackageConfig) -> str:
    return _expression_root_entity(predicate.input, config)


def _predicate_context_entity_candidates(
    predicate: MetricPredicateExpr, query: NormalizedQuery, config: PackageConfig
) -> list[str]:
    dimensions = _dimension_index(config)
    candidates: list[str] = []
    for dim_id in list(query.group_by or []):
        dim = dimensions[dim_id]
        if dim.entity == predicate.entity or dim.entity in candidates:
            continue
        candidates.append(dim.entity)
    return candidates


def _entity_determines(
    *,
    config: PackageConfig,
    source_entity: str,
    target_entity: str,
    preference: str,
    time_bound_relationships: set[str],
) -> bool:
    if source_entity == target_entity:
        return True
    try:
        chosen, candidates = _preferred_path(
            config, start=source_entity, target=target_entity, preference=preference
        )
    except SemanticLayerError:
        return False
    if len(candidates) != 1:
        return False
    try:
        analysis = analyze_fanout(
            config, source_entity, chosen, time_bound_relationships=time_bound_relationships
        )
    except SemanticLayerError:
        return False
    return analysis.get("status") == "ok"


def _reduced_context_entities(
    *,
    config: PackageConfig,
    entities: list[str],
    query: NormalizedQuery,
    time_bound_relationships: set[str],
) -> list[str]:
    retained: list[str] = []
    for entity_id in entities:
        redundant = False
        for other in entities:
            if other == entity_id:
                continue
            if _entity_determines(
                config=config,
                source_entity=other,
                target_entity=entity_id,
                preference=query.path_policy.preference,
                time_bound_relationships=time_bound_relationships,
            ):
                redundant = True
                break
        if not redundant and entity_id not in retained:
            retained.append(entity_id)
    return retained


def _path_has_temporal_validity(path: list[str], config: PackageConfig) -> bool:
    relationships = _relationship_index(config)
    return any(
        bool(relationships.get(rel_id) and relationships[rel_id].temporal_validity)
        for rel_id in list(path or [])
    )


def _predicate_time_spec(
    predicate: MetricPredicateExpr, query: NormalizedQuery, config: PackageConfig
) -> dict[str, Any] | None:
    if predicate.scope_mode == "entity_only":
        if predicate.time_grain:
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                "metric_predicate time_grain requires contextual scope with query.time",
            )
        if predicate.time_alignment in {"query_window", "rolling_window_in_period"}:
            if query.time is None:
                raise SemanticLayerError(
                    "PREDICATE_GRAIN_UNSAFE",
                    "entity_only metric_predicate query-window alignment requires query.time",
                )
            if query.time.start is None or query.time.end is None:
                raise SemanticLayerError(
                    "PREDICATE_GRAIN_UNSAFE",
                    "entity_only metric_predicate query-window alignment requires bounded query.time start and end",
                )
            compatible_roles = _expr_compatible_temporal_roles(predicate.input, config, query)
            predicate_temporal_role = query.time.temporal_role
            if query.time.temporal_role not in compatible_roles:
                predicate_temporal_role = next(iter(sorted(compatible_roles)), "")
                if not predicate_temporal_role:
                    raise SemanticLayerError(
                        "PREDICATE_GRAIN_UNSAFE",
                        "Metric predicate input does not expose a compatible temporal role",
                        details={
                            "compatible": sorted(compatible_roles),
                            "predicate": expr_to_dict(predicate),
                        },
                    )
            return {
                "temporal_role": predicate_temporal_role,
                "output_temporal_role": "",
                "grain": "",
                "start": query.time.start,
                "end": query.time.end,
                "calendar_id": query.time.calendar_id,
                "entity_only_window": True,
                "time_alignment": predicate.time_alignment,
                "window": {"unit": predicate.window_unit, "value": predicate.window_value}
                if predicate.window_unit and predicate.window_value
                else {},
            }
        return None
    if query.time is None:
        return None
    compatible_roles = _expr_compatible_temporal_roles(predicate.input, config, query)
    predicate_temporal_role = query.time.temporal_role
    if query.time.temporal_role not in compatible_roles:
        predicate_temporal_role = next(iter(sorted(compatible_roles)), "")
        if not predicate_temporal_role:
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                "Metric predicate input does not expose a compatible temporal role",
                details={
                    "compatible": sorted(compatible_roles),
                    "predicate": expr_to_dict(predicate),
                },
            )
    outer_grain = str(query.time.grain or "").lower()
    predicate_grain = str(predicate.time_grain or outer_grain or "").lower()
    if predicate.time_grain:
        if not outer_grain:
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                "Explicit predicate time_grain requires an outer query grain",
            )
        if not _deterministic_ancestor_grain(outer_grain, predicate_grain):
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                f"Predicate grain '{predicate_grain}' is not a deterministic ancestor of outer grain '{outer_grain}'",
                details={"outer_grain": outer_grain, "predicate_grain": predicate_grain},
            )
    start = query.time.start
    end = query.time.end
    if outer_grain and predicate_grain and predicate_grain != outer_grain:
        start, end = _expanded_predicate_range(start, end, predicate_grain)
    return {
        "temporal_role": predicate_temporal_role,
        "output_temporal_role": query.time.temporal_role,
        "grain": predicate_grain,
        "start": start,
        "end": end,
        "calendar_id": query.time.calendar_id,
    }


def _public_time_spec(time_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(time_spec or {}).items()
        if key in {"temporal_role", "grain", "start", "end", "fill", "calendar_id"}
    }


def _predicate_time_join_expr(
    *,
    source_entity: str,
    role_id: str,
    grain: str,
    config: PackageConfig,
) -> Any:
    roles = _temporal_role_index(config)
    dimensions = _dimension_index(config)
    entities = _entity_index(config)
    role = roles.get(role_id)
    if role is None:
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE", f"Unknown predicate temporal role '{role_id}'"
        )
    dim = dimensions[role.dimension]
    raw_expr = _column_ref(entities[dim.entity].table, dim.column)
    return (
        dialect_for_warehouse(config.package.warehouse).date_trunc(grain, raw_expr)
        if grain
        else raw_expr
    )


def _predicate_scope(
    predicate: MetricPredicateExpr,
    *,
    plan: LogicalPlan,
    measure_plan: MeasurePlan | None = None,
    config: PackageConfig,
) -> dict[str, Any]:
    query = normalize_query(plan.query)
    input_root = _predicate_input_root_entity(predicate, config)
    time_spec = _predicate_time_spec(predicate, query, config)
    time_bound_relationships = (
        {row.id for row in config.relationships if row.temporal_validity}
        if time_spec is not None
        else set()
    )
    if predicate.entity != input_root:
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=input_root,
            target_entity=predicate.entity,
            preference=query.path_policy.preference,
            purpose="metric_predicate_entity",
        )
        if selection is not None and selection.analysis.get("status") != "ok":
            raise SemanticLayerError(
                "PREDICATE_SCOPE_UNSAFE",
                "metric_predicate entity requires an unsafe or unsupported path from the predicate input root",
                details={
                    "entity": predicate.entity,
                    "input_root": input_root,
                    "analysis": selection.analysis,
                    "path": selection.chosen_path,
                },
            )
        if (
            selection is not None
            and time_spec is None
            and _path_has_temporal_validity(selection.chosen_path, config)
        ):
            raise SemanticLayerError(
                "PREDICATE_SCOPE_UNSAFE",
                "metric_predicate entity requires a time anchor to traverse a temporal-validity path",
                details={
                    "entity": predicate.entity,
                    "input_root": input_root,
                    "path": selection.chosen_path,
                },
            )
    context_entities = []
    if predicate.scope_mode == "contextual":
        candidate_entities = _predicate_context_entity_candidates(predicate, query, config)
        context_entities = _reduced_context_entities(
            config=config,
            entities=candidate_entities,
            query=query,
            time_bound_relationships=time_bound_relationships,
        )
        for entity_id in context_entities:
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=input_root,
                target_entity=entity_id,
                preference=query.path_policy.preference,
                purpose="metric_predicate_context",
            )
            if selection is not None and selection.analysis.get("status") != "ok":
                raise SemanticLayerError(
                    "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE",
                    f"Predicate input cannot safely inherit context entity '{entity_id}'",
                    details={
                        "entity": entity_id,
                        "input_root": input_root,
                        "analysis": selection.analysis,
                        "path": selection.chosen_path,
                    },
                )
            if (
                selection is not None
                and time_spec is None
                and _path_has_temporal_validity(selection.chosen_path, config)
            ):
                raise SemanticLayerError(
                    "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE",
                    f"Predicate input requires a time anchor to inherit context entity '{entity_id}'",
                    details={
                        "entity": entity_id,
                        "input_root": input_root,
                        "path": selection.chosen_path,
                    },
                )
        for item in list(query.where or []):
            dim = _dimension_index(config)[item.field]
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=input_root,
                target_entity=dim.entity,
                preference=query.path_policy.preference,
                purpose="metric_predicate_filter",
            )
            if selection is not None and selection.analysis.get("status") != "ok":
                raise SemanticLayerError(
                    "PREDICATE_FILTER_INCOMPATIBLE",
                    f"Predicate input cannot safely inherit filter on '{item.field}'",
                    details={
                        "dimension": item.field,
                        "entity": dim.entity,
                        "analysis": selection.analysis,
                        "path": selection.chosen_path,
                    },
                )
            if (
                selection is not None
                and time_spec is None
                and _path_has_temporal_validity(selection.chosen_path, config)
            ):
                raise SemanticLayerError(
                    "PREDICATE_FILTER_INCOMPATIBLE",
                    f"Predicate input requires a time anchor to inherit filter on '{item.field}'",
                    details={
                        "dimension": item.field,
                        "entity": dim.entity,
                        "path": selection.chosen_path,
                    },
                )
    group_by_dims = [*_entity_key_dimension_ids(predicate.entity, config)]
    for entity_id in context_entities:
        group_by_dims.extend(_entity_key_dimension_ids(entity_id, config))
    group_by_dims = list(dict.fromkeys(group_by_dims))
    time_alias = (
        ""
        if time_spec is not None and time_spec.get("entity_only_window")
        else _predicate_time_alias(
            str(time_spec.get("output_temporal_role") or time_spec["temporal_role"]),
            str(time_spec["grain"]),
        )
        if time_spec is not None
        else ""
    )
    source_time_alias = (
        _predicate_time_alias(str(time_spec["temporal_role"]), str(time_spec["grain"]))
        if time_spec is not None
        else ""
    )
    return {
        "input_root": input_root,
        "group_by_dims": group_by_dims,
        "context_entities": context_entities,
        "time_spec": time_spec,
        "time_alias": time_alias,
        "source_time_alias": source_time_alias,
        "filter_items": list(query.where or []) if predicate.scope_mode == "contextual" else [],
    }


def _predicate_ctes_and_join(
    predicate: MetricPredicateExpr,
    *,
    index: int,
    plan: LogicalPlan,
    measure_plan: MeasurePlan,
    config: PackageConfig,
) -> tuple[list[SqlCte], SqlJoin]:
    entity_cfg = _entity_index(config).get(predicate.entity)
    if entity_cfg is None:
        raise SemanticLayerError(
            "PREDICATE_ENTITY_REQUIRED", f"Unknown predicate entity '{predicate.entity}'"
        )
    scope = _predicate_scope(predicate, plan=plan, measure_plan=measure_plan, config=config)
    key_dim_map = list(
        zip(
            entity_cfg.key,
            _entity_key_dimension_ids(predicate.entity, config),
            strict=True,
        )
    )
    mini_query = {
        "version": 1,
        "select": [{"expression": expr_to_dict(predicate.input), "as": "__predicate_value"}],
        "group_by": list(scope["group_by_dims"]),
    }
    predicate_where_items = [asdict(item) for item in scope["filter_items"]]
    if scope["time_spec"] is not None and scope["time_spec"].get("entity_only_window"):
        role = _temporal_role_index(config)[str(scope["time_spec"]["temporal_role"])]
        if scope["time_spec"].get("start") is not None:
            predicate_where_items.append(
                {"dimension": role.dimension, "op": ">=", "value": scope["time_spec"]["start"]}
            )
        if scope["time_spec"].get("end") is not None:
            predicate_where_items.append(
                {"dimension": role.dimension, "op": "<", "value": scope["time_spec"]["end"]}
            )
    if scope["filter_items"] or predicate_where_items:
        mini_query["where"] = predicate_where_items
    if scope["time_spec"] is not None and not scope["time_spec"].get("entity_only_window"):
        mini_query["time"] = _public_time_spec(scope["time_spec"])
    predicate_sql = _compile_query_sql_ast(config, mini_query)
    source_name, set_name = _predicate_sql_names(predicate, index, scope)
    source_cte = SqlCte(
        name=source_name, query=_namespace_sql_select(predicate_sql, f"{source_name}__")
    )
    projected_keys = [dim_id for dim_id in scope["group_by_dims"]]
    source_group_keys = [dim_id for dim_id in scope["group_by_dims"]]
    select_fields = [
        SqlField(SqlIdentifier(parts=["predicate_source", dim_id]), dim_id)
        for dim_id in projected_keys
    ]
    if scope["time_alias"]:
        select_fields.append(
            SqlField(
                SqlIdentifier(parts=["predicate_source", scope["source_time_alias"]]),
                scope["time_alias"],
            )
        )
        projected_keys.append(scope["time_alias"])
        source_group_keys.append(scope["source_time_alias"])
    # `source_cte` already produces one row per key combination; the outer
    # GROUP BY would just re-group identical rows. SELECT DISTINCT expresses
    # "set of qualifying ids" idiomatically.
    threshold_ctes, threshold_joins, where_condition = _inline_threshold_cte_and_where(
        predicate=predicate,
        set_name=set_name,
        source_name=source_name,
        config=config,
    )
    set_query = SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=source_name, alias="predicate_source"),
        joins=threshold_joins,
        where=[where_condition],
        distinct=True,
    )
    _ = source_group_keys  # retained for naming clarity; no GROUP BY emitted
    set_cte = SqlCte(name=set_name, query=set_query)
    predicate_table = entity_cfg.table
    direct_predicate_keys = [
        _direct_entity_key_source_expr(
            measure_plan.source_entity, predicate.entity, key_col, config
        )
        for key_col in entity_cfg.key
    ]
    can_join_predicate_directly = all(expr is not None for expr in direct_predicate_keys)
    if (
        predicate.entity != measure_plan.source_entity
        and predicate.entity
        not in {selection.target_entity for selection in measure_plan.path_selections}
        and not can_join_predicate_directly
    ):
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "metric_predicate entity is not reachable from the outer measure",
            details={"entity": predicate.entity, "measure": measure_plan.bound_measure.measure_id},
        )
    outer_key_expr = (
        direct_predicate_keys[0]
        if can_join_predicate_directly
        else _column_ref(predicate_table, entity_cfg.key[0])
    )
    assert outer_key_expr is not None  # can_join_predicate_directly guarantees non-None
    join_condition: Any = SqlBinary(
        outer_key_expr,
        "=",
        SqlIdentifier(parts=[set_name, key_dim_map[0][1]]),
    )
    for index_key, (key_col, dim_id) in enumerate(key_dim_map[1:], start=1):
        outer_key_expr = (
            direct_predicate_keys[index_key]
            if can_join_predicate_directly
            else _column_ref(predicate_table, key_col)
        )
        assert outer_key_expr is not None  # can_join_predicate_directly guarantees non-None
        join_condition = SqlBinary(
            join_condition,
            "AND",
            SqlBinary(outer_key_expr, "=", SqlIdentifier(parts=[set_name, dim_id])),
        )
    for entity_id in scope["context_entities"]:
        for _key_col, dim_id in zip(
            _entity_index(config)[entity_id].key,
            _entity_key_dimension_ids(entity_id, config),
            strict=True,
        ):
            dim = _dimension_index(config)[dim_id]
            join_condition = SqlBinary(
                join_condition,
                "AND",
                SqlBinary(
                    _column_ref(_entity_index(config)[dim.entity].table, dim.column),
                    "=",
                    SqlIdentifier(parts=[set_name, dim_id]),
                ),
            )
    if scope["time_spec"] is not None and scope["time_alias"]:
        query = normalize_query(plan.query)
        outer_temporal_role = _leaf_time_role(measure_plan.bound_measure, query, config)
        join_condition = SqlBinary(
            join_condition,
            "AND",
            SqlBinary(
                _predicate_time_join_expr(
                    source_entity=measure_plan.source_entity,
                    role_id=outer_temporal_role,
                    grain=str(scope["time_spec"]["grain"]),
                    config=config,
                ),
                "=",
                SqlIdentifier(parts=[set_name, scope["time_alias"]]),
            ),
        )
    return [source_cte, *threshold_ctes, set_cte], SqlJoin(
        join_type="INNER", table=SqlTableRef(name=set_name), on=join_condition
    )


def _leaf_path_selections(
    bound: BoundMeasure, config: PackageConfig, query: NormalizedQuery
) -> tuple[list[PathSelection], list[str]]:
    measures = _measure_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    measure = measures[bound.measure_id]

    selections: list[PathSelection] = []
    required_entities: set[str] = set(_measure_required_entities(measure, config))
    for entity_id in sorted(required_entities):
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=entity_id,
            preference=query.path_policy.preference,
            purpose="measure_expr",
        )
        if selection is not None:
            selections.append(selection)
    for dim_id in query.group_by:
        dim = dimensions[dim_id]
        required_entities.add(dim.entity)
        if _direct_dimension_source_expr(measure.entity, dim_id, config) is not None:
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=dim.entity,
            preference=query.path_policy.preference,
            purpose="group_by",
        )
        if selection is not None:
            selections.append(selection)
    if query.time is not None:
        role = temporal_roles[_leaf_time_role(bound, query, config)]
        time_entity = dimensions[role.dimension].entity
        required_entities.add(time_entity)
        if _direct_dimension_source_expr(measure.entity, role.dimension, config) is None:
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=measure.entity,
                target_entity=time_entity,
                preference=query.path_policy.preference,
                purpose="time",
            )
            if selection is not None:
                selections.append(selection)
    for item in query.where:
        dim = dimensions[item.field]
        required_entities.add(dim.entity)
        if _direct_dimension_source_expr(measure.entity, item.field, config) is not None:
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=dim.entity,
            preference=query.path_policy.preference,
            purpose="where",
        )
        if selection is not None:
            selections.append(selection)
    for bound_clause in _bound_filter_clauses(bound, config):
        dim = dimensions[bound_clause["field"]]
        required_entities.add(dim.entity)
        if _direct_dimension_source_expr(measure.entity, bound_clause["field"], config) is not None:
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=dim.entity,
            preference=query.path_policy.preference,
            purpose="metric_filter",
        )
        if selection is not None:
            selections.append(selection)
    for predicate in _bound_metric_predicates(bound):
        required_entities.add(predicate.entity)
        if _can_project_entity_key_from_source(measure.entity, predicate.entity, config):
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=predicate.entity,
            preference=query.path_policy.preference,
            purpose="metric_predicate",
        )
        if selection is not None:
            selections.append(selection)
    for metric_filter in query.metric_filters:
        if metric_filter.expression is None or not isinstance(
            metric_filter.expression, MetricPredicateExpr
        ):
            continue
        required_entities.add(metric_filter.expression.entity)
        if _can_project_entity_key_from_source(
            measure.entity, metric_filter.expression.entity, config
        ):
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=metric_filter.expression.entity,
            preference=query.path_policy.preference,
            purpose="metric_predicate",
        )
        if selection is not None:
            selections.append(selection)

    dedup: dict[tuple[str, str], PathSelection] = {}
    for selection in selections:
        dedup[(selection.target_entity, selection.purpose)] = selection
    selections = list(dedup.values())

    unsupported = [row for row in selections if row.analysis.get("status") != "ok"]
    if unsupported:
        if all(
            _entity_in_terms_of_rewrite_supported(
                measure=measure, bound=bound, selection=row, config=config, query=query
            )
            for row in unsupported
        ):
            return selections, sorted(required_entities)
        row = unsupported[0]
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Measure '{bound.measure_id}' requires unsupported leaf rewrite toward '{row.target_entity}'",
            details={
                "measure": bound.measure_id,
                "target_entity": row.target_entity,
                "purpose": row.purpose,
                "path": list(row.chosen_path),
                "analysis": row.analysis,
            },
        )
    return selections, sorted(required_entities)


def _mixed_grain_error(
    *,
    target_entity: str,
    purpose: str,
    chosen: list[str],
    analysis: dict[str, Any],
    config: PackageConfig,
    query: NormalizedQuery,
    bound_measures: list[BoundMeasure],
) -> SemanticLayerError:
    """MIXED_GRAIN_INVALID with recovery enrichment for the failed pairing.

    Beyond the path/analysis evidence, the details carry
    ``compatible_measures`` / ``compatible_dimensions`` /
    ``time_axis_recovery`` so agents can pivot to a working query in
    one step instead of brute-forcing the registry (see
    ``compiler_parts.grain_recovery``).
    """
    details: dict[str, Any] = {
        "target_entity": target_entity,
        "purpose": purpose,
        "path": list(chosen),
        "analysis": analysis,
    }
    details.update(
        mixed_grain_pairing_enrichment(
            config=config,
            query=query,
            measure_ids=[row.measure_id for row in bound_measures],
            target_entity=target_entity,
        )
    )
    return SemanticLayerError(
        "MIXED_GRAIN_INVALID",
        f"Path to '{target_entity}' requires a rewrite that is not supported for {purpose}",
        details=details,
    )


def _root_path_summary(
    root_entity: str,
    bound_measures: list[BoundMeasure],
    config: PackageConfig,
    query: NormalizedQuery,
) -> tuple[
    dict[str, list[str]], dict[str, list[list[str]]], list[RewriteStep], dict[str, dict[str, Any]]
]:
    measures = _measure_index(config)
    dimensions = _dimension_index(config)
    selected_paths: dict[str, list[str]] = {}
    candidate_paths: dict[str, list[list[str]]] = {}
    analyses: dict[str, dict[str, Any]] = {}
    rewrite_steps: list[RewriteStep] = []

    targets = {measures[row.measure_id].entity: "measure" for row in bound_measures}
    for dim_id in query.group_by:
        if dim_id not in dimensions:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown dimension '{dim_id}'",
                details={"dimension": dim_id},
            )
        targets[dimensions[dim_id].entity] = "group_by"
    for item in query.where:
        if item.field not in dimensions:
            if item.field in measures:
                raise SemanticLayerError(
                    "OBJECT_NOT_FOUND",
                    f"where filter field '{item.field}' is a measure, not a dimension",
                    details={
                        "dimension": item.field,
                        "measure": item.field,
                        "path": "where[].field",
                        "why_invalid": (
                            "where filters compare row-level dimensions to literal values. "
                            "Measures require metric_filters, a governed segment, or an "
                            "authored dimension value that represents the cohort."
                        ),
                        "recovery_hints": [
                            {
                                "kind": "use_metric_filter_or_segment",
                                "message": (
                                    "Move aggregate/cohort predicates to metric_filters, "
                                    "or filter by a reachable dimension/segment that encodes "
                                    "the governed cohort."
                                ),
                            }
                        ],
                    },
                )
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown dimension '{item.field}'",
                details={"dimension": item.field},
            )
        _validate_where_value_type(dimensions[item.field], item)
        targets[dimensions[item.field].entity] = "where"
    for target_entity, purpose in sorted(targets.items()):
        if target_entity == root_entity:
            continue
        try:
            chosen, candidates = _preferred_path(
                config,
                start=root_entity,
                target=target_entity,
                preference=query.path_policy.preference,
            )
        except SemanticLayerError as exc:
            if exc.code in {"PATH_NOT_FOUND", "AMBIGUOUS_PATH"} and purpose == "measure":
                measure = next(
                    row
                    for row in bound_measures
                    if measures[row.measure_id].entity == target_entity
                )
                analyses[target_entity] = {"status": "rewrite_required", "reason": exc.code.lower()}
                rewrite_steps.append(
                    RewriteStep(
                        kind="leaf_preaggregate_join",
                        status="applied",
                        measure_id=measure.measure_id,
                        reason="Independent fact families compiled as separate leaf aggregates and reconciled on final grain keys",
                        details={"target_entity": target_entity},
                    )
                )
                continue
            raise
        selected_paths[target_entity] = list(chosen)
        candidate_paths[target_entity] = [list(path) for path in candidates]
        analysis = analyze_fanout(
            config,
            root_entity,
            chosen,
            time_bound_relationships=_time_bound_relationship_ids(query, config),
        )
        analyses[target_entity] = analysis
        if analysis.get("status") == "rewrite_required" and purpose == "measure":
            measure = next(
                row for row in bound_measures if measures[row.measure_id].entity == target_entity
            )
            rewrite_steps.append(
                RewriteStep(
                    kind="leaf_preaggregate_join",
                    status="applied",
                    measure_id=measure.measure_id,
                    reason="Mixed-grain measure compiled as an independent leaf aggregate and joined on final grain keys",
                    details={"path": list(chosen), "analysis": analysis},
                )
            )
        elif (
            analysis.get("status") == "rewrite_required"
            and purpose == "group_by"
            and len(bound_measures) == 1
        ):
            measure_cfg = measures[bound_measures[0].measure_id]
            selection = PathSelection(
                target_entity=target_entity,
                purpose=purpose,
                chosen_path=list(chosen),
                candidate_paths=[list(path) for path in candidates],
                analysis=analysis,
            )
            if _entity_in_terms_of_rewrite_supported(
                measure=measure_cfg,
                bound=bound_measures[0],
                selection=selection,
                config=config,
                query=query,
            ):
                rewrite_steps.append(
                    RewriteStep(
                        kind="entity_in_terms_of",
                        status="applied",
                        measure_id=bound_measures[0].measure_id,
                        reason="Count-distinct parent entity is computed from the qualifying child relation and rolled up by the requested child dimension.",
                        details={
                            "target_entity": target_entity,
                            "path": list(chosen),
                            "omits_semantic_root_entity": measure_cfg.entity,
                        },
                    )
                )
            else:
                raise _mixed_grain_error(
                    target_entity=target_entity,
                    purpose=purpose,
                    chosen=list(chosen),
                    analysis=analysis,
                    config=config,
                    query=query,
                    bound_measures=bound_measures,
                )
        elif analysis.get("status") == "rewrite_required":
            raise _mixed_grain_error(
                target_entity=target_entity,
                purpose=purpose,
                chosen=list(chosen),
                analysis=analysis,
                config=config,
                query=query,
                bound_measures=bound_measures,
            )
    return selected_paths, candidate_paths, rewrite_steps, analyses


def _query_target_entities(query: NormalizedQuery, config: PackageConfig) -> dict[str, str]:
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    targets: dict[str, str] = {}
    for dim_id in query.group_by:
        targets.setdefault(dimensions[dim_id].entity, "group_by")
    for item in query.where:
        targets.setdefault(dimensions[item.field].entity, "where")
    if query.time is not None:
        role = temporal_roles.get(query.time.temporal_role)
        if role is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{query.time.temporal_role}'"
            )
        targets.setdefault(dimensions[role.dimension].entity, "time")
    return targets


def _preferred_root_order(query: NormalizedQuery, config: PackageConfig) -> list[str]:
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    preferred: list[str] = []
    for dim_id in query.group_by:
        preferred.append(dimensions[dim_id].entity)
    if query.time is not None:
        role = temporal_roles.get(query.time.temporal_role)
        if role is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{query.time.temporal_role}'"
            )
        preferred.append(dimensions[role.dimension].entity)
    for item in query.where:
        preferred.append(dimensions[item.field].entity)
    return list(dict.fromkeys(preferred))


def _candidate_root_summary(
    root_entity: str, query: NormalizedQuery, config: PackageConfig
) -> dict[str, Any]:
    relationships = _relationship_index(config)
    targets = _query_target_entities(query, config)
    selected_paths: dict[str, list[str]] = {}
    candidate_paths: dict[str, list[list[str]]] = {}
    analyses: dict[str, dict[str, Any]] = {}
    total_hops = 0
    max_hops = 0
    total_preference = 0
    for target_entity, purpose in sorted(targets.items()):
        if target_entity == root_entity:
            continue
        chosen, candidates = _preferred_path(
            config, start=root_entity, target=target_entity, preference=query.path_policy.preference
        )
        analysis = analyze_fanout(
            config,
            root_entity,
            chosen,
            time_bound_relationships=_time_bound_relationship_ids(query, config),
        )
        if analysis.get("status") != "ok":
            raise SemanticLayerError(
                "MIXED_GRAIN_INVALID",
                f"Path to '{target_entity}' requires a rewrite that is not supported for distinct-values queries",
                details={
                    "root_entity": root_entity,
                    "target_entity": target_entity,
                    "purpose": purpose,
                    "path": list(chosen),
                    "analysis": analysis,
                },
            )
        selected_paths[target_entity] = list(chosen)
        candidate_paths[target_entity] = [list(path) for path in candidates]
        analyses[target_entity] = analysis
        total_hops += len(chosen)
        max_hops = max(max_hops, len(chosen))
        total_preference += sum(relationships[rel_id].path_preference for rel_id in chosen)
    preferred_roots = _preferred_root_order(query, config)
    return {
        "root_entity": root_entity,
        "selected_paths": selected_paths,
        "candidate_paths": candidate_paths,
        "analyses": analyses,
        "score": (
            total_hops,
            preferred_roots.index(root_entity)
            if root_entity in preferred_roots
            else len(preferred_roots) + 1,
            max_hops,
            total_preference,
        ),
    }


def _infer_root_entity_for_distinct_query(
    config: PackageConfig, query: NormalizedQuery
) -> dict[str, Any]:
    targets = _query_target_entities(query, config)
    if not targets:
        raise SemanticLayerError(
            "INVALID_QUERY", "Distinct-values queries require group_by or time"
        )

    candidates: list[dict[str, Any]] = []
    failures: list[SemanticLayerError] = []
    for entity in config.entities:
        if not entity.allowed_as_root:
            continue
        try:
            candidates.append(_candidate_root_summary(entity.id, query, config))
        except SemanticLayerError as exc:
            failures.append(exc)

    if not candidates:
        if failures:
            raise failures[0]
        raise SemanticLayerError(
            "PATH_NOT_FOUND",
            "No valid root entity found for distinct-values query",
            details={"targets": sorted(targets)},
        )

    ranked = sorted(candidates, key=lambda row: (*row["score"], row["root_entity"]))
    return ranked[0]


def _unique_path_selections(rows: Iterable[PathSelection]) -> list[PathSelection]:
    dedup: dict[tuple[str, str], PathSelection] = {}
    for row in rows:
        dedup[(row.target_entity, row.purpose)] = row
    return list(dedup.values())


def _entity_key_expr(table: str, columns: list[str]) -> Any:
    if len(columns) == 1:
        return _column_ref(table, columns[0])
    return SqlCall(
        "CONCAT_WS",
        [
            SqlLiteral("|"),
            *[SqlCast(_column_ref(table, column), "VARCHAR") for column in columns],
        ],
    )


def _conversion_operand_filters(
    filter_spec: dict[str, Any], config: PackageConfig, *, side: str
) -> list[dict[str, Any]]:
    """Resolve an operand-level filter spec on a conversion base/converted
    aggregate into dimension filter items the event CTEs can lower.

    Every clause must land in SQL or raise: a conversion operand filter
    that parses but silently disappears from the compiled plan is the
    exact wrong-answer footgun external agents flagged ("orders
    containing Product A then Product B" quietly becoming "all orders
    then all orders").
    """
    spec = dict(filter_spec or {})
    if not spec:
        return []
    unsupported_keys = sorted(set(spec) - {"all"})
    if unsupported_keys:
        raise SemanticLayerError(
            "CONVERSION_NOT_SUPPORTED",
            (
                f"Conversion {side} operand filters support only an 'all' clause list "
                f"of dimension predicates; got unsupported keys {unsupported_keys}"
            ),
            details={"side": side, "unsupported_keys": unsupported_keys, "filter": spec},
        )
    items: list[dict[str, Any]] = []
    for clause in list(spec.get("all", []) or []):
        clause = dict(clause or {})
        if clause.get("expression") is not None:
            raise SemanticLayerError(
                "CONVERSION_NOT_SUPPORTED",
                (
                    f"Expression clauses (e.g. metric_predicate) inside conversion {side} "
                    "operand filters are not supported. Use query-level metric_filters to "
                    "constrain the conversion population instead."
                ),
                details={"side": side, "clause": clause},
            )
        field = str(clause.get("field", "") or clause.get("dimension", "")).strip()
        if not field:
            raise SemanticLayerError(
                "CONVERSION_NOT_SUPPORTED",
                f"Conversion {side} operand filter clauses require a dimension 'field'",
                details={"side": side, "clause": clause},
            )
        items.append(
            {
                "dimension": _resolve_filter_dimension(field, config),
                "op": str(clause.get("op", "=") or "="),
                "value": clause.get("value"),
            }
        )
    return items


def _resolve_conversion_source(
    expr: SemanticExpr,
    config: PackageConfig,
    query: NormalizedQuery,
    *,
    side: str = "base/converted",
) -> dict[str, Any]:
    measures = _measure_index(config)
    recipes = _recipe_index(config)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown metric '{expr.metric_recipe}'")
        return _resolve_conversion_source(recipe.expression, config, query, side=side)
    if isinstance(expr, AggregateExpr):
        if expr.window:
            raise SemanticLayerError(
                "CONVERSION_NOT_SUPPORTED",
                (
                    f"Window specs on the conversion {side} operand are not "
                    "supported. Set the conversion-level 'window' for the matching "
                    "horizon, or pre-author a windowed metric and reference it."
                ),
                details={"side": side, "window": dict(expr.window), "measure": expr.measure},
            )
        source = _resolve_conversion_source(
            MeasureRefExpr(
                measure=expr.measure,
                aggregation=expr.aggregation,
                temporal_role=expr.temporal_role,
                parameters=dict(expr.parameters or {}),
            ),
            config,
            query,
            side=side,
        )
        source["filters"] = _conversion_operand_filters(expr.filter, config, side=side)
        return source
    if isinstance(expr, MeasureRefExpr):
        bound = _bind_measure(expr, config, query)
        measure = measures[bound.measure_id]
        if (
            measure.measure_class not in {"event_count", "distinct_population"}
            or bound.aggregation != "count_distinct"
        ):
            raise _unsupported_conversion(
                ConversionExpr(
                    base=expr,
                    converted=expr,
                    entity=measure.entity,
                    window_unit="day",
                    window_value=1,
                    matching_mode="first_converted_after_base",
                )
            )
        return {
            "measure": measure,
            "bound_measure": bound,
            "root_entity": measure.entity,
            "time_role": bound.temporal_role or _default_temporal_role(measure),
            "filters": [],
        }
    raise SemanticLayerError(
        "CONVERSION_NOT_SUPPORTED",
        "Conversion execution currently requires base and converted inputs to resolve to event-count measures",
        details={"expression": expr_to_dict(expr)},
    )


def _conversion_dimension_paths(
    *,
    source_entity: str,
    target_dimension_ids: Iterable[str],
    config: PackageConfig,
    query: NormalizedQuery,
    purpose: str,
    allow_rewrite_dimension_ids: set[str] | None = None,
) -> list[PathSelection]:
    dimensions = _dimension_index(config)
    allow_rewrite = set(allow_rewrite_dimension_ids or set())
    selections: list[PathSelection] = []
    for dim_id in target_dimension_ids:
        dim = dimensions.get(dim_id)
        if dim is None:
            # constant_properties (and other conversion-only dimension
            # references) reach this point without earlier object
            # resolution; an unknown id must surface as a structured
            # error, not a KeyError-turned-INTERNAL_ERROR.
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown dimension '{dim_id}' referenced by the conversion ({purpose})",
                details={"dimension": dim_id, "purpose": purpose},
            )
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=source_entity,
            target_entity=dim.entity,
            preference=query.path_policy.preference,
            purpose=purpose,
        )
        if selection is not None:
            status = selection.analysis.get("status")
            rewrite_safe = (
                dim_id in allow_rewrite
                and status == "rewrite_required"
                and _path_reverse_count_distinct_safe(
                    start_entity=source_entity,
                    path=list(selection.chosen_path),
                    config=config,
                )
            )
            if status != "ok" and not rewrite_safe:
                raise SemanticLayerError(
                    "CONVERSION_NOT_SUPPORTED",
                    f"Conversion execution does not support the required path to '{dim.entity}'",
                    details={
                        "dimension": dim_id,
                        "analysis": selection.analysis,
                        "path": selection.chosen_path,
                    },
                )
            selections.append(selection)
    return selections


def _conversion_entity_key_fields(
    entity_id: str, config: PackageConfig
) -> list[tuple[str, SqlIdentifier]]:
    entity = _entity_index(config).get(entity_id)
    if entity is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown entity '{entity_id}'")
    table = entity.table
    return [
        (f"__match_key_{index + 1}", _column_ref(table, column))
        for index, column in enumerate(entity.key)
    ]


def _conversion_event_cte(
    *,
    cte_name: str,
    source_entity: str,
    event_time_role: str,
    match_entity: str,
    event_key_alias: str,
    time_alias: str,
    group_by: list[str],
    group_time_alias: str,
    group_time_expr: Any | None,
    property_dimensions: list[str],
    extra_dimensions: list[str] | None = None,
    allow_rewrite_dimension_ids: set[str] | None = None,
    operand_filters: list[dict[str, Any]] | None = None,
    config: PackageConfig,
    query: NormalizedQuery,
    where_items: list[Any],
) -> SqlCte:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    source_table = entities[source_entity].table
    event_entity = entities[source_entity]
    role = temporal_roles.get(event_time_role)
    if role is None:
        raise SemanticLayerError(
            "INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{event_time_role}'"
        )
    event_time_dim = dimensions[role.dimension]
    extra_dimensions = list(extra_dimensions or [])
    # Operand-level filters (e.g. "base = orders containing Product A")
    # become plain WHERE predicates on the event relation. Their
    # dimensions must be joined in, and fanout paths are acceptable here:
    # downstream matching dedups on the event key (COUNT DISTINCT /
    # rank-1 per __base_event_key), so a one-to-many join acts as an
    # EXISTS-style "event has at least one qualifying row" filter.
    operand_filters = list(operand_filters or [])
    operand_filter_dims = list(dict.fromkeys(str(item["dimension"]) for item in operand_filters))
    where_items = [
        *where_items,
        *[
            build_filter_condition(
                _resolve_dimension_expr(str(item["dimension"]), config)[0],
                item["op"],
                item["value"],
            )
            for item in operand_filters
        ],
    ]
    allow_rewrite_dimension_ids = set(allow_rewrite_dimension_ids or set()) | set(
        operand_filter_dims
    )
    required_dimensions = list(
        dict.fromkeys(
            [
                *group_by,
                *property_dimensions,
                *extra_dimensions,
                *operand_filter_dims,
                role.dimension,
            ]
        )
    )
    joins = _joins_for_paths(
        source_entity,
        _unique_path_selections(
            [
                *_conversion_dimension_paths(
                    source_entity=source_entity,
                    target_dimension_ids=required_dimensions,
                    config=config,
                    query=query,
                    purpose="conversion_dimension",
                    allow_rewrite_dimension_ids=allow_rewrite_dimension_ids,
                ),
                *(
                    []
                    if match_entity == source_entity
                    else [
                        sel
                        for sel in [
                            _path_selection(
                                config=config,
                                query=query,
                                start_entity=source_entity,
                                target_entity=match_entity,
                                preference=query.path_policy.preference,
                                purpose="conversion_match_entity",
                            )
                        ]
                        if sel is not None
                    ]
                ),
            ]
        ),
        config,
        time_spec=(
            query.time.to_dict()
            if query.time is not None and hasattr(query.time, "to_dict")
            else asdict(query.time)
            if query.time is not None
            else None
        ),
    )
    select_fields: list[SqlField] = []
    selected_aliases: set[str] = set()
    for dim_id in group_by:
        dim_expr, alias = _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(dim_expr, alias))
        selected_aliases.add(alias)
    if group_time_alias and group_time_expr is not None:
        select_fields.append(SqlField(group_time_expr, group_time_alias))
        selected_aliases.add(group_time_alias)
    event_time_expr = _column_ref(entities[event_time_dim.entity].table, event_time_dim.column)
    select_fields.append(SqlField(event_time_expr, time_alias))
    selected_aliases.add(time_alias)
    select_fields.append(
        SqlField(_entity_key_expr(source_table, list(event_entity.key)), event_key_alias)
    )
    selected_aliases.add(event_key_alias)
    for alias, expr in _conversion_entity_key_fields(match_entity, config):
        if alias not in selected_aliases:
            select_fields.append(SqlField(expr, alias))
            selected_aliases.add(alias)
    for index, dim_id in enumerate(property_dimensions):
        dim_expr, _ = _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(dim_expr, f"__property_{index + 1}"))
        selected_aliases.add(f"__property_{index + 1}")
    for dim_id in extra_dimensions:
        dim_expr, alias = _resolve_dimension_expr(dim_id, config)
        if alias not in selected_aliases:
            select_fields.append(SqlField(dim_expr, alias))
            selected_aliases.add(alias)
    return SqlCte(
        name=cte_name,
        query=SqlSelect(
            select=select_fields,
            from_table=SqlTableRef(name=source_table),
            joins=joins,
            where=where_items,
        ),
    )


def _conversion_predicate_set_ctes(
    predicate: MetricPredicateExpr,
    *,
    index: int,
    plan: LogicalPlan,
    config: PackageConfig,
    scope: dict[str, Any],
) -> tuple[list[SqlCte], str, list[str]]:
    mini_query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": expr_to_dict(predicate.input), "as": "__predicate_value"}],
        "group_by": list(scope["group_by_dims"]),
    }
    predicate_where_items = [asdict(item) for item in scope["filter_items"]]
    if scope["time_spec"] is not None and scope["time_spec"].get("entity_only_window"):
        role = _temporal_role_index(config)[str(scope["time_spec"]["temporal_role"])]
        if scope["time_spec"].get("start") is not None:
            predicate_where_items.append(
                {"dimension": role.dimension, "op": ">=", "value": scope["time_spec"]["start"]}
            )
        if scope["time_spec"].get("end") is not None:
            predicate_where_items.append(
                {"dimension": role.dimension, "op": "<", "value": scope["time_spec"]["end"]}
            )
    if predicate_where_items:
        mini_query["where"] = predicate_where_items
    if scope["time_spec"] is not None and not scope["time_spec"].get("entity_only_window"):
        mini_query["time"] = _public_time_spec(scope["time_spec"])

    predicate_sql = _compile_query_sql_ast(config, mini_query)
    source_name, set_name = _predicate_sql_names(predicate, index, scope)
    source_cte = SqlCte(
        name=source_name, query=_namespace_sql_select(predicate_sql, f"{source_name}__")
    )

    projected_keys = list(scope["group_by_dims"])
    source_group_keys = list(scope["group_by_dims"])
    select_fields = [
        SqlField(SqlIdentifier(parts=["predicate_source", dim_id]), dim_id)
        for dim_id in projected_keys
    ]
    if scope["time_alias"]:
        select_fields.append(
            SqlField(
                SqlIdentifier(parts=["predicate_source", scope["source_time_alias"]]),
                scope["time_alias"],
            )
        )
        projected_keys.append(scope["time_alias"])
        source_group_keys.append(scope["source_time_alias"])
    # `source_cte` already produces one row per key combination, so re-grouping
    # is a no-op. `SELECT DISTINCT` reads as "set of qualifying ids" without
    # implying we need to dedup duplicate rows that don't exist.
    set_cte = SqlCte(
        name=set_name,
        query=SqlSelect(
            select=select_fields,
            from_table=SqlTableRef(name=source_name, alias="predicate_source"),
            where=[_predicate_where_condition(predicate.op, predicate.value)],
            distinct=True,
        ),
    )
    _ = source_group_keys  # retained to make intent visible; no GROUP BY emitted
    return [source_cte, set_cte], set_name, projected_keys


def _conversion_supported(
    expr: ConversionExpr, config: PackageConfig, query: NormalizedQuery
) -> bool:
    try:
        _resolve_conversion_source(expr.base, config, query)
        _resolve_conversion_source(expr.converted, config, query)
        return True
    except SemanticLayerError:
        return False


def _conversion_dimension_binding(expr: ConversionExpr, dim_id: str) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in dict((expr.dimension_bindings or {}).get(dim_id, {}) or {}).items()
    }


def _converted_side_group_dimensions(expr: ConversionExpr, group_by: Iterable[str]) -> list[str]:
    converted: list[str] = []
    for dim_id in group_by:
        binding = _conversion_dimension_binding(expr, str(dim_id))
        if binding.get("side") == "converted":
            if binding.get("denominator") != "all_base_events":
                raise SemanticLayerError(
                    "CONVERSION_NOT_SUPPORTED",
                    "Converted-side conversion dimensions require denominator: all_base_events",
                    details={"dimension": str(dim_id), "binding": binding},
                )
            converted.append(str(dim_id))
    return converted


def _conversion_dimension_requires_binding(
    *,
    dim_id: str,
    base_entity: str,
    converted_entity: str,
    config: PackageConfig,
    query: NormalizedQuery,
) -> bool:
    dimensions = _dimension_index(config)
    dim = dimensions[dim_id]

    def _supported_from(entity_id: str, *, allow_rewrite: bool) -> bool:
        if entity_id == dim.entity:
            return True
        try:
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=entity_id,
                target_entity=dim.entity,
                preference=query.path_policy.preference,
                purpose="conversion_dimension_probe",
            )
        except SemanticLayerError:
            return False
        if selection is None:
            return True
        status = selection.analysis.get("status")
        if status == "ok":
            return True
        return (
            allow_rewrite
            and status == "rewrite_required"
            and _path_reverse_count_distinct_safe(
                start_entity=entity_id,
                path=list(selection.chosen_path),
                config=config,
            )
        )

    return not _supported_from(base_entity, allow_rewrite=False) and _supported_from(
        converted_entity, allow_rewrite=True
    )


def _conversion_leaf_cte(
    expr: ConversionExpr, *, index: int, plan: LogicalPlan, config: PackageConfig
) -> SqlCte:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    dialect = dialect_for_warehouse(config.package.warehouse)
    query = normalize_query(plan.query)
    base_source = _resolve_conversion_source(expr.base, config, query, side="base")
    converted_source = _resolve_conversion_source(expr.converted, config, query, side="converted")
    match_entity = str(expr.entity or base_source["root_entity"])
    if match_entity not in entities:
        raise SemanticLayerError(
            "CONVERSION_ENTITY_REQUIRED", f"Unknown conversion entity '{match_entity}'"
        )

    group_time_alias = ""
    group_time_expr = None
    base_group_where: list[Any] = []
    converted_group_where: list[Any] = []
    converted_side_group_dims = _converted_side_group_dimensions(expr, plan.group_by)
    converted_side_group_dim_set = set(converted_side_group_dims)
    for dim_id in plan.group_by:
        if dim_id in converted_side_group_dim_set:
            continue
        if _conversion_dimension_requires_binding(
            dim_id=dim_id,
            base_entity=base_source["root_entity"],
            converted_entity=converted_source["root_entity"],
            config=config,
            query=query,
        ):
            raise SemanticLayerError(
                "CONVERSION_NOT_SUPPORTED",
                "Conversion dimension requires an explicit converted-side binding and denominator policy",
                details={
                    "dimension": dim_id,
                    "required_binding": {"side": "converted", "denominator": "all_base_events"},
                },
            )
    if plan.time:
        query_role = temporal_roles.get(str(plan.time.get("temporal_role", "")))
        if query_role is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE",
                f"Unknown temporal role '{plan.time.get('temporal_role', '')}'",
            )
        query_dim = dimensions[query_role.dimension]
        # Defensive: the conversion-leaf CTE selects from the base anchor
        # relation. If the requested temporal role's underlying dimension
        # entity isn't the base entity (or the converted entity when both
        # share the same relation), the rendered SQL would reference a
        # table that isn't in the CTE's FROM clause — a DuckDB binder
        # error at execute time. _validate_conversion_temporal_bindings()
        # at plan_query gate-time should catch this before we get here;
        # if we still reach it, raise the same structured envelope rather
        # than letting the binder error leak.
        base_entity_id = base_source["root_entity"]
        converted_entity_id = converted_source["root_entity"]
        binding_dim_entity = query_dim.entity
        if binding_dim_entity not in {base_entity_id, converted_entity_id}:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_BINDING",
                (
                    f"Conversion leaf CTE cannot bind temporal role "
                    f"'{plan.time['temporal_role']}' — the role's dimension "
                    f"resolves to entity '{binding_dim_entity}' but the CTE "
                    f"is anchored on '{base_entity_id}'. This is an internal "
                    f"invariant violation; the planner's pre-validation "
                    f"should have rejected this query."
                ),
                details={
                    "requested": plan.time["temporal_role"],
                    "anchor_temporal_role": base_source["time_role"],
                    "allowed_temporal_roles": [base_source["time_role"]]
                    if base_source["time_role"]
                    else [],
                    "base_entity": base_entity_id,
                    "converted_entity": converted_entity_id,
                    "dim_entity": binding_dim_entity,
                },
            )
        raw_expr = _column_ref(entities[query_dim.entity].table, query_dim.column)
        group_time_alias = (
            str(plan.time["temporal_role"])
            if not plan.time.get("grain")
            else f"{plan.time['temporal_role']}__{plan.time['grain']}"
        )
        group_time_expr = (
            dialect.date_trunc(plan.time["grain"], raw_expr) if plan.time.get("grain") else raw_expr
        )
        if plan.time.get("start") is not None:
            base_group_where.append(SqlBinary(raw_expr, ">=", SqlLiteral(plan.time["start"])))
        if plan.time.get("end") is not None:
            base_group_where.append(SqlBinary(raw_expr, "<", SqlLiteral(plan.time["end"])))

    for item in list(query.where or []):
        where_target = (
            converted_group_where
            if str(item.field) in converted_side_group_dim_set
            else base_group_where
        )
        dim_expr, _ = _resolve_dimension_expr(str(item.field), config)
        where_target.append(build_filter_condition(dim_expr, item.op, item.value))

    base_cte_name = f"conversion_base_{index + 1}"
    converted_cte_name = f"conversion_converted_{index + 1}"
    matched_cte_name = f"conversion_matches_{index + 1}"
    alias = _expression_alias(expr, config)
    property_dims = list(expr.constant_properties or [])
    query_predicates = _query_metric_predicates(plan)
    predicate_scopes = [
        (predicate, _predicate_scope(predicate, plan=plan, config=config))
        for predicate in query_predicates
    ]
    predicate_dimensions = list(
        dict.fromkeys(
            dim_id for _, scope in predicate_scopes for dim_id in list(scope["group_by_dims"] or [])
        )
    )

    if converted_side_group_dims:
        base_group_dims = [
            dim_id for dim_id in plan.group_by if dim_id not in converted_side_group_dim_set
        ]
        base_cte = _conversion_event_cte(
            cte_name=base_cte_name,
            source_entity=base_source["root_entity"],
            event_time_role=base_source["time_role"],
            match_entity=match_entity,
            event_key_alias="__base_event_key",
            time_alias="__base_event_time",
            group_by=base_group_dims,
            group_time_alias=group_time_alias,
            group_time_expr=group_time_expr,
            property_dimensions=property_dims,
            extra_dimensions=predicate_dimensions,
            operand_filters=list(base_source.get("filters") or []),
            config=config,
            query=query,
            where_items=base_group_where,
        )
        converted_cte = _conversion_event_cte(
            cte_name=converted_cte_name,
            source_entity=converted_source["root_entity"],
            event_time_role=converted_source["time_role"],
            match_entity=match_entity,
            event_key_alias="__converted_event_key",
            time_alias="__converted_event_time",
            group_by=converted_side_group_dims,
            group_time_alias="",
            group_time_expr=None,
            property_dimensions=property_dims,
            allow_rewrite_dimension_ids=set(converted_side_group_dims),
            operand_filters=list(converted_source.get("filters") or []),
            config=config,
            query=query,
            where_items=converted_group_where,
        )

        predicate_ctes: list[SqlCte] = []
        predicate_joins: list[SqlJoin] = []
        for predicate_index, (predicate, scope) in enumerate(predicate_scopes):
            ctes, set_name, join_keys = _conversion_predicate_set_ctes(
                predicate,
                index=predicate_index,
                plan=plan,
                config=config,
                scope=scope,
            )
            predicate_ctes.extend(ctes)
            predicate_joins.append(
                SqlJoin(
                    join_type="INNER",
                    table=SqlTableRef(name=set_name),
                    on=_join_condition(join_keys, "base_events", set_name, dialect=dialect),
                )
            )

        join_condition: Any = SqlBinary(
            SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
            ">=",
            SqlIdentifier(parts=["base_events", "__base_event_time"]),
        )
        join_condition = SqlBinary(
            join_condition,
            "AND",
            SqlBinary(
                dialect.date_diff(
                    expr.window_unit,
                    SqlIdentifier(parts=["base_events", "__base_event_time"]),
                    SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
                ),
                "<=",
                SqlLiteral(expr.window_value),
            ),
        )
        for key_alias, _ in _conversion_entity_key_fields(match_entity, config):
            join_condition = SqlBinary(
                join_condition,
                "AND",
                dialect.null_safe_eq(
                    SqlIdentifier(parts=["base_events", key_alias]),
                    SqlIdentifier(parts=["converted_events", key_alias]),
                ),
            )
        for index_prop, _ in enumerate(property_dims):
            prop_alias = f"__property_{index_prop + 1}"
            join_condition = SqlBinary(
                join_condition,
                "AND",
                dialect.null_safe_eq(
                    SqlIdentifier(parts=["base_events", prop_alias]),
                    SqlIdentifier(parts=["converted_events", prop_alias]),
                ),
            )

        order_terms = [
            SqlOrderTerm(
                expr=SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
                direction="ASC",
            ),
            SqlOrderTerm(
                expr=SqlIdentifier(parts=["converted_events", "__converted_event_key"]),
                direction="ASC",
            ),
        ]
        if expr.matching_mode == "closest_converted_after_base":
            order_terms = [
                SqlOrderTerm(
                    expr=dialect.date_diff(
                        expr.window_unit,
                        SqlIdentifier(parts=["base_events", "__base_event_time"]),
                        SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
                    ),
                    direction="ASC",
                ),
                *order_terms,
            ]

        base_keys = [*base_group_dims, *([group_time_alias] if group_time_alias else [])]
        output_keys = [*plan.group_by, *([group_time_alias] if group_time_alias else [])]
        matched_fields = [
            *[SqlField(SqlIdentifier(parts=["base_events", key]), key) for key in base_group_dims],
            *[
                SqlField(SqlIdentifier(parts=["converted_events", key]), key)
                for key in converted_side_group_dims
            ],
            *(
                [SqlField(SqlIdentifier(parts=["base_events", group_time_alias]), group_time_alias)]
                if group_time_alias
                else []
            ),
            SqlField(SqlIdentifier(parts=["base_events", "__base_event_key"]), "__base_event_key"),
            SqlField(
                SqlIdentifier(parts=["converted_events", "__converted_event_key"]),
                "__converted_event_key",
            ),
            SqlField(
                SqlWindow(
                    function=SqlCall("RANK", []),
                    partition_by=[SqlIdentifier(parts=["base_events", "__base_event_key"])],
                    order_by=order_terms,
                ),
                "__match_rank",
            ),
        ]
        matched_cte = SqlCte(
            name=matched_cte_name,
            query=SqlSelect(
                ctes=[*predicate_ctes, base_cte, converted_cte],
                select=matched_fields,
                from_table=SqlTableRef(name=base_cte_name, alias="base_events"),
                joins=[
                    *predicate_joins,
                    SqlJoin(
                        join_type="LEFT",
                        table=SqlTableRef(name=converted_cte_name, alias="converted_events"),
                        on=join_condition,
                    ),
                ],
            ),
        )
        denominator_name = f"conversion_denominator_{index + 1}"
        numerator_name = f"conversion_numerator_{index + 1}"
        denominator_cte = SqlCte(
            name=denominator_name,
            query=SqlSelect(
                select=[
                    *[SqlField(SqlIdentifier(parts=["matches", key]), key) for key in base_keys],
                    SqlField(
                        _aggregation_expr(
                            SqlIdentifier(parts=["matches", "__base_event_key"]), "count_distinct"
                        ),
                        "__denominator",
                    ),
                ],
                from_table=SqlTableRef(name=matched_cte_name, alias="matches"),
                where=[
                    SqlBinary(SqlIdentifier(parts=["matches", "__match_rank"]), "=", SqlLiteral(1))
                ],
                group_by=[SqlIdentifier(parts=["matches", key]) for key in base_keys],
            ),
        )
        numerator_cte = SqlCte(
            name=numerator_name,
            query=SqlSelect(
                select=[
                    *[SqlField(SqlIdentifier(parts=["matches", key]), key) for key in output_keys],
                    SqlField(
                        _aggregation_expr(
                            SqlIdentifier(parts=["matches", "__base_event_key"]), "count_distinct"
                        ),
                        "__numerator",
                    ),
                ],
                from_table=SqlTableRef(name=matched_cte_name, alias="matches"),
                where=[
                    SqlBinary(SqlIdentifier(parts=["matches", "__match_rank"]), "=", SqlLiteral(1)),
                    SqlBinary(
                        SqlIdentifier(parts=["matches", "__converted_event_key"]),
                        "IS NOT",
                        SqlLiteral(None),
                    ),
                ],
                group_by=[SqlIdentifier(parts=["matches", key]) for key in output_keys],
            ),
        )
        denominator_join = (
            SqlJoin(
                join_type="CROSS", table=SqlTableRef(name=denominator_name, alias="denominator")
            )
            if not base_keys
            else SqlJoin(
                join_type="INNER",
                table=SqlTableRef(name=denominator_name, alias="denominator"),
                on=_join_condition(base_keys, "numerator", "denominator", dialect=dialect),
            )
        )
        return SqlCte(
            name=f"conversion_leaf_{index + 1}",
            query=SqlSelect(
                ctes=[matched_cte, denominator_cte, numerator_cte],
                select=[
                    *[
                        SqlField(SqlIdentifier(parts=["numerator", key]), key)
                        for key in output_keys
                    ],
                    SqlField(
                        SqlBinary(
                            SqlIdentifier(parts=["numerator", "__numerator"]),
                            "/",
                            SqlCall(
                                "NULLIF",
                                [
                                    SqlIdentifier(parts=["denominator", "__denominator"]),
                                    SqlLiteral(0),
                                ],
                            ),
                        ),
                        alias,
                    ),
                ],
                from_table=SqlTableRef(name=numerator_name, alias="numerator"),
                joins=[denominator_join],
            ),
        )

    base_cte = _conversion_event_cte(
        cte_name=base_cte_name,
        source_entity=base_source["root_entity"],
        event_time_role=base_source["time_role"],
        match_entity=match_entity,
        event_key_alias="__base_event_key",
        time_alias="__base_event_time",
        group_by=list(plan.group_by),
        group_time_alias=group_time_alias,
        group_time_expr=group_time_expr,
        property_dimensions=property_dims,
        extra_dimensions=predicate_dimensions,
        operand_filters=list(base_source.get("filters") or []),
        config=config,
        query=query,
        where_items=base_group_where,
    )
    converted_cte = _conversion_event_cte(
        cte_name=converted_cte_name,
        source_entity=converted_source["root_entity"],
        event_time_role=converted_source["time_role"],
        match_entity=match_entity,
        event_key_alias="__converted_event_key",
        time_alias="__converted_event_time",
        group_by=[],
        group_time_alias="",
        group_time_expr=None,
        property_dimensions=property_dims,
        extra_dimensions=[],
        operand_filters=list(converted_source.get("filters") or []),
        config=config,
        query=query,
        where_items=[],
    )

    predicate_ctes = []
    predicate_joins = []
    for predicate_index, (predicate, scope) in enumerate(predicate_scopes):
        ctes, set_name, join_keys = _conversion_predicate_set_ctes(
            predicate,
            index=predicate_index,
            plan=plan,
            config=config,
            scope=scope,
        )
        predicate_ctes.extend(ctes)
        predicate_joins.append(
            SqlJoin(
                join_type="INNER",
                table=SqlTableRef(name=set_name),
                on=_join_condition(join_keys, "base_events", set_name, dialect=dialect),
            )
        )

    join_condition = SqlBinary(
        SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
        ">=",
        SqlIdentifier(parts=["base_events", "__base_event_time"]),
    )
    join_condition = SqlBinary(
        join_condition,
        "AND",
        SqlBinary(
            dialect.date_diff(
                expr.window_unit,
                SqlIdentifier(parts=["base_events", "__base_event_time"]),
                SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
            ),
            "<=",
            SqlLiteral(expr.window_value),
        ),
    )
    match_keys = _conversion_entity_key_fields(match_entity, config)
    for key_alias, _ in match_keys:
        join_condition = SqlBinary(
            join_condition,
            "AND",
            dialect.null_safe_eq(
                SqlIdentifier(parts=["base_events", key_alias]),
                SqlIdentifier(parts=["converted_events", key_alias]),
            ),
        )
    for index_prop, _ in enumerate(property_dims):
        prop_alias = f"__property_{index_prop + 1}"
        join_condition = SqlBinary(
            join_condition,
            "AND",
            dialect.null_safe_eq(
                SqlIdentifier(parts=["base_events", prop_alias]),
                SqlIdentifier(parts=["converted_events", prop_alias]),
            ),
        )
    order_terms = [
        SqlOrderTerm(
            expr=SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
            direction="ASC",
        ),
        SqlOrderTerm(
            expr=SqlIdentifier(parts=["converted_events", "__converted_event_key"]), direction="ASC"
        ),
    ]
    if expr.matching_mode == "closest_converted_after_base":
        order_terms = [
            SqlOrderTerm(
                expr=dialect.date_diff(
                    expr.window_unit,
                    SqlIdentifier(parts=["base_events", "__base_event_time"]),
                    SqlIdentifier(parts=["converted_events", "__converted_event_time"]),
                ),
                direction="ASC",
            ),
            *order_terms,
        ]

    matched_cte = SqlCte(
        name=matched_cte_name,
        query=SqlSelect(
            ctes=[*predicate_ctes, base_cte, converted_cte],
            select=[
                *[
                    SqlField(SqlIdentifier(parts=["base_events", key]), key)
                    for key in [*plan.group_by, *([group_time_alias] if group_time_alias else [])]
                ],
                SqlField(
                    SqlIdentifier(parts=["base_events", "__base_event_key"]), "__base_event_key"
                ),
                SqlField(
                    SqlIdentifier(parts=["converted_events", "__converted_event_key"]),
                    "__converted_event_key",
                ),
                SqlField(
                    SqlWindow(
                        function=SqlCall("ROW_NUMBER", []),
                        partition_by=[SqlIdentifier(parts=["base_events", "__base_event_key"])],
                        order_by=order_terms,
                    ),
                    "__match_rank",
                ),
            ],
            from_table=SqlTableRef(name=base_cte_name, alias="base_events"),
            joins=[
                *predicate_joins,
                SqlJoin(
                    join_type="LEFT",
                    table=SqlTableRef(name=converted_cte_name, alias="converted_events"),
                    on=join_condition,
                ),
            ],
        ),
    )

    # `else_expr=None` renders without an ELSE clause; CASE WHEN ... END
    # implicitly returns NULL when no branch matches, so the explicit
    # `ELSE NULL` was redundant.
    numerator_expr = _aggregation_expr(
        SqlCase(
            whens=[
                SqlCaseWhen(
                    condition=SqlBinary(
                        SqlIdentifier(parts=["matches", "__converted_event_key"]),
                        "IS NOT",
                        SqlLiteral(None),
                    ),
                    result=SqlIdentifier(parts=["matches", "__base_event_key"]),
                )
            ],
        ),
        "count_distinct",
    )
    denominator_expr = _aggregation_expr(
        SqlIdentifier(parts=["matches", "__base_event_key"]), "count_distinct"
    )
    select_fields = [
        SqlField(SqlIdentifier(parts=["matches", key]), key)
        for key in [*plan.group_by, *([group_time_alias] if group_time_alias else [])]
    ]
    # NULLIF(denominator, 0) yields NULL when denom is zero, and x / NULL = NULL,
    # so an outer CASE-when-zero null-guard is dead code.
    select_fields.append(
        SqlField(
            SqlBinary(numerator_expr, "/", SqlCall("NULLIF", [denominator_expr, SqlLiteral(0)])),
            alias,
        )
    )
    return SqlCte(
        name=f"conversion_leaf_{index + 1}",
        query=SqlSelect(
            ctes=[matched_cte],
            select=select_fields,
            from_table=SqlTableRef(name=matched_cte_name, alias="matches"),
            where=[SqlBinary(SqlIdentifier(parts=["matches", "__match_rank"]), "=", SqlLiteral(1))],
            group_by=[
                SqlIdentifier(parts=["matches", key])
                for key in [*plan.group_by, *([group_time_alias] if group_time_alias else [])]
            ],
        ),
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
    from dataclasses import replace as _replace

    return _replace(config, measures=[*config.measures, *synthetic.values()])


def resolve_compile_config(plan: LogicalPlan, config: PackageConfig) -> PackageConfig:
    """Public helper for callers (``compile_query``, ``lower_to_sql``
    wrappers, validators) that need the synthetic-measure-extended
    config produced by ``plan_query``. When the plan has no synthetics
    the original config is returned unchanged.
    """
    return _extend_config_with_synthetic_measures(config, plan.synthetic_measures)


_ROUTABLE_GRAIN_ORDER = {
    "transaction": 0,
    "minute": 1,
    "hour": 2,
    "day": 3,
    "week": 4,
    "month": 5,
    "quarter": 6,
    "year": 7,
}


def _grain_rank(grain: str) -> int:
    return _ROUTABLE_GRAIN_ORDER.get(str(grain or "").strip().lower(), -1)


def _aggregate_dimension_coverage(row: AggregateRelationConfig) -> set[str]:
    return {str(item) for item in [*row.dimensions, *row.dimension_columns]}


def _aggregate_measure_coverage(row: AggregateRelationConfig) -> set[str]:
    return {str(item) for item in [*row.measures, *row.measure_columns]}


def _aggregate_relation_rejection_reason(
    row: AggregateRelationConfig,
    *,
    bound: BoundMeasure,
    query: NormalizedQuery,
    leaf_time_role: str,
    required_dimensions: set[str],
) -> str:
    if row.source != "default":
        return "non_default_source"
    if (row.equivalence_kind or "exact") != "exact":
        return "non_exact_equivalence"
    if row.temporal_role and row.temporal_role != leaf_time_role:
        return "temporal_role_mismatch"
    requested_grain = str((query.time.grain if query.time else "") or "").lower()
    if not requested_grain:
        return "missing_query_time_grain"
    if requested_grain not in set(row.eligible_time_grains or [row.grain]):
        return "unsupported_query_grain"
    if _grain_rank(row.grain) < 0 or _grain_rank(row.grain) > _grain_rank(requested_grain):
        return "aggregate_grain_too_coarse"
    if bound.measure_id not in _aggregate_measure_coverage(row):
        return "missing_measure"
    if bound.measure_id not in row.measure_columns:
        return "missing_measure_column"
    if required_dimensions - _aggregate_dimension_coverage(row):
        return "missing_dimension"
    rollup = str(row.measure_rollups.get(bound.measure_id, "") or "").lower()
    if rollup not in {"additive", "precomputed"}:
        return "unsupported_rollup"
    aggregation = str(row.measure_aggregations.get(bound.measure_id, "") or "").lower()
    if aggregation and aggregation != "sum":
        return "unsupported_rollup_aggregation"
    if str(bound.aggregation or "").lower() not in {"sum", "count", "count_distinct"}:
        return "unsupported_query_aggregation"
    return ""


def _select_aggregate_relation(
    *,
    bound: BoundMeasure,
    query: NormalizedQuery,
    config: PackageConfig,
) -> str:
    if query.time is None or not query.time.grain:
        return ""
    if any(isinstance(item.expression, MetricPredicateExpr) for item in query.metric_filters):
        return ""
    leaf_time_role = _leaf_time_role(bound, query, config)
    measure = _measure_index(config)[bound.measure_id]
    required_dimensions = {str(item) for item in query.group_by if str(item)}
    required_dimensions.update(str(item.field) for item in query.where if str(item.field))
    required_dimensions.update(
        field
        for field in (str(item.get("field", "")) for item in _bound_filter_clauses(bound, config))
        if field
    )
    candidates = [
        row
        for row in config.aggregate_relations
        if row.source_entity == measure.entity
        and not _aggregate_relation_rejection_reason(
            row,
            bound=bound,
            query=query,
            leaf_time_role=leaf_time_role,
            required_dimensions=required_dimensions,
        )
    ]
    if not candidates:
        return ""
    candidates.sort(
        key=lambda row: (
            _grain_rank(row.grain),
            -len(row.entity_grain),
            int(row.selection_priority or 0),
            row.id,
        ),
        reverse=True,
    )
    return candidates[0].id


def plan_query(
    config: PackageConfig, registry: Registry | None, payload: dict[str, Any]
) -> LogicalPlan:
    raw_query = normalize_query(payload)

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

    measures = _measure_index(config)

    _validate_query_temporal_bindings(query, config)
    _validate_restrictive_time_semantics(query, config)

    bound_measures: list[BoundMeasure] = []
    for select_item in query.select:
        if select_item.expression is None:
            continue
        _collect_measure_refs(select_item.expression, config, query, bound_measures)
    for filter_item in query.metric_filters:
        if filter_item.expression is None:
            continue
        if isinstance(filter_item.expression, MetricPredicateExpr):
            _validate_metric_predicate_filter_envelope(asdict(filter_item))
            continue
        _collect_measure_refs(filter_item.expression, config, query, bound_measures)
    conversion_exprs: list[ConversionExpr] = []
    for select_item in query.select:
        if select_item.expression is not None:
            _collect_conversion_exprs(select_item.expression, config, conversion_exprs)
    for filter_item in query.metric_filters:
        if filter_item.expression is not None and not isinstance(
            filter_item.expression, MetricPredicateExpr
        ):
            _collect_conversion_exprs(filter_item.expression, config, conversion_exprs)

    dedup_measures = {
        (
            row.measure_id,
            row.aggregation,
            row.temporal_role,
            _freeze_payload(row.aggregation_params),
            _freeze_payload(row.filter_spec),
            _freeze_payload(row.window_spec),
        ): row
        for row in bound_measures
    }
    bound_measures = list(dedup_measures.values())
    _validate_measure_validity_windows(bound_measures, config, query)
    _validate_rollup_safety(bound_measures, config)
    measure_plans: list[MeasurePlan] = []
    if bound_measures:
        root_entity = measures[bound_measures[0].measure_id].entity
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
            path_selections, required_entities = _leaf_path_selections(bound, config, query)
            grain_keys = [*query.group_by]
            if query.time is not None:
                grain_keys.append(
                    query.time.temporal_role
                    if not query.time.grain
                    else f"{query.time.temporal_role}__{query.time.grain}"
                )
            rewrite_strategy = (
                "leaf_preaggregate_join"
                if any(step.measure_id == bound.measure_id for step in rewrite_steps)
                else "direct"
            )
            aggregate_relation_id = _select_aggregate_relation(
                bound=bound,
                query=query,
                config=config,
            )
            measure_plans.append(
                MeasurePlan(
                    cte_name=f"leaf_{index + 1}",
                    bound_measure=bound,
                    source_entity=measures[bound.measure_id].entity,
                    path_selections=path_selections,
                    grain_keys=grain_keys,
                    rewrite_strategy="aggregate_relation"
                    if aggregate_relation_id
                    else rewrite_strategy,
                    required_entities=required_entities,
                    aggregate_relation_id=aggregate_relation_id,
                )
            )
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
    return LogicalPlan(
        version=2,
        query=query.to_dict(),
        root_entity=root_entity,
        resolved_ids=resolved_ids,
        selected_paths=selected_paths,
        candidate_paths=candidate_paths,
        group_by=list(query.group_by),
        time=asdict(query.time) if query.time else {},
        bound_measures=bound_measures,
        measure_plans=measure_plans,
        post_aggregation_exprs=post_exprs,
        grain_analysis=grain_analysis,
        fanout_strategy=fanout_strategy,
        rewrite_steps=rewrite_steps,
        semantic_dag=_semantic_dag_for_query(query, config),
        synthetic_measures=dict(synthetic_measures),
    )


def _metric_filter_alias(index: int) -> str:
    return f"metric_filter__{index + 1}"


def _query_key_aliases(plan: LogicalPlan) -> list[str]:
    aliases = list(plan.group_by)
    if plan.time:
        aliases.append(
            plan.time["temporal_role"]
            if not plan.time.get("grain")
            else f"{plan.time['temporal_role']}__{plan.time['grain']}"
        )
    return aliases


def _measure_leaf_select(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> SqlSelect:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    measures = _measure_index(config)
    temporal_roles = _temporal_role_index(config)
    query = plan.query
    measure = measures[measure_plan.bound_measure.measure_id]

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
    leaf_time_role = _leaf_time_role(
        measure_plan.bound_measure, normalize_query(plan.query), config
    )
    for dim_id in plan.group_by:
        expr, alias = _resolve_dimension_expr(dim_id, config)
        select_fields.append(SqlField(expr, alias))
        group_fields.append(expr)
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        raw_expr = _column_ref(entities[dim.entity].table, dim.column)
        time_expr = (
            dialect_for_warehouse(config.package.warehouse).date_trunc(time["grain"], raw_expr)
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
        expr, _ = _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(build_filter_condition(expr, item.get("op", "="), item.get("value")))
    for item in _bound_filter_clauses(measure_plan.bound_measure, config):
        expr, _ = _resolve_dimension_expr(str(item["field"]), config)
        where_clauses.append(build_filter_condition(expr, item.get("op", "="), item.get("value")))
    if plan.time:
        time = dict(plan.time)
        role = temporal_roles[leaf_time_role]
        dim = dimensions[role.dimension]
        raw_expr = _column_ref(entities[dim.entity].table, dim.column)
        if time.get("start") is not None:
            where_clauses.append(SqlBinary(raw_expr, ">=", SqlLiteral(time["start"])))
        if time.get("end") is not None:
            where_clauses.append(SqlBinary(raw_expr, "<", SqlLiteral(time["end"])))

    order_expr = None
    if measure_plan.bound_measure.temporal_role:
        role = temporal_roles[measure_plan.bound_measure.temporal_role]
        time_dim = dimensions[role.dimension]
        order_expr = _column_ref(entities[time_dim.entity].table, time_dim.column)

    leaf_alias = _expression_alias(
        MeasureRefExpr(
            measure=measure.id,
            aggregation=measure_plan.bound_measure.aggregation,
            parameters=dict(measure_plan.bound_measure.aggregation_params),
        ),
        config,
    )
    select_fields.append(
        SqlField(
            _aggregation_expr(
                _config_expr_to_sql(measure.expr, measure, config),
                measure_plan.bound_measure.aggregation,
                order_expr=order_expr,
                parameters=measure_plan.bound_measure.aggregation_params,
            ),
            leaf_alias,
        )
    )
    return SqlSelect(
        ctes=predicate_ctes,
        select=select_fields,
        from_table=SqlTableRef(
            name=(getattr(measure, "source_relation", "") or entities[measure.entity].table)
        ),
        joins=[
            *_joins_for_paths(
                measure.entity, measure_plan.path_selections, config, time_spec=plan.time
            ),
            *predicate_joins,
        ],
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
    return any(
        _expr_requires_dense_series(_parse_public_expr(dict(item["expression"])), config)
        for item in list(plan.query.get("metric_filters", []) or [])
    )


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


def lower_to_sql(plan: LogicalPlan, config: PackageConfig) -> SqlSelect:
    from .compiler_parts.sql_lowering import lower_to_sql as _lower_to_sql

    return _lower_to_sql(plan, config)


def _compile_query_sql_ast(config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
    plan = plan_query(config, None, payload)
    config = resolve_compile_config(plan, config)
    return attach_relation_ctes(config, lower_to_sql(plan, config))


def compile_query(
    config: PackageConfig, registry: Registry | None, payload: dict[str, Any]
) -> dict[str, Any]:
    started = time.perf_counter()
    plan = plan_query(config, registry, payload)
    config = resolve_compile_config(plan, config)
    sql_ast = attach_relation_ctes(config, lower_to_sql(plan, config))
    rendered = render_select_for_profile(
        sql_ast, str(payload.get("sql_profile", payload.get("render_profile", "audit")) or "audit")
    )
    from .compiler_parts.sql_lowering import build_performance_plan, build_physical_plan

    physical_plan = build_physical_plan(plan, config)
    performance_plan = build_performance_plan(plan, config, physical_plan, rendered)
    compile_stats = {
        "compile_ms": round((time.perf_counter() - started) * 1000, 3),
        "cache_hit": False,
        "dialect": dialect_for_warehouse(config.package.warehouse).name,
        "physical_plan_nodes": len(physical_plan.nodes),
    }
    relationships = {row.id: row for row in config.relationships}
    explain = ExplainArtifact(
        normalized_query=plan.query,
        alias_resolution=plan.resolved_ids,
        chosen_paths={
            entity: {
                "selected": path,
                "candidates": plan.candidate_paths.get(entity, []),
                "contracts": [
                    relationship_contract_payload(relationships[rel_id])
                    for rel_id in list(path or [])
                    if rel_id in relationships
                ],
            }
            for entity, path in plan.selected_paths.items()
        },
        grain_analysis=plan.grain_analysis,
        fanout_strategy=plan.fanout_strategy,
        rewrite_strategy={
            "status": "rewritten" if plan.rewrite_steps else "direct",
            "steps": [asdict(row) for row in plan.rewrite_steps],
        },
        logical_plan=asdict(plan),
        semantic_dag=asdict(plan.semantic_dag),
        physical_plan=asdict(physical_plan),
        performance_plan=asdict(performance_plan),
        sql_plan=asdict(sql_ast),
        rendered_sql=rendered,
        compile_stats=compile_stats,
    )
    return {
        "logical_plan": plan,
        "sql_ast": sql_ast,
        "sql": rendered,
        "explain": explain,
        "physical_plan": physical_plan,
        "performance_plan": performance_plan,
        "compile_stats": compile_stats,
    }


def plan_comparison_bundle(
    config: PackageConfig,
    registry: Registry,
    *,
    left_object_id: str,
    right_object_id: str,
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    partial_query = dict(partial_query or {})
    partial_query.setdefault("version", 1)

    def _select_item(object_id: str) -> dict[str, Any]:
        kind = _object_kind(config, object_id)
        if kind == "metric":
            return {"expression": {"metric": object_id}, "as": _object_label(config, object_id)}
        measure = _measure_index(config)[object_id]
        return {
            "expression": {"measure": object_id, "aggregation": measure.default_aggregation},
            "as": measure.label,
        }

    left_meta = _object_comparison_metadata(config, left_object_id)
    right_meta = _object_comparison_metadata(config, right_object_id)
    family = left_meta["comparison_family"] or right_meta["comparison_family"]
    preferred_mode = left_meta["comparison_mode"] or right_meta["comparison_mode"] or "same_query"

    combined_query = dict(partial_query)
    combined_query["select"] = [_select_item(left_object_id), _select_item(right_object_id)]

    combined_validation: dict[str, Any] = {"ok": True, "error": {}}
    try:
        compile_query(config, registry, combined_query)
    except SemanticLayerError as exc:
        combined_validation = {
            "ok": False,
            "error": {"code": exc.code, "message": str(exc), "details": exc.details},
        }

    def _single_query(object_id: str) -> dict[str, Any]:
        query = dict(partial_query)
        query["version"] = int(query.get("version", 1) or 1)
        query["select"] = [_select_item(object_id)]
        if dict(query.get("time", {}) or {}):
            time_spec = dict(query.get("time", {}) or {})
            default_role = _object_default_query_temporal_role(config, object_id)
            if default_role:
                time_spec["temporal_role"] = default_role
            query["time"] = time_spec
        return query

    left_query = _single_query(left_object_id)
    right_query = _single_query(right_object_id)

    try:
        compile_query(config, registry, left_query)
        compile_query(config, registry, right_query)
    except SemanticLayerError as exc:
        raise SemanticLayerError(
            exc.code,
            str(exc),
            details={
                "left_object_id": left_object_id,
                "right_object_id": right_object_id,
                **dict(exc.details or {}),
            },
        ) from exc

    same_query_allowed = preferred_mode != "coordinated_queries" and combined_validation["ok"]
    comparison_mode = "same_query" if same_query_allowed else "coordinated_queries"
    bundle = [
        {
            "object_id": left_object_id,
            "label": _object_label(config, left_object_id),
            "query_patch": left_query,
        },
        {
            "object_id": right_object_id,
            "label": _object_label(config, right_object_id),
            "query_patch": right_query,
        },
    ]
    return {
        "comparison_family": family,
        "comparison_mode": comparison_mode,
        "same_query_candidate": combined_query if same_query_allowed else None,
        "comparison_bundle": bundle,
        "blocked_same_query": None if combined_validation["ok"] else combined_validation["error"],
        "preferred_mode": preferred_mode,
    }
