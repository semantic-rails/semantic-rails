"""Agent-facing metadata surfaces: catalog, discover, inspect, build-options.

Every governed lookup surface other than ``validate`` / ``compile`` /
``query`` lives here. The big public entries are :func:`catalog_payload`,
:func:`discover_payload`, :func:`inspect_payload`,
:func:`build_options_payload`, and :func:`valid_values_payload`.
Natural-language intent planning lives in ``semantic_rails.planner.plan``.
``plan`` and ``discover`` both enforce the classifier + relevance-floor
gate at the HTTP/MCP/CLI boundary (see ``enforce_scope`` flag on
``discover_payload``); internal guided flows leave the gate off so partial
queries and builder steps still work.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

from .ast import normalize_partial_query, normalize_query
from .catalog_search import CatalogSearchDocument, SearchTerms
from .compiler import (
    _conversion_supported,
    _predicate_context_entity_candidates,
    _reduced_context_entities,
    _time_bound_relationship_ids,
)
from .diagnostics import relationship_contract_payload
from .errors import SemanticLayerError
from .expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    CumulativeExpr,
    DistributionExpr,
    EntityValueExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RatioExpr,
    RollingExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    parse_semantic_expression,
)
from .metadata_parts.capabilities import (
    _capability_payload,
)
from .metadata_parts.guidance import (
    _aggregation_guidance,
    _metric_guidance,
    _metric_preferences,
)
from .metadata_parts.object_metadata import (
    _comparison_metadata,
    _example_test_metadata,
    _operational_metadata,
    _public_object_type,
    _review_metadata,
    _time_contract_metadata,
)
from .metadata_parts.path_coverage import (
    _declared_value_rows,
    _dimension_history_coverage,
    _metric_history_coverage_notes,
    _path_availability,
    _valid_values_lookup_hint,
    _value_domain_for_dimension,
)
from .metadata_parts.relevance import (
    _INTENT_STOPWORDS,
    _catalog_token_doc_freq,
    _catalog_token_index,
    _intent_passes_relevance_floor,
    _low_relevance_block,
    _norm,
    _token_idf_weight,
    _tokenize,
)
from .metadata_parts.scope_gate import scope_block_payload as _scope_block_payload
from .metadata_parts.valid_values import valid_values_payload
from .policies import hidden_object_ids, policy_effects_for_object
from .request_context import context_from_policy_context
from .runtime import Runtime, runtime_request_scope
from .schema import PackageConfig
from .scope import classify_question
from .segments import build_segment_query, normalize_segment


def _query_state(query: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "version",
        "select",
        "group_by",
        "where",
        "metric_filters",
        "time",
        "temporal_role_overrides",
        "path_policy",
        "order_by",
        "limit",
        "debug",
        "explain",
        "export",
    ]
    state = {key: query[key] for key in keys if key in query}
    with contextlib.suppress(SemanticLayerError):
        state["normalized_query"] = normalize_query(dict(query)).to_dict()
    return state


def _config_maps(config: PackageConfig) -> dict[str, dict[str, Any]]:
    return {
        "entities": {row.id: row for row in config.entities},
        "dimensions": {row.id: row for row in config.dimensions},
        "temporal_roles": {row.id: row for row in config.temporal_roles},
        "measures": {row.id: row for row in config.measures},
        "metric_recipes": {row.id: row for row in config.metric_recipes},
        "segments": {row.id: row for row in config.segments},
        "value_domains": {row.id: row for row in config.value_domains},
    }


def _relationship_index(config: PackageConfig) -> dict[str, Any]:
    return {row.id: row for row in config.relationships}


def _relationship_card_payload(config: PackageConfig, relationship_id: str) -> dict[str, Any]:
    relationships = _relationship_index(config)
    rel = relationships.get(relationship_id)
    if rel is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown relationship '{relationship_id}'",
            details={"object_id": relationship_id},
        )
    return {
        "root_entity": rel.source_entity,
        "source_entity": rel.source_entity,
        "target_entity": rel.target_entity,
        "source_columns": list(rel.source_columns or [rel.source_column]),
        "target_columns": list(rel.target_columns or [rel.target_column]),
        "cardinality": rel.cardinality,
        "safety": rel.safety,
        "allowed_directions": list(rel.allowed_directions),
        "path_preference": rel.path_preference,
        "temporal_validity": dict(rel.temporal_validity or {}),
        "contract": relationship_contract_payload(rel),
    }


def _compatible_temporal_roles_for_expr(config: PackageConfig, expr: SemanticExpr) -> list[str]:
    maps = _config_maps(config)
    measures = maps["measures"]
    recipes = maps["metric_recipes"]
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        measure = measures.get(expr.measure)
        return list(measure.compatible_temporal_roles if measure is not None else [])
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None:
            return []
        if recipe.compatible_temporal_roles:
            return list(recipe.compatible_temporal_roles)
        if recipe.temporal_role:
            return [recipe.temporal_role]
        return _compatible_temporal_roles_for_expr(config, recipe.expression)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        left = set(_compatible_temporal_roles_for_expr(config, expr.left))
        right = set(_compatible_temporal_roles_for_expr(config, expr.right))
        if left and right:
            return sorted(left & right)
        return sorted(left or right)
    if isinstance(expr, RatioExpr):
        left = set(_compatible_temporal_roles_for_expr(config, expr.numerator))
        right = set(_compatible_temporal_roles_for_expr(config, expr.denominator))
        if left and right:
            return sorted(left & right)
        return sorted(left or right)
    if isinstance(expr, EntityValueExpr):
        return _compatible_temporal_roles_for_expr(config, expr.input)
    if isinstance(expr, DistributionExpr):
        return _compatible_temporal_roles_for_expr(config, expr.over)
    if isinstance(expr, BooleanExpr):
        current: set[str] = set()
        for index, arg in enumerate(expr.args):
            roles = set(_compatible_temporal_roles_for_expr(config, arg))
            current = roles if index == 0 else current & roles
        return sorted(current)
    if isinstance(expr, CallExpr):
        current = set()
        for index, arg in enumerate(expr.args):
            roles = set(_compatible_temporal_roles_for_expr(config, arg))
            current = roles if index == 0 else current & roles
        return sorted(current)
    if isinstance(expr, CaseExpr):
        role_sets: list[set[str]] = []
        for item in expr.whens:
            role_sets.append(set(_compatible_temporal_roles_for_expr(config, item.when)))
            role_sets.append(set(_compatible_temporal_roles_for_expr(config, item.then)))
        if expr.else_expr is not None:
            role_sets.append(set(_compatible_temporal_roles_for_expr(config, expr.else_expr)))
        role_sets = [row for row in role_sets if row]
        if not role_sets:
            return []
        current = role_sets[0]
        for row in role_sets[1:]:
            current &= row
        return sorted(current)
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return _compatible_temporal_roles_for_expr(config, expr.input)
    if isinstance(expr, MetricPredicateExpr):
        return []
    if isinstance(expr, ConversionExpr):
        return _compatible_temporal_roles_for_expr(config, expr.base)
    return []


def _first_root_entity(config: PackageConfig, expr: SemanticExpr) -> str:
    maps = _config_maps(config)
    measures = maps["measures"]
    recipes = maps["metric_recipes"]
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        if expr.measure not in measures:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{expr.measure}'")
        return measures[expr.measure].entity
    if isinstance(expr, MetricRecipeRefExpr):
        if expr.metric_recipe not in recipes:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown metric '{expr.metric_recipe}'")
        return _first_root_entity(config, recipes[expr.metric_recipe].expression)
    if isinstance(expr, ArithmeticExpr):
        return _first_root_entity(config, expr.left)
    if isinstance(expr, ComparisonExpr):
        return _first_root_entity(config, expr.left)
    if isinstance(expr, RatioExpr):
        return _first_root_entity(config, expr.numerator)
    if isinstance(expr, EntityValueExpr):
        return _first_root_entity(config, expr.input)
    if isinstance(expr, DistributionExpr):
        return _first_root_entity(config, expr.over)
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            entity = _first_root_entity(config, arg)
            if entity:
                return entity
        return ""
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            entity = _first_root_entity(config, arg)
            if entity:
                return entity
        return ""
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            entity = _first_root_entity(config, item.when)
            if entity:
                return entity
            entity = _first_root_entity(config, item.then)
            if entity:
                return entity
        return _first_root_entity(config, expr.else_expr) if expr.else_expr is not None else ""
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return _first_root_entity(config, expr.input)
    if isinstance(expr, MetricPredicateExpr):
        return expr.entity
    if isinstance(expr, ConversionExpr):
        return expr.entity or _first_root_entity(config, expr.base)
    if isinstance(expr, LiteralExpr):
        return ""
    return ""


def _selection_context(config: PackageConfig, partial_query: dict[str, Any]) -> dict[str, Any]:
    root_entity = ""
    selected_exprs: list[SemanticExpr] = []
    for item in list(partial_query.get("select", []) or []):
        raw_expr = item.get("expression", {}) or {}
        if raw_expr:
            expr = parse_semantic_expression(raw_expr, context="query")
            selected_exprs.append(expr)
            if not root_entity:
                root_entity = _first_root_entity(config, expr)
    return {
        "root_entity": root_entity,
        "selected_exprs": selected_exprs,
        "selected_measure_ids": [
            expr.measure for expr in selected_exprs if isinstance(expr, MeasureRefExpr)
        ],
        "selected_metric_recipe_ids": [
            expr.metric_recipe for expr in selected_exprs if isinstance(expr, MetricRecipeRefExpr)
        ],
        "selected_dimensions": [
            str(item) for item in list(partial_query.get("group_by", []) or [])
        ],
        "selected_temporal_role": str(
            dict(partial_query.get("time", {}) or {}).get("temporal_role", "")
        ),
    }


def _measure_default_metric_id(measure: Any) -> str:
    default_metric_name = measure.name or measure.id.split("measure.", 1)[-1]
    return f"metric.{default_metric_name}"


def _is_auto_metric(config: PackageConfig, metric_id: str) -> bool:
    return any(_measure_default_metric_id(measure) == metric_id for measure in config.measures)


def _expr_summary(config: PackageConfig, expr: SemanticExpr) -> str:
    maps = _config_maps(config)
    if isinstance(expr, AggregateExpr):
        measure = maps["measures"].get(expr.measure)
        name = measure.label if measure is not None else expr.measure
        return f"{expr.aggregation} of {name}"
    if isinstance(expr, MeasureRefExpr):
        measure = maps["measures"].get(expr.measure)
        return measure.label if measure is not None else expr.measure
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = maps["metric_recipes"].get(expr.metric_recipe)
        return recipe.label if recipe is not None else expr.metric_recipe
    if isinstance(expr, ArithmeticExpr):
        op = {
            "add": "plus",
            "subtract": "minus",
            "multiply": "times",
            "divide": "divided by",
        }.get(expr.op, expr.op)
        return f"{_expr_summary(config, expr.left)} {op} {_expr_summary(config, expr.right)}"
    if isinstance(expr, CumulativeExpr):
        return f"cumulative {_expr_summary(config, expr.input)}"
    if isinstance(expr, RollingExpr):
        # ``RollingExpr`` carries ``unit``/``value`` directly on the
        # dataclass — there is no ``.window`` attribute. The earlier
        # ``expr.window.get(...)`` form crashed with AttributeError as
        # soon as ``_expr_summary`` ran over any rolling expression.
        # (The ruff/mypy pass on mainline landed a parallel fix using
        # bare attribute access; this defensive form supersedes it.)
        return f"rolling {expr.value or ''}-{expr.unit or ''} {_expr_summary(config, expr.input)}"
    if isinstance(expr, PriorPeriodExpr):
        return f"prior-period {_expr_summary(config, expr.input)}"
    if isinstance(expr, PeriodToDateExpr):
        return f"{expr.period} to date {_expr_summary(config, expr.input)}"
    if isinstance(expr, MetricPredicateExpr):
        scope = "contextual" if expr.scope_mode == "contextual" else "entity-only"
        if expr.time_grain:
            return f"{_expr_summary(config, expr.input)} {expr.op} {expr.value} ({scope}, {expr.time_grain})"
        return f"{_expr_summary(config, expr.input)} {expr.op} {expr.value} ({scope})"
    if isinstance(expr, ScopedAggregateExpr):
        measure = maps["measures"].get(expr.measure)
        name = measure.label if measure is not None else expr.measure
        return f"{expr.aggregation or (measure.default_aggregation if measure else 'aggregate')} of {name} scoped"
    if isinstance(expr, RatioExpr):
        return (
            f"{_expr_summary(config, expr.numerator)} / {_expr_summary(config, expr.denominator)}"
        )
    if isinstance(expr, EntityValueExpr):
        return f"{_expr_summary(config, expr.input)} by {expr.entity}"
    if isinstance(expr, DistributionExpr):
        return f"{expr.function} over {_expr_summary(config, expr.over)}"
    if isinstance(expr, ConversionExpr):
        return f"conversion from {_expr_summary(config, expr.base)} to {_expr_summary(config, expr.converted)}"
    return type(expr).__name__


def _infer_stage(partial_query: dict[str, Any], explicit_stage: str = "", terms: str = "") -> str:
    if explicit_stage:
        return explicit_stage
    lowered = str(terms or "").lower()
    if any(token in lowered for token in (" vs ", " versus ", "compare ", "compared to ")):
        return "comparison"
    selected = list(partial_query.get("select", []) or [])
    if list(partial_query.get("group_by", []) or []):
        return "post_dimension"
    if selected:
        return "post_measure"
    return "initial"


def _stage_kind_bonus(stage: str, kind: str) -> float:
    if stage == "initial":
        return {
            "measure": 18.0,
            "metric": 12.0,
            "segment": 10.0,
            "dimension": 6.0,
            "dimension_value": 5.0,
            "entity": 4.0,
        }.get(kind, 0.0)
    if stage == "post_measure":
        return {
            "dimension": 16.0,
            "dimension_value": 14.0,
            "measure": 10.0,
            "metric": 8.0,
            "segment": 6.0,
            "entity": 6.0,
        }.get(kind, 0.0)
    if stage == "post_dimension":
        return {
            "dimension_value": 18.0,
            "dimension": 12.0,
            "measure": 8.0,
            "metric": 6.0,
            "segment": 5.0,
            "entity": 4.0,
        }.get(kind, 0.0)
    if stage == "comparison":
        return {
            "metric": 14.0,
            "measure": 12.0,
            "segment": 8.0,
            "dimension": 8.0,
            "dimension_value": 7.0,
            "entity": 4.0,
        }.get(kind, 0.0)
    return 0.0


def _business_priority_adjustment(terms: str, candidate: dict[str, Any]) -> tuple[float, list[str]]:
    tokens = set(_tokenize(terms))
    if not tokens or not tokens <= {"sale", "sales"}:
        return 0.0, []
    title = " ".join(
        [
            str(candidate.get("id", "")),
            str(candidate.get("name", "")),
            str(candidate.get("label", "")),
            str(candidate.get("description", "")),
            " ".join(candidate.get("topics", []) or []),
        ]
    ).lower()
    normalized_id = _norm(str(candidate.get("id", "")))
    normalized_name = _norm(str(candidate.get("name", "")))
    adjustment = 0.0
    reasons: list[str] = []

    if normalized_id.endswith("revenueusd") or normalized_name.endswith("salesrevenueusd"):
        adjustment += 28.0
        reasons.append("core sales revenue metric")
    elif normalized_id.endswith("orders") or normalized_name.endswith("salesorders"):
        adjustment += 12.0
        reasons.append("core sales order metric")
    elif "revenue" in title and not any(
        marker in title for marker in ("food", "drink", "delivered", "product", "item")
    ):
        adjustment += 6.0
        reasons.append("core sales revenue topic")

    derived_markers = (
        "mtd",
        "qtd",
        "ytd",
        "rolling",
        "prior period",
        "month-over-month",
        "cumulative",
        "median",
    )
    if any(marker in title for marker in derived_markers):
        adjustment -= 18.0
        reasons.append("specific derived metric requires explicit intent")
    if any(marker in title for marker in ("supply", "inventory", "session")):
        adjustment -= 10.0
        reasons.append("non-core sales domain")
    return adjustment, reasons


def _compact_summary_from_card(
    card: dict[str, Any],
    *,
    available: bool,
    blocked_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "id": card.get("id", ""),
        "object_type": _public_object_type(str(card.get("kind", ""))),
        "kind": card.get("kind", ""),
        "name": card.get("name", ""),
        "label": card.get("label", ""),
        "description": card.get("description", ""),
        "root_entity": card.get("root_entity", ""),
        "default_temporal_role": card.get("default_temporal_role", ""),
        "available": available,
        "blocked_reason": blocked_reason,
    }
    if extra:
        row.update(extra)
    return row


def _metric_object_payload(config: PackageConfig, object_id: str, kind: str) -> dict[str, Any]:
    maps = _config_maps(config)
    if kind == "metric":
        recipe = maps["metric_recipes"][object_id]
        executable, unsupported_reason = _metric_executable(recipe, config)
        root_entity = _first_root_entity(config, recipe.expression)
        predicate_meta = _predicate_metadata(config, recipe.expression)
        valid_grouping_entities: list[dict[str, Any]] = []
        disabled_grouping_entities: list[dict[str, Any]] = []
        for entity in config.entities:
            availability = _path_availability(config, root_entity, entity.id)
            row = {
                "id": entity.id,
                "available": availability["available"],
                "reason": availability.get("reason", ""),
                "path": availability.get("path", []),
            }
            (
                valid_grouping_entities if availability["available"] else disabled_grouping_entities
            ).append(row)
        provenance = {}
        for dim in config.dimensions:
            if dim.entity == root_entity:
                continue
            availability = _path_availability(config, root_entity, dim.entity)
            provenance[dim.id] = {
                "entity": dim.entity,
                "path": availability.get("path", []),
                "available": availability["available"],
                "reason": availability.get("reason", ""),
            }
        return {
            "root_entity": root_entity,
            **_time_contract_metadata(recipe),
            "valid_grouping_entities": valid_grouping_entities,
            "disabled_grouping_entities": disabled_grouping_entities,
            "provenance": provenance,
            "curated_status": "auto_published" if _is_auto_metric(config, recipe.id) else "curated",
            "expression_summary": _expr_summary(config, recipe.expression),
            "executable": executable,
            "unsupported_reason": unsupported_reason,
            "coverage_notes": _metric_history_coverage_notes(config, root_entity),
            **predicate_meta,
            **_comparison_metadata(recipe),
        }
    if kind == "measure":
        measure = maps["measures"][object_id]
        valid_grouping_entities = []
        disabled_grouping_entities = []
        for entity in config.entities:
            availability = _path_availability(config, measure.entity, entity.id)
            row = {
                "id": entity.id,
                "available": availability["available"],
                "reason": availability.get("reason", ""),
                "path": availability.get("path", []),
            }
            (
                valid_grouping_entities if availability["available"] else disabled_grouping_entities
            ).append(row)
        return {
            "root_entity": measure.entity,
            **_time_contract_metadata(measure),
            "valid_grouping_entities": valid_grouping_entities,
            "disabled_grouping_entities": disabled_grouping_entities,
            "provenance": {},
            **_aggregation_guidance(measure),
            "default_metric_id": _measure_default_metric_id(measure),
            "executable": True,
            "unsupported_reason": "",
            "coverage_notes": _metric_history_coverage_notes(config, measure.entity),
            **_comparison_metadata(measure),
        }
    return {}


def _recommended_actions(kind: str) -> list[str]:
    actions = {
        "measure": ["inspect aggregations", "add breakdown dimension", "add time grain"],
        "metric": ["inspect metric card", "add breakdown dimension", "validate query"],
        "dimension": ["group by this dimension", "filter this dimension", "list valid values"],
        "entity": ["inspect related measures", "use as grouping entity"],
        "dimension_value": ["apply as a filter", "discover related measures"],
        "segment": ["validate segment", "preview members", "explain derived query"],
    }
    return actions.get(kind, ["inspect object"])


def _policy_context(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    context = dict((payload or {}).get("policy_context", {}) or {})
    return context_from_policy_context(context).to_policy_context()


def _policy_metadata(
    config: PackageConfig, object_id: str, *, partial_query: dict[str, Any] | None = None
) -> dict[str, Any]:
    policy_context = _policy_context(partial_query)
    effects = policy_effects_for_object(
        config,
        object_id,
        environment=str(policy_context.get("environment", "")),
        audience=str(policy_context.get("audience", "")),
        roles=policy_context.get("roles", []),
    )
    return {
        "policy_effects": effects,
        "policy_visibility": "hidden"
        if any(row["action"] == "hidden" for row in effects)
        else "visible",
    }


def _result_shape_preview(query_patch: dict[str, Any]) -> dict[str, Any]:
    select_aliases = [
        str(row.get("as", "") or "")
        for row in list(query_patch.get("select", []) or [])
        if str(row.get("as", "") or "")
    ]
    group_by = [str(item) for item in list(query_patch.get("group_by", []) or [])]
    time = dict(query_patch.get("time", {}) or {})
    time_alias = ""
    if time.get("temporal_role"):
        time_alias = (
            str(time["temporal_role"])
            if not time.get("grain")
            else f"{time['temporal_role']}__{time['grain']}"
        )
    columns = [*group_by, *([time_alias] if time_alias else []), *select_aliases]
    return {
        "columns": columns,
        "group_by": group_by,
        "time_alias": time_alias,
        "has_metrics": bool(select_aliases),
    }


def _availability_for_object(
    config: PackageConfig, root_entity: str, object_id: str, kind: str
) -> dict[str, Any]:
    maps = _config_maps(config)
    if kind == "measure":
        return _path_availability(config, root_entity, maps["measures"][object_id].entity)
    if kind == "metric":
        return _path_availability(
            config,
            root_entity,
            _first_root_entity(config, maps["metric_recipes"][object_id].expression),
        )
    if kind == "dimension":
        return _path_availability(config, root_entity, maps["dimensions"][object_id].entity)
    if kind == "segment":
        return _path_availability(config, root_entity, maps["segments"][object_id].entity)
    if kind == "entity":
        return _path_availability(config, root_entity, object_id)
    return {"available": True, "reason": "", "path": [], "candidates": []}


def _metric_executable(
    recipe: Any, config: PackageConfig, partial_query: dict[str, Any] | None = None
) -> tuple[bool, str]:
    if recipe.kind != "conversion":
        return True, ""
    probe = dict(partial_query or {})
    probe.setdefault("version", 1)
    probe["select"] = [{"expression": {"metric": recipe.id}, "as": "probe"}]
    try:
        executable = _conversion_supported(recipe.expression, config, normalize_query(probe))
    except SemanticLayerError:
        executable = False
    return executable, "" if executable else "CONVERSION_NOT_SUPPORTED"


def _related_metric_ids_for_measure(config: PackageConfig, measure: Any) -> list[str]:
    out = [_measure_default_metric_id(measure)]
    for recipe in config.metric_recipes:
        summary = _expr_summary(config, recipe.expression).lower()
        if (
            measure.label.lower() in summary or measure.name.lower() in summary
        ) and recipe.id not in out:
            out.append(recipe.id)
    return out


def _predicate_exprs_from_filter_spec(filter_spec: dict[str, Any]) -> list[MetricPredicateExpr]:
    predicates: list[MetricPredicateExpr] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        raw_expr = node.get("expression")
        if isinstance(raw_expr, dict):
            try:
                expr = parse_semantic_expression(raw_expr, context="config")
            except SemanticLayerError:
                expr = None
            if isinstance(expr, MetricPredicateExpr):
                predicates.append(expr)
        for key in ("all", "any"):
            _walk(node.get(key))
        if "not" in node:
            _walk(node.get("not"))

    _walk(filter_spec or {})
    return predicates


def _predicate_exprs_from_expr(
    config: PackageConfig, expr: SemanticExpr, *, seen_metric_ids: set[str] | None = None
) -> list[MetricPredicateExpr]:
    seen_metric_ids = set(seen_metric_ids or set())
    if isinstance(expr, MetricPredicateExpr):
        return [expr]
    if isinstance(expr, AggregateExpr):
        return _predicate_exprs_from_filter_spec(expr.filter)
    if isinstance(expr, MetricRecipeRefExpr):
        if expr.metric_recipe in seen_metric_ids:
            return []
        recipe = _config_maps(config)["metric_recipes"].get(expr.metric_recipe)
        if recipe is None:
            return []
        return _predicate_exprs_from_expr(
            config, recipe.expression, seen_metric_ids=seen_metric_ids | {expr.metric_recipe}
        )
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return [
            *_predicate_exprs_from_expr(config, expr.left, seen_metric_ids=seen_metric_ids),
            *_predicate_exprs_from_expr(config, expr.right, seen_metric_ids=seen_metric_ids),
        ]
    if isinstance(expr, BooleanExpr):
        predicates: list[MetricPredicateExpr] = []
        for arg in expr.args:
            predicates.extend(
                _predicate_exprs_from_expr(config, arg, seen_metric_ids=seen_metric_ids)
            )
        return predicates
    if isinstance(expr, CallExpr):
        predicates = []
        for arg in expr.args:
            predicates.extend(
                _predicate_exprs_from_expr(config, arg, seen_metric_ids=seen_metric_ids)
            )
        return predicates
    if isinstance(expr, CaseExpr):
        predicates = []
        for item in expr.whens:
            predicates.extend(
                _predicate_exprs_from_expr(config, item.when, seen_metric_ids=seen_metric_ids)
            )
            predicates.extend(
                _predicate_exprs_from_expr(config, item.then, seen_metric_ids=seen_metric_ids)
            )
        if expr.else_expr is not None:
            predicates.extend(
                _predicate_exprs_from_expr(config, expr.else_expr, seen_metric_ids=seen_metric_ids)
            )
        return predicates
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return _predicate_exprs_from_expr(config, expr.input, seen_metric_ids=seen_metric_ids)
    if isinstance(expr, ConversionExpr):
        return [
            *_predicate_exprs_from_expr(config, expr.base, seen_metric_ids=seen_metric_ids),
            *_predicate_exprs_from_expr(config, expr.converted, seen_metric_ids=seen_metric_ids),
        ]
    return []


def _predicate_metadata(
    config: PackageConfig, expr: SemanticExpr, partial_query: dict[str, Any] | None = None
) -> dict[str, Any]:
    predicates = _predicate_exprs_from_expr(config, expr)
    if not predicates:
        return {}
    predicate = predicates[0]
    partial_query = dict(partial_query or {})
    context_entities: list[str] = []
    effective_grain = predicate.time_grain or (
        "inherit" if predicate.scope_mode == "contextual" else ""
    )
    if predicate.scope_mode == "contextual" and partial_query:
        try:
            normalized = normalize_partial_query(
                {
                    "version": int(partial_query.get("version", 1) or 1),
                    "select": list(partial_query.get("select", []) or []),
                    "group_by": list(partial_query.get("group_by", []) or []),
                    "where": list(partial_query.get("where", []) or []),
                    "metric_filters": list(partial_query.get("metric_filters", []) or []),
                    "time": dict(partial_query.get("time", {}) or {}) or None,
                }
            )
            # PartialQueryState shares the attributes (group_by, time, etc.) that
            # these helpers access; safe to pass at runtime.
            candidate_entities = _predicate_context_entity_candidates(
                predicate,
                normalized,  # type: ignore[arg-type]
                config,
            )
            context_entities = _reduced_context_entities(
                config=config,
                entities=candidate_entities,
                query=normalized,  # type: ignore[arg-type]
                time_bound_relationships=_time_bound_relationship_ids(
                    normalized,  # type: ignore[arg-type]
                    config,
                ),
            )
            if not predicate.time_grain and normalized.time is not None and normalized.time.grain:
                effective_grain = str(normalized.time.grain)
        except Exception:
            context_entities = []
    return {
        "predicate_scope_mode": predicate.scope_mode,
        "predicate_entity": predicate.entity,
        "predicate_time_grain": effective_grain,
        "predicate_context_entities": context_entities,
        "predicate_filter_inheritance": "inherit_compatible"
        if predicate.scope_mode == "contextual"
        else "none",
        "predicate_count": len(predicates),
    }


def _object_card(
    runtime: Runtime, object_id: str, partial_query: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = runtime.config
    maps = _config_maps(config)
    partial_query = dict(partial_query or {})
    policy_context = _policy_context(partial_query)
    hidden_ids = hidden_object_ids(
        config,
        environment=str(policy_context.get("environment", "")),
        audience=str(policy_context.get("audience", "")),
        roles=policy_context.get("roles", []),
    )
    if object_id in hidden_ids:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND", f"Unknown object '{object_id}'", details={"object_id": object_id}
        )
    selection = _selection_context(config, partial_query)
    obj = runtime.registry.get(object_id)
    payload = dict(obj.payload or {})
    payload.update(_metric_object_payload(config, object_id, obj.kind))
    base = {
        "id": obj.id,
        "object_type": _public_object_type(obj.kind),
        "kind": obj.kind,
        "name": payload.get("name", obj.canonical_name),
        "label": payload.get("label", obj.display_name),
        "description": payload.get("description", obj.display_name),
        "recommended_next_actions": _recommended_actions(obj.kind),
    }
    if obj.kind == "measure":
        measure = maps["measures"][object_id]
        base.update(
            {
                "root_entity": measure.entity,
                "measure_class": measure.measure_class,
                "accumulation": {
                    "kind": measure.accumulation.kind,
                    "snapshot": measure.accumulation.snapshot,
                },
                "value_type": measure.value_type,
                "usage_summary": _aggregation_guidance(measure),
                **_aggregation_guidance(measure),
                "default_temporal_role": payload.get("default_temporal_role", ""),
                "compatible_temporal_roles": payload.get("compatible_temporal_roles", []),
                "related_curated_metrics": [
                    metric_id
                    for metric_id in _related_metric_ids_for_measure(config, measure)
                    if metric_id in maps["metric_recipes"]
                    and not _is_auto_metric(config, metric_id)
                ],
                "default_metric_id": _measure_default_metric_id(measure),
                "coverage_notes": list(payload.get("coverage_notes", []) or []),
                **_operational_metadata(measure),
                **_review_metadata(measure),
                **_example_test_metadata(measure),
                **_comparison_metadata(measure),
            }
        )
    elif obj.kind == "metric":
        recipe = maps["metric_recipes"][object_id]
        predicate_meta = _predicate_metadata(config, recipe.expression, partial_query)
        preference_meta = _metric_preferences(config, recipe)
        usage_summary = _metric_guidance(config, recipe)
        base.update(
            {
                "root_entity": payload.get("root_entity", ""),
                "value_type": recipe.value_type,
                "default_temporal_role": payload.get("default_temporal_role", ""),
                "compatible_temporal_roles": payload.get("compatible_temporal_roles", []),
                "expression_summary": payload.get("expression_summary", ""),
                "aggregation_mode": "user_selectable"
                if _is_auto_metric(config, recipe.id)
                else "fixed",
                "usage_summary": usage_summary,
                **usage_summary,
                "related_measures": preference_meta["related_measures"]
                or [
                    measure.id
                    for measure in config.measures
                    if _measure_default_metric_id(measure) == recipe.id
                    or measure.label.lower() in payload.get("expression_summary", "").lower()
                ],
                "executable": payload.get("executable", True),
                "unsupported_reason": payload.get("unsupported_reason", ""),
                "coverage_notes": list(payload.get("coverage_notes", []) or []),
                **_operational_metadata(recipe),
                **_review_metadata(recipe),
                **_example_test_metadata(recipe),
                **predicate_meta,
                **_comparison_metadata(recipe),
            }
        )
    elif obj.kind == "dimension":
        dim = maps["dimensions"][object_id]
        domain = _value_domain_for_dimension(config, object_id)
        sample_values, _, _ = _declared_value_rows(domain, limit=5)
        coverage_notes, null_bucket_meaning = _dimension_history_coverage(
            config, object_id, selection["root_entity"]
        )
        base.update(
            {
                "root_entity": dim.entity,
                "kind_semantics": dim.semantic_kind or dim.data_type,
                "preferred_filter_ops": list(dim.preferred_filter_ops),
                "value_domain_summary": {
                    "value_domain_id": domain.id if domain is not None else "",
                    "value_count": len(domain.values) if domain is not None else 0,
                },
                "top_values": sample_values,
                "sample_values": sample_values,
                "valid_values_lookup_required": domain is None,
                "valid_values_hint": _valid_values_lookup_hint(domain is not None),
                "related_measures": [
                    measure.id
                    for measure in config.measures
                    if _path_availability(config, measure.entity, dim.entity)["available"]
                ][:10],
                "coverage_notes": coverage_notes,
                "null_bucket_meaning": null_bucket_meaning,
            }
        )
    elif obj.kind == "entity":
        entity = maps["entities"][object_id]
        base.update(
            {
                "root_entity": entity.id,
                "key": list(entity.key),
                "allowed_as_root": entity.allowed_as_root,
                "related_measures": [
                    measure.id for measure in config.measures if measure.entity == entity.id
                ][:20],
            }
        )
    elif obj.kind == "relationship":
        base.update(_relationship_card_payload(config, object_id))
    elif obj.kind == "segment":
        normalized = normalize_segment(config, object_id)
        basis_metric = maps["metric_recipes"].get(normalized.basis_metric)
        base.update(
            {
                "root_entity": normalized.entity,
                "basis_metric": normalized.basis_metric,
                "basis_metric_label": basis_metric.label
                if basis_metric is not None
                else normalized.basis_metric,
                "member_key_dimensions": list(normalized.member_key_dimensions),
                "preview_dimensions": list(normalized.preview_dimensions),
                "member_key_dimension_labels": [
                    maps["dimensions"][dimension_id].label
                    for dimension_id in normalized.member_key_dimensions
                    if dimension_id in maps["dimensions"]
                ],
                "preview_dimension_labels": [
                    maps["dimensions"][dimension_id].label
                    for dimension_id in normalized.preview_dimensions
                    if dimension_id in maps["dimensions"]
                ],
                "membership_summary": {
                    "where_count": len(normalized.where),
                    "metric_filter_count": len(normalized.metric_filters),
                    "has_time": bool(normalized.time),
                },
                "starter_preview_request": {"segment_id": normalized.id, "limit": 50},
                "derived_query_summary": {
                    "preview_query": build_segment_query(
                        normalized, include_preview_dimensions=True
                    ),
                    "membership_query": build_segment_query(
                        normalized, include_preview_dimensions=False
                    ),
                },
            }
        )
    base["starter_query_patches"] = _starter_query_patches(
        runtime, object_id, partial_query, card=base
    )
    base.update(_policy_metadata(config, object_id, partial_query=partial_query))
    return base


def _summary_row(
    runtime: Runtime,
    obj: dict[str, Any],
    *,
    root_entity: str = "",
    verbosity: str = "compact",
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = runtime.config
    payload = dict(obj.get("payload", {}) or {})
    payload.update(_metric_object_payload(config, str(obj["id"]), str(obj["kind"])))
    if verbosity != "full":
        payload.pop("operational", None)
    availability = _availability_for_object(config, root_entity, str(obj["id"]), str(obj["kind"]))
    card = _object_card(runtime, str(obj["id"]), partial_query)
    row = _compact_summary_from_card(
        card,
        available=availability["available"],
        blocked_reason=availability.get("reason", ""),
        extra={
            "canonical_name": obj.get("canonical_name", ""),
            "display_name": obj.get("display_name", ""),
            "aliases": list(obj.get("aliases", []) or []),
            "comparison_family": card.get("comparison_family", ""),
            "comparison_mode": card.get("comparison_mode", ""),
            "clock_variants": list(card.get("clock_variants", []) or []),
            "preferred_companion_metrics": list(card.get("preferred_companion_metrics", []) or []),
        },
    )
    if obj["kind"] == "dimension":
        domain = _value_domain_for_dimension(config, str(obj["id"]))
        row.update(
            {
                "kind_semantics": payload.get("semantic_kind", payload.get("data_type", "")),
                "categorical": payload.get("semantic_kind", "") == "categorical",
                "value_domain_present": domain is not None,
                "sample_values_available": bool(domain is not None and domain.values),
                "valid_values_lookup_required": domain is None,
            }
        )
    # ``payload`` is the heavy field (19KB+ per measure on jaffle:
    # semantic_kind, validity windows, ...). At ``compact`` (the new
    # default) the catalog is for browsing — agents should
    # ``inspect`` an id to get the payload, not pull every measure's
    # payload in one shot.
    if obj["kind"] == "measure" and verbosity == "full":
        row["payload"] = payload
        row["operational"] = dict(card.get("operational", {}) or {})
    if obj["kind"] == "metric":
        row["curated_status"] = payload.get("curated_status", "")
        row["executable"] = payload.get("executable", True)
        row["unsupported_reason"] = payload.get("unsupported_reason", "")
        if verbosity == "full":
            row["payload"] = payload
            row["operational"] = dict(card.get("operational", {}) or {})
    if obj["kind"] == "segment" and verbosity == "full":
        row["payload"] = payload
    if obj["kind"] == "temporal_role":
        # Surface temporal_class so agents and discovery surfaces can
        # reason about role semantics (event_time, calendar_time,
        # as_of_time, state_time) without re-reading the YAML config.
        row.update(
            {
                "temporal_class": payload.get("temporal_class", ""),
                "supported_grains": list(payload.get("supported_grains", []) or []),
                "default_query_time_axis": bool(payload.get("default_query_time_axis", False)),
                "timezone": payload.get("timezone", "UTC"),
                "dimension": payload.get("dimension", ""),
            }
        )
    if obj["kind"] == "relationship":
        rel_payload = _relationship_card_payload(config, str(obj["id"]))
        row.update(
            {
                "source_entity": rel_payload["source_entity"],
                "target_entity": rel_payload["target_entity"],
                "cardinality": rel_payload["cardinality"],
                "safety": rel_payload["safety"],
            }
        )
        if verbosity == "full":
            row.update(
                {
                    "source_columns": rel_payload["source_columns"],
                    "target_columns": rel_payload["target_columns"],
                    "allowed_directions": rel_payload["allowed_directions"],
                    "path_preference": rel_payload["path_preference"],
                    "temporal_validity": rel_payload["temporal_validity"],
                    "contract": rel_payload["contract"],
                }
            )
    if obj["kind"] not in {"measure", "metric"} and verbosity == "full":
        row["payload"] = payload
    if obj["kind"] == "measure":
        row["usage_summary"] = _aggregation_guidance(
            _config_maps(config)["measures"][str(obj["id"])]
        )
    row.update(_policy_metadata(config, str(obj["id"]), partial_query=partial_query))
    return row


_MINIMAL_CATALOG_ROW_KEYS: frozenset[str] = frozenset({"id", "kind", "name", "label", "available"})


def _minimal_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _MINIMAL_CATALOG_ROW_KEYS if key in row}


@runtime_request_scope
def catalog_payload(
    runtime: Runtime,
    *,
    view: str = "summary",
    verbosity: str = "compact",
    kind: str = "",
    search: str = "",
    entity: str = "",
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = runtime.catalog()
    supported_capabilities, unsupported_capabilities = _capability_payload(runtime.config)
    context = _policy_context({"policy_context": dict(policy_context or {})})
    hidden_ids = hidden_object_ids(
        runtime.config,
        environment=str(context.get("environment", "")),
        audience=str(context.get("audience", "")),
        roles=context.get("roles", []),
    )
    grouped: dict[str, Any] = {
        "entities": [],
        "dimensions": [],
        "measures": [],
        "segments": [],
        "temporal_roles": [],
        "relationships": [],
        "value_domains": [],
        "metrics": [],
    }
    kind_map = {
        "entity": "entities",
        "dimension": "dimensions",
        "measure": "measures",
        "segment": "segments",
        "temporal_role": "temporal_roles",
        "relationship": "relationships",
        "value_domain": "value_domains",
        "metric": "metrics",
    }
    aliases: dict[str, list[str]] = {}
    alias_index: dict[str, list[str]] = {}
    search_norm = _norm(search)
    for raw_obj in catalog["objects"]:
        if str(raw_obj.get("id", "")) in hidden_ids:
            continue
        if kind and str(raw_obj["kind"]) != kind:
            continue
        row = _summary_row(
            runtime,
            dict(raw_obj),
            root_entity=entity,
            verbosity=verbosity,
            partial_query={"policy_context": dict(policy_context or {})},
        )
        haystack = " ".join(
            [
                str(row.get("id", "")),
                str(row.get("canonical_name", "")),
                str(row.get("display_name", "")),
                str(row.get("description", "")),
                " ".join(dict(raw_obj.get("payload", {}) or {}).get("topics", []) or []),
            ]
        )
        if search_norm and search_norm not in _norm(haystack):
            continue
        bucket = kind_map.get(str(row["kind"]))
        if not bucket:
            continue
        if view == "summary" and verbosity != "full":
            row = dict(row)
        grouped[bucket].append(row)
        aliases[row["id"]] = list(row.get("aliases", []) or [])
        for alias in [
            row.get("id"),
            row.get("canonical_name"),
            row.get("display_name"),
            row.get("name"),
            row.get("label"),
            *(row.get("aliases", []) or []),
        ]:
            alias_key = _norm(str(alias))
            if not alias_key:
                continue
            alias_index.setdefault(alias_key, [])
            if row["id"] not in alias_index[alias_key]:
                alias_index[alias_key].append(row["id"])
    grouped["meta"] = {
        "package": catalog["package"],
        "schema_version": runtime.config.version,
        "view": view,
        "verbosity": verbosity,
    }
    verbosity_norm = str(verbosity or "").lower()
    if verbosity_norm == "summary":
        # Cold-start orientation: counts + flat ID list per kind. No row
        # dicts, no descriptions, no payload — agents inspect specific
        # ids via the ``inspect`` tool. Target envelope: under 10KB even
        # on a 300-object package. Capabilities ship here too so a single
        # ``catalog?verbosity=summary`` call gives an agent everything it
        # needs to choose its next move without paying for row metadata.
        # ``kind_map`` (kind→bucket) reversed gives bucket→singular kind
        # so id-list keys read naturally as ``measure_ids``, ``entity_ids``
        # etc. rather than tripping over irregular plurals.
        bucket_to_kind = {bucket: kind for kind, bucket in kind_map.items()}
        summary_payload: dict[str, Any] = {
            "meta": grouped["meta"],
            "counts": {
                bucket_key: len(grouped[bucket_key])
                for bucket_key in list(grouped)
                if isinstance(grouped[bucket_key], list)
            },
            "capabilities": supported_capabilities,
            "unsupported_capabilities": unsupported_capabilities,
        }
        for bucket_key in list(grouped):
            bucket = grouped[bucket_key]
            if not isinstance(bucket, list):
                continue
            singular = bucket_to_kind.get(bucket_key, bucket_key)
            summary_payload[f"{singular}_ids"] = [
                str(row.get("id", "")) for row in bucket if isinstance(row, dict)
            ]
        return summary_payload
    if verbosity_norm == "minimal":
        # Strip every row to {id, kind, name, label, available} and drop
        # the alias dictionaries — both can be rebuilt by inspecting
        # individual objects once the agent has narrowed its target.
        for bucket_key in list(grouped.keys()):
            bucket = grouped[bucket_key]
            if isinstance(bucket, list):
                grouped[bucket_key] = [
                    _minimal_catalog_row(row) for row in bucket if isinstance(row, dict)
                ]
        grouped["counts"] = {
            bucket_key: len(grouped[bucket_key])
            for bucket_key in grouped
            if isinstance(grouped[bucket_key], list)
        }
    elif verbosity_norm == "full":
        # Full verbosity is opt-in: keep the alias maps for typo-resolution
        # and emit every matching row uncapped.
        grouped["aliases"] = aliases
        grouped["alias_index"] = alias_index
    else:
        # Compact (the default). Round four dropped ``alias_index`` and
        # ``aliases`` here — both grew unbounded with the catalog and
        # drowned token budgets even with ``search=...``. Callers who
        # need typo-resolution request ``verbosity=full``; callers who
        # know an object id use ``inspect``. Compact also caps each
        # bucket at ``_CATALOG_COMPACT_BUCKET_CAP`` rows and reports the
        # pre-cap totals + a ``truncated`` flag when any bucket spilled.
        counts_total: dict[str, int] = {}
        truncated_buckets: list[str] = []
        for bucket_key, bucket in list(grouped.items()):
            if not isinstance(bucket, list):
                continue
            counts_total[bucket_key] = len(bucket)
            if len(bucket) > _CATALOG_COMPACT_BUCKET_CAP:
                grouped[bucket_key] = bucket[:_CATALOG_COMPACT_BUCKET_CAP]
                truncated_buckets.append(bucket_key)
        grouped["counts_total"] = counts_total
        grouped["counts"] = {bucket_key: len(grouped[bucket_key]) for bucket_key in counts_total}
        if truncated_buckets:
            grouped["truncated"] = True
            grouped["truncated_buckets"] = sorted(truncated_buckets)
            grouped["truncation_hint"] = (
                "Compact view caps each kind at "
                f"{_CATALOG_COMPACT_BUCKET_CAP} rows. Narrow with "
                "search=/kind=/entity= or request verbosity=full to see "
                "the full inventory."
            )
    grouped["capabilities"] = supported_capabilities
    grouped["unsupported_capabilities"] = unsupported_capabilities
    return grouped


# Compact-view per-bucket row cap. Sized so a 7-kind catalog stays under
# ~5K tokens even with all buckets full. Callers asking for the full
# inventory request ``verbosity=full``.
_CATALOG_COMPACT_BUCKET_CAP: int = 200


def _relevance_score(
    terms: str,
    candidate: dict[str, Any],
    *,
    preferred_kind: str = "",
    stage: str = "initial",
    context_root_entity: str = "",
    token_doc_freq: Mapping[str, int] | None = None,
    search_document: CatalogSearchDocument | None = None,
    search_terms: SearchTerms | None = None,
) -> tuple[float, list[str]]:
    if not terms:
        base_score = _stage_kind_bonus(stage, str(candidate["kind"]))
        return base_score, ["default ordering"]
    compiled_terms = search_terms or SearchTerms.from_text(terms)
    terms_norm = compiled_terms.normalized
    tokens = compiled_terms.tokens
    score = 0.0
    reasons: list[str] = []
    requested_grain = ""
    grain_aliases: dict[str, tuple[str, ...]] = {
        "day": ("daily", "by day", "per day"),
        "week": ("weekly", "by week", "per week"),
        "month": ("monthly", "by month", "per month"),
        "quarter": ("quarterly", "by quarter", "per quarter"),
        "year": ("yearly", "by year", "per year"),
    }
    lowered_terms = compiled_terms.lowered
    for grain, markers in grain_aliases.items():
        if any(marker in lowered_terms for marker in markers):
            requested_grain = grain
            break

    document = search_document or CatalogSearchDocument.from_candidate(candidate)
    if terms_norm in document.exact_fields:
        score += 60.0
        reasons.append("exact name match")

    def _contains_token(values: Iterable[str], token: str) -> bool:
        return any(token and token in value for value in values if value)

    if _contains_token(document.derived_tokens, terms_norm) or _contains_token(
        document.topic_fields, terms_norm
    ):
        score += 25.0
        reasons.append("search-term match")

    for token in tokens:
        # Skip stopwords ("in", "by", "the", "of", ...) — they substring-match
        # too liberally (e.g. "in" inside "drink" inflated `drink_revenue_usd`
        # over `revenue_usd` on intents like "top stores by revenue in the
        # most recent full month"). Short stopwords are the dominant cause
        # of compound-prefix measures outranking their unqualified parent.
        if token in _INTENT_STOPWORDS:
            continue
        # IDF-style weight: rare tokens (e.g. "aov" in a single metric)
        # get a 2.0 multiplier, common tokens (e.g. "customer" in a
        # customer-centric package) get 0.5, otherwise 1.0. Without this
        # the term-bag scorer ranks candidates that match a common token
        # multiple times above one that matches a rare token once.
        idf_weight = _token_idf_weight(token, token_doc_freq)
        token_variants = {token}
        if token.endswith("s") and len(token) > 3:
            token_variants.add(token[:-1])
        for candidate_token in token_variants:
            if _contains_token(document.name_label_fields, candidate_token):
                score += 12.0 * idf_weight
                reasons.append(f"label/name matched '{candidate_token}'")
            if _contains_token(document.derived_tokens, candidate_token):
                score += 10.0 * idf_weight
                reasons.append(f"derived-token matched '{candidate_token}'")
            if _contains_token(document.topic_fields, candidate_token):
                score += 8.0 * idf_weight
                reasons.append(f"topic matched '{candidate_token}'")
            if _contains_token(document.description_fields, candidate_token):
                score += 6.0 * idf_weight
                reasons.append(f"description matched '{candidate_token}'")
            if _contains_token(document.comparison_fields, candidate_token):
                score += 6.0 * idf_weight
                reasons.append(f"comparison metadata matched '{candidate_token}'")
    if candidate.get("available", True):
        score += 3.0
    review_priority = (
        str(
            candidate.get("review_priority")
            or dict(candidate.get("meta", {}) or {}).get("review_priority", "")
            or ""
        )
        .strip()
        .lower()
    )
    review_boosts = {"critical": 6.0, "high": 4.0, "medium": 2.0, "low": 1.0}
    if review_priority in review_boosts:
        score += review_boosts[review_priority]
        reasons.append(f"review priority {review_priority}")
    if stage == "comparison" and candidate.get("comparison_family"):
        score += 8.0
        reasons.append("comparison family")
    if candidate.get("comparison_family") and any(
        token in tokens for token in ("vs", "versus", "compare", "comparison", "share")
    ):
        score += 6.0
        reasons.append("comparison-family match")
    if preferred_kind and candidate["kind"] == preferred_kind:
        score += 10.0
        reasons.append("preferred kind")
    identity_requested = bool(
        {"rate", "ratio", "share", "utilization", "percent", "percentage", "pct"} & set(tokens)
    ) or bool(
        re.search(r"\bper\s+(?!day\b|week\b|month\b|quarter\b|year\b)[a-z0-9_ -]+", lowered_terms)
    )
    if identity_requested:
        title = document.lowered_title
        if candidate["kind"] == "metric":
            score += 12.0
            reasons.append("metric identity requested")
            if any(
                marker in title
                for marker in ("rate", "ratio", "share", "utilization", " per ", "_per_")
            ):
                score += 8.0
                reasons.append("metric identity label match")
        elif candidate["kind"] == "measure" and not any(
            marker in title
            for marker in ("rate", "ratio", "share", "utilization", " per ", "_per_")
        ):
            score -= 8.0
            reasons.append("metric identity preferred over primitive measure")
    business_adjustment, business_reasons = _business_priority_adjustment(terms, candidate)
    if business_adjustment:
        score += business_adjustment
        reasons.extend(business_reasons)
    if requested_grain:
        title = document.lowered_title
        explicit_markers: dict[str, tuple[str, ...]] = {
            "day": ("daily",),
            "week": ("weekly",),
            "month": ("monthly",),
            "quarter": ("quarterly",),
            "year": ("yearly",),
        }
        for grain, markers in explicit_markers.items():
            if any(marker in title for marker in markers):
                if grain == requested_grain:
                    score += 18.0
                    reasons.append(f"explicit {grain} grain match")
                else:
                    score -= 30.0
                    reasons.append(f"explicit {grain} grain mismatch")
    # Cross-entity dimension penalty: blind-agent benchmark found that
    # ``revenue by store`` returned an inventory-snapshot dimension at
    # rank 1 because "store" + "day" both token-matched, even though the
    # top measure lives on a different entity. Anchor the dimension/value
    # rank to the entity the query implies. Gated on candidate.kind, not
    # preferred_kind, so callers don't have to set preferred_kind just to
    # opt into the penalty (which would also trigger an unrelated +10
    # "preferred kind" bonus).
    candidate_kind = str(candidate.get("kind", ""))
    candidate_root = str(candidate.get("root_entity", ""))
    if (
        context_root_entity
        and candidate_kind in {"dimension", "dimension_value"}
        and candidate_root
        and candidate_root != context_root_entity
    ):
        score -= 5.0
        reasons.append(
            f"cross-entity {candidate_kind} (root_entity={candidate_root}; "
            f"query implies {context_root_entity})"
        )
    score += _stage_kind_bonus(stage, str(candidate["kind"]))
    return score, list(dict.fromkeys(reasons))


# --- Intent-priority adjustments (planner ranker) -----------------------
#
# Blind-agent benchmarks identified two failure modes that pure lexical
# scoring cannot fix without help:
#
#   1. Grain double-count. An intent like "monthly revenue" yields a
#      ``time.grain=month`` slot, AND every per-token bonus that touches
#      "month" pumps the score of grain-bearing derivative measures
#      ("month_over_month_revenue_growth_ratio") above the base measure.
#
#   2. Derivative promotion. The intent "revenue by store last month"
#      promoted ``metric.sales.drink_revenue_share`` (a ratio derivative)
#      to rank 0 because share/ratio metrics tend to carry the "revenue"
#      token without an explicit qualifier — but the user didn't ask for
#      a share/ratio.
#
# We apply post-discover adjustments in the planner path only — discover
# itself stays purely lexical so catalog-wide scoring (e.g. "top stores by
# revenue" via the IDF gate) is undisturbed.

_INTENT_GRAIN_TOKENS: frozenset[str] = frozenset(
    {
        "day",
        "daily",
        "week",
        "weekly",
        "month",
        "monthly",
        "quarter",
        "quarterly",
        "year",
        "yearly",
    }
)

_DERIVATIVE_LABEL_MARKERS: tuple[str, ...] = (
    "ratio",
    "share",
    "growth",
    "conversion",
    "mom",
    "yoy",
)

_DERIVATIVE_INTENT_KEYWORDS: tuple[str, ...] = (
    "growth",
    "ratio",
    "share",
    "%",
    "percent",
    "vs",
    "over",
    "change",
    "delta",
    "lift",
    "compared",
    "versus",
    "against",
)


def _intent_grain_from_text(text: str) -> str:
    """Return the time grain implied by the intent text (e.g. ``month`` for
    "monthly revenue"), or ``""`` when none."""
    lowered = str(text or "").lower()
    grain_markers = {
        "day": ("daily", "by day", "per day"),
        "week": ("weekly", "by week", "per week"),
        "month": ("monthly", "by month", "per month", "last month", "this month"),
        "quarter": ("quarterly", "by quarter", "per quarter", "last quarter", "this quarter"),
        "year": ("yearly", "by year", "per year", "last year", "this year"),
    }
    for grain, markers in grain_markers.items():
        if any(marker in lowered for marker in markers):
            return grain
    return ""


def _label_has_derivative_marker(candidate: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(candidate.get("id", "") or ""),
            str(candidate.get("name", "") or ""),
            str(candidate.get("label", "") or ""),
            str(candidate.get("description", "") or ""),
        ]
    ).lower()
    return any(marker in text for marker in _DERIVATIVE_LABEL_MARKERS)


_PERIOD_SHIFT_RE = re.compile(
    r"\b(?:this|current|last|previous|prior|next)\s+"
    r"(?:day|week|month|quarter|year)\b"
)


def _intent_is_period_shift(intent: str) -> bool:
    """Return True when an intent looks like a period-shift comparison
    ("X this month vs last month") rather than a categorical comparison
    ("food vs drink"). Period-shift intents are still asking for the
    base measure on both sides — derivative share/ratio metrics are the
    wrong answer."""
    lowered = str(intent or "").lower()
    matches = _PERIOD_SHIFT_RE.findall(lowered)
    return len(matches) >= 1 and any(
        marker in lowered for marker in (" vs ", " versus ", " compared ", " over ")
    )


def _intent_invokes_derivative(intent: str) -> bool:
    """Return True when the intent text explicitly invokes a derivative
    pattern (growth/ratio/share/% etc.) — which means a share/ratio
    candidate is legitimate and should NOT be demoted.

    Period-shift comparisons ("X this month vs last month") use the
    comparison keywords ("vs", "over") but want the base measure on
    both sides, not a derivative metric — they are excluded here so the
    demotion still applies.
    """
    lowered = str(intent or "").lower()
    if _intent_is_period_shift(lowered):
        # Period-shift "vs" / "over" doesn't count as a derivative
        # invocation — the user wants the base measure on both sides.
        strong_keywords = (
            "growth",
            "ratio",
            "share",
            "%",
            "percent",
            "change",
            "delta",
            "lift",
        )
        return any(keyword in lowered for keyword in strong_keywords)
    return any(keyword in lowered for keyword in _DERIVATIVE_INTENT_KEYWORDS)


def _apply_intent_priority_adjustments(
    rows: list[dict[str, Any]],
    *,
    intent: str,
    grain_token_bonus: float = 12.0,
    derivative_penalty: float = 40.0,
    family_tie_break_epsilon: float = 0.05,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Adjust discover candidate scores for the planner intent path.

    Returns ``(rows_sorted_descending_by_score, demotion_notes)``. Rows are
    returned as a new list — input is not mutated.

    Adjustments applied (in order):

    * Subtract a per-token grain bonus when the intent yields a grain
      (``"month"`` in "monthly revenue") AND the candidate label/name/topic
      contains a grain token. This unwinds the per-token bonus
      ``_relevance_score`` added for the grain token — without this the
      grain double-counts.
    * Penalize candidates whose label/description matches a derivative
      marker (``ratio``/``share``/``growth``/...) when the intent contains
      no comparison/derivative keyword. Drops score by
      ``derivative_penalty`` (default 40) so base measures outrank
      derivatives in normal cases.
    * Tie-break by ``comparison_family``: when two candidates' adjusted
      scores are within ``family_tie_break_epsilon``, prefer the one whose
      ``comparison_family`` is empty/absent (a base measure) over a
      derivative family (``product_mix_share``, ``product_mix_ratio``).

    The intent_grain bonus rationale: ``_relevance_score`` adds up to
    ``12.0 * idf_weight`` per matching label/name token, and at IDF 1.0
    that's the dominant contribution. We undo a single such bonus per
    candidate that legitimately carries the grain token in its name.
    """
    if not rows:
        return list(rows), []
    grain = _intent_grain_from_text(intent)
    derivative_keywords_in_intent = _intent_invokes_derivative(intent)
    demotion_notes: list[str] = []
    adjusted: list[tuple[float, dict[str, Any], str, bool]] = []
    for row in rows:
        score = float(row.get("score", 0.0) or 0.0)
        comparison_family = str(row.get("comparison_family", "") or "")
        candidate_text_tokens = set()
        for source in (
            str(row.get("label", "") or ""),
            str(row.get("name", "") or ""),
            str(row.get("id", "") or ""),
        ):
            candidate_text_tokens.update(_tokenize(source))
        adjustment_reasons: list[str] = []
        if grain and (candidate_text_tokens & _INTENT_GRAIN_TOKENS):
            score -= grain_token_bonus
            adjustment_reasons.append(
                f"removed grain double-count for '{grain}' "
                f"(token already consumed by time.grain slot)"
            )
        is_derivative = _label_has_derivative_marker(row)
        demoted_derivative = False
        if is_derivative and not derivative_keywords_in_intent:
            score -= derivative_penalty
            adjustment_reasons.append(
                "demoted derivative (intent has no growth/ratio/share/vs/% keyword)"
            )
            demoted_derivative = True
            demotion_notes.append(
                "Demoted derivative candidate "
                f"{row.get('id', '')!r} because intent contained no "
                "comparison/derivative keywords."
            )
        new_row = dict(row)
        new_row["score"] = score
        if adjustment_reasons:
            new_row["match_reasons"] = [
                *list(row.get("match_reasons", []) or []),
                *adjustment_reasons,
            ]
        adjusted.append((score, new_row, comparison_family, demoted_derivative))
    # Sort by score desc, then base-measure tie-break (empty
    # comparison_family wins), then deterministic id tie-break.
    adjusted.sort(
        key=lambda item: (
            -item[0],
            1 if item[2] else 0,
            str(item[1].get("id", "")),
        )
    )
    # Apply family tie-break across the ε-band: if a base-measure
    # candidate exists within ε of the leader, promote it to rank 0.
    if adjusted:
        leader_score = adjusted[0][0]
        leader_family = adjusted[0][2]
        if leader_family:
            for idx in range(1, len(adjusted)):
                candidate_score, _, candidate_family, _ = adjusted[idx]
                if leader_score - candidate_score > family_tie_break_epsilon:
                    break
                if not candidate_family:
                    promoted = adjusted.pop(idx)
                    adjusted.insert(0, promoted)
                    demotion_notes.append(
                        "Tie-break: base measure "
                        f"{promoted[1].get('id', '')!r} preferred over "
                        f"derivative family {leader_family!r} within ε={family_tie_break_epsilon}."
                    )
                    break
    return [item[1] for item in adjusted], list(dict.fromkeys(demotion_notes))


def _unmatched_grouping_assumptions(
    runtime: Runtime,
    *,
    text: str,
    chosen_group_dims: list[str],
    query: dict[str, Any],
) -> list[str]:
    """Return assumption notes for grouping terms in the intent text
    that didn't resolve to an available dimension.

    The agent UX failure: user says "orders by customer status", we
    pick ``customer_id`` (no reachable status dim) and silently drop
    the status request. The agent thinks we honored the intent. This
    helper flags the gap so the agent sees we couldn't match.
    """
    notes: list[str] = []
    requested = _requested_grouping_terms(text)
    if not requested:
        return notes
    # Build per-dim chosen tokens so we can check that EACH meaningful
    # token in the requested term is covered by some chosen dim.
    chosen_per_dim: list[set[str]] = [set(_tokenize(dim_id)) for dim_id in chosen_group_dims]
    # Broad entity-name stopwords — these substring-match common
    # dimension ids ("customer" matches customer_id, customer_name,
    # customer_first_order_at) and would mask a missing qualifier.
    weak_tokens = {
        "the",
        "a",
        "an",
        "and",
        "of",
        "by",
        "customer",
        "store",
        "stores",
        "product",
        "order",
        "orders",
        "item",
        "session",
    }
    for term in requested:
        if _is_temporal_grouping_term(term):
            continue
        term_tokens = set(_tokenize(term))
        if not term_tokens:
            continue
        # If the requested term has a SPECIFIC qualifier token (not in
        # the weak/entity set), require some chosen dim to carry that
        # qualifier. If none does, the group is "covered" in name only.
        specific_tokens = term_tokens - weak_tokens
        if specific_tokens:
            if any(specific_tokens & dim_tokens for dim_tokens in chosen_per_dim):
                continue
        elif chosen_per_dim and any(term_tokens & dim_tokens for dim_tokens in chosen_per_dim):
            continue
        notes.append(
            f"Could not match a reachable dimension for grouping term "
            f"{term!r}; the closest semantic candidate was not reachable "
            f"from the query's root entity."
        )
    return notes


def _maybe_force_promote_for_intent(
    candidates: list[dict[str, Any]],
    *,
    interpreted_intent: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """If ``interpreted_intent.measure`` (or ``target_measure``) names a
    measure the intent parser already extracted, promote the matching
    candidate to rank 0 regardless of score.

    Defensive: returns the list unchanged when no explicit measure is
    set, when there are zero/one candidates, or when no candidate matches.
    """
    if not candidates or len(candidates) < 2:
        return list(candidates)
    interpreted = dict(interpreted_intent or {})
    explicit = str(interpreted.get("measure") or interpreted.get("target_measure") or "")
    if not explicit:
        return list(candidates)
    for idx, candidate in enumerate(candidates):
        if idx == 0:
            continue
        ir = dict(candidate.get("candidate_ir", {}) or {})
        for sel in ir.get("select") or []:
            expr = dict(sel.get("expression") or {})
            ident = str(expr.get("measure") or expr.get("metric") or "")
            if ident == explicit:
                promoted = list(candidates)
                row = promoted.pop(idx)
                promoted.insert(0, row)
                return promoted
    return list(candidates)


def _is_identifier_like_dimension(dim: Any) -> bool:
    label = _norm(str(getattr(dim, "label", "") or ""))
    name = _norm(str(getattr(dim, "name", "") or ""))
    semantic_kind = _norm(
        str(getattr(dim, "semantic_kind", "") or getattr(dim, "data_type", "") or "")
    )
    return bool(
        semantic_kind == "id"
        or label.endswith(" id")
        or name.endswith("_id")
        or name.endswith(".id")
    )


def _dimension_builder_score(
    dim: Any,
    card: dict[str, Any],
    *,
    focus_terms: str,
    focus_measure: Any | None,
    stage: str,
) -> tuple[float, list[str]]:
    lowered_focus_terms = str(focus_terms or "").lower()
    grouping_terms = ""
    top_match = re.search(
        r"^(?:top|highest|lowest)\s+([a-z0-9 _-]+?)\s+by\s+([a-z0-9 _-]+)$", lowered_focus_terms
    )
    if top_match:
        grouping_terms = top_match.group(1).strip()
    else:
        by_match = re.search(r"\bby ([a-z0-9 _-]+)", lowered_focus_terms)
        if by_match:
            grouping_terms = by_match.group(1).strip()
    if grouping_terms in {"day", "week", "month", "quarter", "year"}:
        grouping_terms = ""
    relevance_terms = grouping_terms or focus_terms
    score, reasons = _relevance_score(
        relevance_terms,
        {
            "id": dim.id,
            "kind": "dimension",
            "name": card.get("name", ""),
            "label": card.get("label", ""),
            "description": card.get("description", ""),
            "topics": list(getattr(dim, "topics", []) or []),
            "available": True,
            "comparison_family": "",
            "comparison_peers": [],
            "clock_variants": [],
        },
        preferred_kind="dimension",
        stage=stage,
    )
    score += 12.0
    reasons = [*reasons, "valid at current root entity"]
    semantic_kind = _norm(
        str(getattr(dim, "semantic_kind", "") or getattr(dim, "data_type", "") or "")
    )
    label_norm = _norm(str(card.get("label", "") or ""))
    focus_tokens = _tokenize(relevance_terms)
    time_requested = any(
        token in lowered_focus_terms
        for token in (
            "time",
            "trend",
            "trending",
            "historical",
            "history",
            "daily",
            "weekly",
            "monthly",
            "quarterly",
            "yearly",
            "by day",
            "by week",
            "by month",
            "by quarter",
            "by year",
        )
    )
    grouping_tokens = _tokenize(grouping_terms)
    if semantic_kind == "categorical":
        score += 8.0
        reasons.append("categorical breakdown")
    if semantic_kind in {"date", "timestamp", "datetime"}:
        score += 6.0
        reasons.append("human-readable time field")
        if not time_requested:
            score -= 14.0
            reasons.append("time breakdown not explicitly requested")
    if "name" in label_norm:
        score += 5.0
        reasons.append("display label")
    if any(token in label_norm for token in ("type", "segment", "category")):
        score += 6.0
        reasons.append("business grouping")
    for token in focus_tokens:
        token_variants = {token}
        if token.endswith("s") and len(token) > 3:
            token_variants.add(token[:-1])
        for candidate_token in token_variants:
            if candidate_token and candidate_token in label_norm:
                score += 10.0
                reasons.append(f"label matched '{candidate_token}'")
    if grouping_tokens:
        grouping_match = False
        for token in grouping_tokens:
            token_variants = {token}
            if token.endswith("s") and len(token) > 3:
                token_variants.add(token[:-1])
            if any(
                candidate_token and candidate_token in label_norm
                for candidate_token in token_variants
            ):
                grouping_match = True
                score += 16.0
                reasons.append(f"grouping phrase matched '{token}'")
        if not grouping_match:
            score -= 8.0
            reasons.append("grouping phrase mismatch")
    if list(card.get("coverage_notes", []) or []) and any(
        token in str(focus_terms or "").lower() for token in ("historical", "history")
    ):
        score += 12.0
        reasons.append("history-backed breakdown")
    if _is_identifier_like_dimension(dim):
        score -= 18.0
        reasons.append("technical identifier dimension")
        if grouping_tokens and "id" not in focus_tokens and "identifier" not in lowered_focus_terms:
            score -= 12.0
            reasons.append("display dimension preferred over identifier")
    else:
        score += 5.0
        reasons.append("human-readable breakdown")
    return score, list(dict.fromkeys(reasons))


def _card_comparison_family(runtime: Runtime, choice: dict[str, Any]) -> str:
    if not choice:
        return ""
    try:
        card = _object_card(runtime, str(choice["id"]))
    except SemanticLayerError:
        return ""
    return str(card.get("comparison_family", "") or "")


def _prefer_clock_variant(
    runtime: Runtime,
    current_choice: dict[str, Any],
    clock_choice: dict[str, Any],
) -> bool:
    if not clock_choice:
        return False
    if not current_choice:
        return True
    current_family = _card_comparison_family(runtime, current_choice)
    clock_family = _card_comparison_family(runtime, clock_choice)
    if clock_family and current_family != clock_family:
        return True
    return str(current_choice.get("id", "")) != str(clock_choice.get("id", ""))


@runtime_request_scope
def discover_payload(
    runtime: Runtime,
    *,
    terms: str,
    kinds: list[str] | None = None,
    partial_query: dict[str, Any] | None = None,
    stage: str = "",
    verbosity: str = "compact",
    limit: int = 10,
    enforce_scope: bool = False,
) -> dict[str, Any]:
    config = runtime.config
    search_index = runtime._get_catalog_search_index()
    search_terms = SearchTerms.from_text(terms)
    partial_query = dict(partial_query or {})
    # When invoked from the HTTP boundary (``enforce_scope=True``), gate
    # the response on the same classifier ``validate``/``compile`` use
    # and a content-token relevance floor against the package catalog.
    # Internal guided flows leave ``enforce_scope=False`` so builder and
    # planner candidate generation can keep working with partial context.
    if enforce_scope and str(terms or "").strip():
        classification = classify_question(str(terms))
        if classification.category != "data_query":
            return {
                "terms": terms,
                "stage": stage,
                "verbosity": verbosity,
                "query_state": _query_state(partial_query),
                "selection_context": {
                    "root_entity": "",
                    "selected_measure_ids": [],
                    "selected_metric_recipe_ids": [],
                },
                "measures": [],
                "metrics": [],
                "segments": [],
                "dimensions": [],
                "dimension_values": [],
                "entities": [],
                "blocked": [],
                "out_of_scope": _scope_block_payload(str(terms), classification),
            }
        catalog_tokens = _catalog_token_index(config, search_index=search_index)
        passes, overlap = _intent_passes_relevance_floor(str(terms), catalog_tokens)
        if not passes:
            sample = sorted(catalog_tokens)[:30]
            return {
                "terms": terms,
                "stage": stage,
                "verbosity": verbosity,
                "query_state": _query_state(partial_query),
                "selection_context": {
                    "root_entity": "",
                    "selected_measure_ids": [],
                    "selected_metric_recipe_ids": [],
                },
                "measures": [],
                "metrics": [],
                "segments": [],
                "dimensions": [],
                "dimension_values": [],
                "entities": [],
                "blocked": [],
                "low_relevance": _low_relevance_block(
                    str(terms), overlap_tokens=overlap, catalog_token_sample=sample
                ),
            }
    policy_context = _policy_context(partial_query)
    hidden_ids = hidden_object_ids(
        config,
        environment=str(policy_context.get("environment", "")),
        audience=str(policy_context.get("audience", "")),
        roles=policy_context.get("roles", []),
    )
    selection = _selection_context(config, partial_query)
    root_entity = selection["root_entity"]
    stage = _infer_stage(partial_query, stage, terms)
    kinds = list(kinds or [])
    maps = _config_maps(config)
    records: list[dict[str, Any]] = []

    # Pre-compute per-token document frequency once per discover call.
    # Threaded into every `_relevance_score` invocation so rare query
    # tokens (e.g. "aov") outrank common ones (e.g. "customer" in a
    # customer-centric package).
    #
    # IDF is gated on ``enforce_scope`` — the same flag the HTTP boundary
    # sets. Internal guided flows leave it False so their ranker stays
    # purely lexical and builder-step scoring stays stable.
    token_doc_freq: Mapping[str, int] | None = (
        _catalog_token_doc_freq(config, search_index=search_index) if enforce_scope else None
    )

    for measure in config.measures:
        if measure.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, measure.id, "measure")
        row = {
            "id": measure.id,
            "kind": "measure",
            "name": measure.name,
            "label": measure.label,
            "description": measure.description,
            "topics": list(measure.topics),
            "review_priority": str((measure.meta or {}).get("review_priority", "") or ""),
            "available": availability["available"],
            "blocked_reason": availability.get("reason", ""),
            "root_entity": measure.entity,
            "default_temporal_role": measure.compatible_temporal_roles[0]
            if measure.compatible_temporal_roles
            else "",
            "recommended_next_actions": _recommended_actions("measure"),
            **_comparison_metadata(measure),
        }
        score, reasons = _relevance_score(
            terms,
            row,
            preferred_kind="measure",
            stage=stage,
            token_doc_freq=token_doc_freq,
            search_document=search_index.document(measure.id),
            search_terms=search_terms,
        )
        row.update({"score": score, "match_reasons": reasons, "object_type": "measure"})
        records.append(row)

    for recipe in config.metric_recipes:
        if recipe.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, recipe.id, "metric")
        executable, unsupported_reason = _metric_executable(recipe, config, partial_query)
        predicate_backed = bool(_predicate_exprs_from_expr(config, recipe.expression))
        row = {
            "id": recipe.id,
            "kind": "metric",
            "name": recipe.name,
            "label": recipe.label,
            "description": recipe.description,
            "topics": list(recipe.topics),
            "review_priority": str((recipe.meta or {}).get("review_priority", "") or ""),
            "available": availability["available"] and executable,
            "blocked_reason": unsupported_reason or availability.get("reason", ""),
            "root_entity": _first_root_entity(config, recipe.expression),
            "default_temporal_role": recipe.temporal_role
            or (
                _compatible_temporal_roles_for_expr(config, recipe.expression)[0]
                if _compatible_temporal_roles_for_expr(config, recipe.expression)
                else ""
            ),
            "recommended_next_actions": _recommended_actions("metric"),
            **_comparison_metadata(recipe),
        }
        score, reasons = _relevance_score(
            terms,
            row,
            preferred_kind="metric",
            stage=stage,
            token_doc_freq=token_doc_freq,
            search_document=search_index.document(recipe.id),
            search_terms=search_terms,
        )
        lowered_terms = str(terms or "").lower()
        predicate_terms = (
            "more than",
            "less than",
            "greater than",
            "fewer than",
            "repeat",
            "high value",
            "at least",
            "threshold",
            "10+",
            "2+",
            "3+",
        )
        if predicate_backed and not any(token in lowered_terms for token in predicate_terms):
            score -= 18.0
            reasons.append("predicate semantics not explicitly requested")
        if _is_auto_metric(config, recipe.id):
            score -= 4.0
            reasons.append("auto-published duplicate")
        row.update(
            {"score": score, "match_reasons": list(dict.fromkeys(reasons)), "object_type": "metric"}
        )
        records.append(row)

    for segment in config.segments:
        if segment.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, segment.id, "segment")
        row = {
            "id": segment.id,
            "kind": "segment",
            "name": segment.name,
            "label": segment.label,
            "description": segment.description,
            "topics": list(segment.topics),
            "available": availability["available"],
            "blocked_reason": availability.get("reason", ""),
            "root_entity": segment.entity,
            "default_temporal_role": "",
            "recommended_next_actions": _recommended_actions("segment"),
        }
        score, reasons = _relevance_score(
            terms,
            row,
            preferred_kind="segment",
            stage=stage,
            token_doc_freq=token_doc_freq,
            search_document=search_index.document(segment.id),
            search_terms=search_terms,
        )
        row.update({"score": score, "match_reasons": reasons, "object_type": "segment"})
        records.append(row)

    # Infer the root entity the query implies — but only when the top
    # three measure/metric candidates agree on it. The "single top
    # candidate wins" heuristic breaks down when a high-priority
    # ratio/growth metric outscores the user's actual target on
    # bonus-stacking; requiring agreement filters those one-offs while
    # still anchoring cleanly when the top results converge (e.g.
    # "revenue by store" — all top revenue measures live on
    # entity.jaffle_order, so the inference is unambiguous).
    top_three_entities: list[str] = []
    for row in sorted(
        (row for row in records if row["kind"] in {"measure", "metric"}),
        key=lambda row: -float(row.get("score", 0.0) or 0.0),
    )[:3]:
        entity = str(row.get("root_entity", "") or "")
        if entity:
            top_three_entities.append(entity)
    # Infer when at least two of the top three (non-empty-root)
    # candidates agree on a root entity. The penalty is intentionally
    # small (a tiebreak, not a domination) so downstream planners
    # that depend on dimension ranking still surface the obvious
    # candidates first.
    inferred_root_entity = ""
    if top_three_entities:
        leader = top_three_entities[0]
        if top_three_entities.count(leader) >= 2:
            inferred_root_entity = leader

    for dim in config.dimensions:
        if dim.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, dim.id, "dimension")
        row = {
            "id": dim.id,
            "kind": "dimension",
            "name": dim.name,
            "label": dim.label,
            "description": dim.description,
            "topics": list(dim.topics),
            "available": availability["available"],
            "blocked_reason": availability.get("reason", ""),
            "root_entity": dim.entity,
            "default_temporal_role": "",
            "recommended_next_actions": _recommended_actions("dimension"),
        }
        score, reasons = _relevance_score(
            terms,
            row,
            stage=stage,
            context_root_entity=inferred_root_entity,
            token_doc_freq=token_doc_freq,
            search_document=search_index.document(dim.id),
            search_terms=search_terms,
        )
        row.update({"score": score, "match_reasons": reasons, "object_type": "dimension"})
        records.append(row)

    for entity in config.entities:
        if entity.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, entity.id, "entity")
        row = {
            "id": entity.id,
            "kind": "entity",
            "name": entity.name,
            "label": entity.label,
            "description": entity.description,
            "topics": list(entity.topics),
            "available": availability["available"],
            "blocked_reason": availability.get("reason", ""),
            "root_entity": entity.id,
            "default_temporal_role": "",
            "recommended_next_actions": _recommended_actions("entity"),
        }
        score, reasons = _relevance_score(
            terms,
            row,
            stage=stage,
            token_doc_freq=token_doc_freq,
            search_document=search_index.document(entity.id),
            search_terms=search_terms,
        )
        row.update({"score": score, "match_reasons": reasons, "object_type": "entity"})
        records.append(row)

    dimension_values: list[dict[str, Any]] = []
    for domain in config.value_domains:
        dim = maps["dimensions"].get(domain.dimensions[0]) if domain.dimensions else None
        if dim is None:
            continue
        if dim.id in hidden_ids:
            continue
        availability = _availability_for_object(config, root_entity, dim.id, "dimension")
        for value in domain.values:
            row = {
                "id": f"{dim.id}={value.value}",
                "kind": "dimension_value",
                "name": str(value.value),
                "label": value.label,
                "description": value.description or value.label,
                "topics": list(dim.topics),
                "available": availability["available"],
                "blocked_reason": availability.get("reason", ""),
                "root_entity": dim.entity,
                "default_temporal_role": "",
                "recommended_next_actions": _recommended_actions("dimension_value"),
                "dimension_id": dim.id,
                "value": value.value,
            }
            score, reasons = _relevance_score(
                terms,
                row,
                preferred_kind="dimension_value",
                stage=stage,
                context_root_entity=inferred_root_entity,
                token_doc_freq=token_doc_freq,
                search_document=search_index.document(str(row["id"])),
                search_terms=search_terms,
            )
            row.update(
                {"score": score + 2.0, "match_reasons": reasons, "object_type": "dimension_value"}
            )
            dimension_values.append(row)

    if kinds:
        kind_set = set(kinds)
        records = [row for row in records if row["kind"] in kind_set]
        dimension_values = [row for row in dimension_values if row["kind"] in kind_set]

    def _sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: (
                -float(row["score"]),
                not bool(row["available"]),
                row["label"],
                row["id"],
            ),
        )[:limit]

    # When the user provided a non-trivial search term but no candidate
    # actually matched it (the "asdjkfhqwiuer" case), drop the floor of
    # static-bonus-only rows so agents see a clear empty-set signal
    # instead of three near-identically-scored "review priority high"
    # candidates. Match reasons that only reflect bookkeeping bonuses
    # (preferred kind, review priority, comparison family, default
    # ordering, "valid at...") don't count as a real match.
    has_real_term = bool(str(terms or "").strip())

    def _is_real_match(row: dict[str, Any]) -> bool:
        reasons = list(row.get("match_reasons", []) or [])
        for reason in reasons:
            text = str(reason or "")
            if (
                "matched" in text  # label/search/topic/description/example match
                or text.startswith("exact name match")
                or text.startswith("search-term match")
                or text.startswith("value domain matched")
            ):
                return True
        return False

    all_records = list(records)
    all_dimension_values = list(dimension_values)
    if has_real_term:
        records = [row for row in records if _is_real_match(row)]
        dimension_values = [row for row in dimension_values if _is_real_match(row)]

    measures = _sort([row for row in records if row["kind"] == "measure"])
    metrics = _sort([row for row in records if row["kind"] == "metric"])
    segments = _sort([row for row in records if row["kind"] == "segment"])
    dimensions = _sort([row for row in records if row["kind"] == "dimension"])
    entities = _sort([row for row in records if row["kind"] == "entity"])
    values = _sort(dimension_values)
    blocked = _sort([row for row in [*records, *dimension_values] if row["blocked_reason"]])
    no_matches = has_real_term and not (
        measures or metrics or segments or dimensions or values or entities
    )

    # Token-AND fallback: when the holistic single-string match returns
    # nothing, retry by splitting on whitespace, scoring each token
    # independently, and intersecting the per-token matcher sets. This is
    # the path that catches multi-word intents the reviewer flagged
    # (``"order count"``, ``"distinct customer count"``) where each token
    # would resolve on its own but the combined string slips below the
    # match floor. Cap the token count to avoid pathological inputs.
    fallback_token_match_reason: str | None = None
    unique_tokens: list[str] = []
    if no_matches:
        tokens = [tok for tok in _tokenize(terms) if tok and tok not in _INTENT_STOPWORDS]
        # Dedupe while preserving order.
        seen: set[str] = set()
        for tok in tokens:
            if tok in seen:
                continue
            seen.add(tok)
            unique_tokens.append(tok)
        del unique_tokens[5:]
        # Gate to short, noun-phrase-style inputs (2–3 distinct meaningful
        # tokens). Longer inputs are natural-language intents that
        # planner fallback passes through ``discover_payload`` internally;
        # the per-token intersection on a long sentence can promote a
        # measure whose label happens to match a stray token, swamping
        # the holistic ranker's better candidate. The reviewer's failing
        # cases (``"order count"``, ``"distinct customer count"``) all
        # fit the 2–3 token window.
        if 2 <= len(unique_tokens) <= 3:
            # Build per-token scored views of every record.
            per_token_scores: dict[str, dict[str, tuple[float, list[str]]]] = {}
            per_token_dimval_scores: dict[str, dict[str, tuple[float, list[str]]]] = {}
            for tok in unique_tokens:
                token_search_terms = SearchTerms.from_text(tok)
                per_token_scores[tok] = {}
                per_token_dimval_scores[tok] = {}
                for row in all_records:
                    score, reasons = _relevance_score(
                        tok,
                        row,
                        preferred_kind=str(row.get("kind", "")),
                        stage=stage,
                        token_doc_freq=token_doc_freq,
                        search_document=search_index.document(str(row.get("id", ""))),
                        search_terms=token_search_terms,
                    )
                    row_proxy = {"match_reasons": reasons}
                    if _is_real_match(row_proxy):
                        per_token_scores[tok][row["id"]] = (score, reasons)
                for row in all_dimension_values:
                    score, reasons = _relevance_score(
                        tok,
                        row,
                        preferred_kind="dimension_value",
                        stage=stage,
                        token_doc_freq=token_doc_freq,
                        search_document=search_index.document(str(row.get("id", ""))),
                        search_terms=token_search_terms,
                    )
                    row_proxy = {"match_reasons": reasons}
                    if _is_real_match(row_proxy):
                        per_token_dimval_scores[tok][row["id"]] = (score, reasons)
            # Intersect the per-token matcher sets.
            shared_ids = (
                set.intersection(*(set(per_token_scores[tok].keys()) for tok in unique_tokens))
                if unique_tokens
                else set()
            )
            shared_dimval_ids = (
                set.intersection(
                    *(set(per_token_dimval_scores[tok].keys()) for tok in unique_tokens)
                )
                if unique_tokens
                else set()
            )
            if shared_ids or shared_dimval_ids:
                fallback_token_match_reason = (
                    f"multi_token_fallback: {' + '.join(repr(t) for t in unique_tokens)}"
                )
                index_by_id = {row["id"]: row for row in all_records}
                value_index_by_id = {row["id"]: row for row in all_dimension_values}
                fallback_records: list[dict[str, Any]] = []
                for object_id in shared_ids:
                    base = dict(index_by_id[object_id])
                    summed_score = sum(per_token_scores[tok][object_id][0] for tok in unique_tokens)
                    collated_reasons: list[str] = []
                    for tok in unique_tokens:
                        collated_reasons.extend(per_token_scores[tok][object_id][1])
                    collated_reasons.append(fallback_token_match_reason)
                    base.update(
                        {
                            "score": summed_score,
                            "match_reasons": list(dict.fromkeys(collated_reasons)),
                        }
                    )
                    fallback_records.append(base)
                fallback_dimvals: list[dict[str, Any]] = []
                for object_id in shared_dimval_ids:
                    base = dict(value_index_by_id[object_id])
                    summed_score = sum(
                        per_token_dimval_scores[tok][object_id][0] for tok in unique_tokens
                    )
                    collated_reasons = []
                    for tok in unique_tokens:
                        collated_reasons.extend(per_token_dimval_scores[tok][object_id][1])
                    collated_reasons.append(fallback_token_match_reason)
                    base.update(
                        {
                            "score": summed_score,
                            "match_reasons": list(dict.fromkeys(collated_reasons)),
                        }
                    )
                    fallback_dimvals.append(base)
                if kinds:
                    kind_set = set(kinds)
                    fallback_records = [r for r in fallback_records if r["kind"] in kind_set]
                    fallback_dimvals = [r for r in fallback_dimvals if r["kind"] in kind_set]
                measures = _sort([row for row in fallback_records if row["kind"] == "measure"])
                metrics = _sort([row for row in fallback_records if row["kind"] == "metric"])
                segments = _sort([row for row in fallback_records if row["kind"] == "segment"])
                dimensions = _sort([row for row in fallback_records if row["kind"] == "dimension"])
                entities = _sort([row for row in fallback_records if row["kind"] == "entity"])
                values = _sort(fallback_dimvals)
                blocked = _sort(
                    [row for row in [*fallback_records, *fallback_dimvals] if row["blocked_reason"]]
                )
                no_matches = not (
                    measures or metrics or segments or dimensions or values or entities
                )

    # Ship a single ready-to-run starter_query_patch on each selectable
    # candidate so blind agents can copy a runnable IR seed directly from
    # discover without a round-trip through inspect. measures/metrics
    # seed `select`; dimensions seed `group_by`. Other kinds (segments,
    # entities, dimension_values, blocked) intentionally skip — they
    # don't map to a single canonical clause.
    for row in [*measures, *metrics]:
        row["starter_query_patch"] = _query_patch_with_selection(
            runtime,
            partial_query,
            {"id": row["id"], "kind": row["kind"], "label": row["label"]},
        )
    for row in dimensions:
        row["starter_query_patch"] = _query_patch_with_group_by(partial_query, row["id"])

    payload = {
        "terms": terms,
        "stage": stage,
        "verbosity": verbosity,
        "query_state": _query_state(partial_query),
        "selection_context": {
            "root_entity": root_entity,
            "selected_measure_ids": selection["selected_measure_ids"],
            "selected_metric_recipe_ids": selection["selected_metric_recipe_ids"],
        },
        "measures": measures,
        "metrics": metrics,
        "segments": segments,
        "dimensions": dimensions,
        "dimension_values": values,
        "entities": entities,
        "blocked": blocked,
    }
    if fallback_token_match_reason is not None and not no_matches:
        payload["match_strategy"] = {
            "kind": "multi_token_fallback",
            "reason": fallback_token_match_reason,
            "tokens": list(unique_tokens),
        }
    if no_matches:
        payload["no_matches"] = {
            "terms": terms,
            "reason": "no candidate matched the supplied search terms",
            "recovery_hint": "Try simpler or more specific terms, or call the `catalog` tool to browse available objects.",
        }
    # Empty-`terms` warning. The blind-agent benchmark caught a probe
    # that passed `term` (singular) and silently got back default-ordering
    # candidates — easy to mistake for a real match. Emit a structured
    # warning so callers don't trust the default ordering as a relevance
    # signal. Only fires when the caller passed no terms at all (an
    # internal flow like build-options' measure-stage discover invokes
    # this deliberately with terms="").
    if not str(terms or "").strip():
        payload["warnings"] = [
            *list(payload.get("warnings", []) or []),
            {
                "code": "DISCOVER_NO_TERMS",
                "message": (
                    "No `terms` provided; results are returned in default catalog order "
                    "and should not be treated as relevance-ranked. The argument is "
                    "spelled `terms` (plural); a common typo is `term` (singular)."
                ),
                "recovery_hint": (
                    "Pass `terms` as a string of business words "
                    "(e.g. 'revenue by store'), or call the `catalog` tool to "
                    "browse the full object inventory without ranking."
                ),
            },
        ]
    if verbosity == "full":
        return payload
    if verbosity == "minimal":
        return _slim_discover_minimal(payload)
    return payload


_MINIMAL_DISCOVER_RECORD_KEYS = (
    "id",
    "kind",
    "score",
    "default_temporal_role",
    "available",
)


def _slim_discover_minimal(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim each bucket to {id, kind, score, default_temporal_role,
    available, match_reasons[:2]}. Drops verbose strings (description,
    label, name, topics, blocked_reason, recommended_next_actions,
    comparison metadata, starter_query_patch). Internal callers that
    need the full row should request verbosity='compact' (the default)."""

    def _slim(row: dict[str, Any]) -> dict[str, Any]:
        slim = {k: row[k] for k in _MINIMAL_DISCOVER_RECORD_KEYS if k in row}
        reasons = row.get("match_reasons") or []
        if reasons:
            slim["match_reasons"] = list(reasons)[:2]
        return slim

    for bucket in ("measures", "metrics", "segments", "dimensions", "entities", "blocked"):
        rows = payload.get(bucket) or []
        payload[bucket] = [_slim(row) for row in rows if isinstance(row, dict)]
    # `dimension_values` is a different shape — keep its existing slimmer
    # ('id', 'dimension', 'values' sample). Leave it alone for now;
    # there's no equivalent verbosity gate downstream.
    return payload


@runtime_request_scope
def inspect_payload(
    runtime: Runtime,
    *,
    object_id: str,
    partial_query: dict[str, Any] | None = None,
    verbosity: str = "compact",
) -> dict[str, Any]:
    partial_query = dict(partial_query or {})
    return {
        "object_id": object_id,
        "verbosity": verbosity,
        "query_state": _query_state(partial_query),
        "card": _object_card(runtime, object_id, partial_query),
    }


def _valid_next_base(runtime: Runtime, partial_query: dict[str, Any]) -> dict[str, Any]:
    config = runtime.config
    policy_context = _policy_context(partial_query)
    hidden_ids = hidden_object_ids(
        config,
        environment=str(policy_context.get("environment", "")),
        audience=str(policy_context.get("audience", "")),
        roles=policy_context.get("roles", []),
    )
    selection = _selection_context(config, partial_query)
    root_entity = selection["root_entity"]

    valid_dimensions: list[dict[str, Any]] = []
    disabled_dimensions: list[dict[str, Any]] = []
    for dim in config.dimensions:
        if dim.id in hidden_ids:
            continue
        availability = _path_availability(config, root_entity, dim.entity)
        row = {**asdict(dim), **availability}
        (valid_dimensions if availability["available"] else disabled_dimensions).append(row)

    valid_temporal_roles: list[dict[str, Any]] = []
    disabled_temporal_roles: list[dict[str, Any]] = []
    for role in config.temporal_roles:
        if role.id in hidden_ids:
            continue
        dim = next((d for d in config.dimensions if d.id == role.dimension), None)
        if dim is None:
            row = {
                **asdict(role),
                "available": False,
                "reason": "temporal role does not have a queryable backing dimension",
                "path": [],
                "candidates": [],
            }
            disabled_temporal_roles.append(row)
            continue
        availability = _path_availability(config, root_entity, dim.entity)
        row = {**asdict(role), **availability}
        (valid_temporal_roles if availability["available"] else disabled_temporal_roles).append(row)

    valid_measures: list[dict[str, Any]] = []
    disabled_measures: list[dict[str, Any]] = []
    for measure in config.measures:
        if measure.id in hidden_ids:
            continue
        availability = _path_availability(config, root_entity, measure.entity)
        row = {**asdict(measure), **availability}
        (valid_measures if availability["available"] else disabled_measures).append(row)

    valid_entities: list[dict[str, Any]] = []
    disabled_entities: list[dict[str, Any]] = []
    for entity in config.entities:
        if entity.id in hidden_ids:
            continue
        availability = _path_availability(config, root_entity, entity.id)
        row = {**asdict(entity), **availability}
        (valid_entities if availability["available"] else disabled_entities).append(row)

    valid_metric_constructs = [
        {"kind": "aggregate", "available": True, "reason": ""},
        {"kind": "arithmetic", "available": True, "reason": ""},
        {
            "kind": "cumulative",
            "available": bool(valid_temporal_roles),
            "reason": "" if valid_temporal_roles else "requires a compatible temporal role",
        },
        {
            "kind": "conversion",
            "available": len(valid_measures) >= 2,
            "reason": ""
            if len(valid_measures) >= 2
            else "requires at least two compatible measures",
        },
        {"kind": "metric_filter", "available": True, "reason": ""},
    ]
    return {
        "root_entity": root_entity,
        "query_state": _query_state(dict(partial_query)),
        "selection": {
            "selected_measure_ids": selection["selected_measure_ids"],
            "selected_metric_recipe_ids": selection["selected_metric_recipe_ids"],
            "selected_dimensions": selection["selected_dimensions"],
            "selected_temporal_role": selection["selected_temporal_role"],
        },
        "valid_dimensions": valid_dimensions,
        "disabled_dimensions": disabled_dimensions,
        "valid_temporal_roles": valid_temporal_roles,
        "disabled_temporal_roles": disabled_temporal_roles,
        "valid_measures": valid_measures,
        "disabled_measures": disabled_measures,
        "valid_entities": valid_entities,
        "disabled_entities": disabled_entities,
        "valid_metric_constructs": valid_metric_constructs,
        "disabled_options": [
            *disabled_dimensions,
            *disabled_temporal_roles,
            *disabled_measures,
            *disabled_entities,
        ],
    }


@runtime_request_scope
def build_options_payload(
    runtime: Runtime,
    *,
    partial_query: dict[str, Any],
    focus_terms: str = "",
    focus_object_id: str = "",
    step: str = "",
    stage: str = "",
    verbosity: str = "compact",
    include_blocked: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    base = _valid_next_base(runtime, partial_query)
    config = runtime.config
    maps = _config_maps(config)
    raw_query = dict(partial_query or {})

    def _infer_builder_step() -> str:
        if step:
            return step
        if not list(raw_query.get("select", []) or []):
            return "measure"
        if not list(raw_query.get("group_by", []) or []):
            return "group_by"
        if not list(raw_query.get("where", []) or []):
            return "filter_dimension"
        if not dict(raw_query.get("time", {}) or {}):
            return "time"
        return "review"

    def _next_legal_steps() -> list[str]:
        """Ordered list of builder steps still unsatisfied by the current
        partial query. Mirrors `_infer_builder_step`'s precondition checks
        so an agent can see the full remaining path, not just the next
        single step. ``review`` is always the tail."""
        steps: list[str] = []
        if not list(raw_query.get("select", []) or []):
            steps.append("measure")
        if not list(raw_query.get("group_by", []) or []):
            steps.append("group_by")
        if not list(raw_query.get("where", []) or []):
            steps.append("filter_dimension")
        if not dict(raw_query.get("time", {}) or {}):
            steps.append("time")
        steps.append("review")
        return steps

    builder_step = _infer_builder_step()
    stage = _infer_stage(partial_query, stage or builder_step, focus_terms)
    shortlist_limit = min(limit, 5 if verbosity != "full" else limit)

    focus_card = _object_card(runtime, focus_object_id, partial_query) if focus_object_id else {}
    if not focus_card and focus_terms:
        discovery = discover_payload(
            runtime,
            terms=focus_terms,
            partial_query=partial_query,
            stage=stage,
            verbosity=verbosity,
            limit=1,
        )
        for bucket in ("measures", "metrics", "dimensions", "dimension_values", "entities"):
            if discovery.get(bucket):
                focus_card = inspect_payload(
                    runtime,
                    object_id=str(discovery[bucket][0]["id"]),
                    partial_query=partial_query,
                    verbosity=verbosity,
                )["card"]
                break
    if not focus_card and base["selection"]["selected_measure_ids"]:
        focus_card = inspect_payload(
            runtime,
            object_id=str(base["selection"]["selected_measure_ids"][0]),
            partial_query=partial_query,
            verbosity=verbosity,
        )["card"]

    focus_measure = maps["measures"].get(str(focus_card.get("id", "")))
    if focus_measure is None and focus_card.get("kind") == "metric":
        related_measures = list(focus_card.get("related_measures", []) or [])
        if related_measures:
            focus_measure = maps["measures"].get(str(related_measures[0]))

    query_patches: list[dict[str, Any]] = []

    def _option_row(
        card: dict[str, Any],
        *,
        rank: float,
        rationale: list[str],
        blocked_reason: str = "",
        query_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = _compact_summary_from_card(
            card, available=not blocked_reason, blocked_reason=blocked_reason
        )
        row.update({"rank": round(rank, 3), "rationale": list(dict.fromkeys(rationale))})
        if query_patch is not None:
            row["query_patch"] = query_patch
        return row

    recommended: list[dict[str, Any]] = []
    available: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    if builder_step == "measure":
        discovery = discover_payload(
            runtime,
            terms=focus_terms,
            partial_query=partial_query,
            stage="initial",
            verbosity=verbosity,
            limit=limit,
        )
        measure_rows = [*discovery["measures"], *discovery["metrics"]]
        # Empty-query, empty-terms case used to give every candidate
        # the same flat rank (the per-kind stage bonus) with rationale
        # "default ordering" — the rank field carried no signal. When
        # there is no scoring context, rank by `review_priority` and
        # use the first topic as `rationale` so the field at least
        # differentiates candidates.
        no_scoring_context = not str(focus_terms or "").strip() and not raw_query
        for row in measure_rows:
            card = _object_card(runtime, row["id"], partial_query)
            if no_scoring_context:
                review_priority = (
                    str(card.get("review_priority") or row.get("review_priority") or "")
                    .strip()
                    .lower()
                )
                # Base from review_priority — but most packages have
                # uniform priorities, so rank would collapse without a
                # second axis. Use description length as a "curation
                # depth" proxy (the authoring fields it previously
                # depended on — search_terms / examples — were removed).
                # Capped at 8 so it's a tiebreak, not a domination —
                # review_priority is still the primary driver.
                priority_ranks = {"critical": 32.0, "high": 30.0, "medium": 20.0, "low": 10.0}
                priority_base = priority_ranks.get(review_priority, 15.0)
                description_len = len(str(card.get("description") or row.get("description") or ""))
                curation_signal = min(8.0, 0.04 * description_len)
                row_rank = round(priority_base + curation_signal, 3)
                topics = list(card.get("topics") or row.get("topics") or [])
                rationale = [f"topic: {topics[0]}"] if topics else ["governed measure"]
                if review_priority:
                    rationale.append(f"review priority {review_priority}")
                if curation_signal > 0:
                    rationale.append(f"curation depth +{curation_signal:.1f}")
            else:
                row_rank = float(row["score"])
                rationale = list(row["match_reasons"])
            option = _option_row(
                card,
                rank=row_rank,
                rationale=rationale,
                blocked_reason=str(row.get("blocked_reason", "") or ""),
                query_patch=_query_patch_with_selection(
                    runtime,
                    partial_query,
                    {"id": row["id"], "kind": row["kind"], "label": row["label"]},
                ),
            )
            if not option["blocked_reason"] and len(recommended) < shortlist_limit:
                recommended.append(option)
            elif not option["blocked_reason"]:
                available.append(option)
            elif include_blocked:
                blocked.append(option)

    elif builder_step == "aggregation" and focus_measure is not None:
        guidance = _aggregation_guidance(focus_measure)
        suggested_rank = {agg: idx for idx, agg in enumerate(guidance["suggested_aggregations"])}
        for agg in guidance["allowed_aggregations"]:
            patch = dict(raw_query or {})
            patch.setdefault("version", 1)
            patch["select"] = [
                {
                    "expression": {"measure": focus_measure.id, "aggregation": agg},
                    "as": focus_measure.label,
                }
            ]
            rank = 20.0 - (float(suggested_rank[agg]) / 10.0) if agg in suggested_rank else 10.0
            row = {
                "id": agg,
                "object_type": "aggregation",
                "name": agg,
                "label": agg,
                "description": "suggested aggregation"
                if agg in guidance["suggested_aggregations"]
                else "allowed aggregation",
                "root_entity": focus_measure.entity,
                "default_temporal_role": focus_measure.compatible_temporal_roles[0]
                if focus_measure.compatible_temporal_roles
                else "",
                "available": True,
                "blocked_reason": "",
                "rank": rank,
                "rationale": ["suggested for selected measure"]
                if agg in guidance["suggested_aggregations"]
                else ["allowed for selected measure"],
                "query_patch": patch,
            }
            (recommended if agg in guidance["suggested_aggregations"] else available).append(row)
        if include_blocked:
            for row in guidance["discouraged_aggregations"]:
                blocked.append(
                    {
                        "id": row["aggregation"],
                        "object_type": "aggregation",
                        "name": row["aggregation"],
                        "label": row["aggregation"],
                        "description": row["reason"],
                        "root_entity": focus_measure.entity,
                        "default_temporal_role": focus_measure.compatible_temporal_roles[0]
                        if focus_measure.compatible_temporal_roles
                        else "",
                        "available": False,
                        "blocked_reason": row["reason"],
                        "rank": 0.0,
                        "rationale": [row["reason"]],
                    }
                )

    elif builder_step in {"group_by", "filter_dimension"}:
        for row in base["valid_dimensions"]:
            dim = maps["dimensions"][str(row["id"])]
            card = _object_card(runtime, str(row["id"]), partial_query)
            score, reasons = _dimension_builder_score(
                dim,
                card,
                focus_terms=focus_terms,
                focus_measure=focus_measure,
                stage=stage,
            )
            patch = (
                _query_patch_with_group_by(partial_query, str(row["id"]))
                if builder_step == "group_by"
                else dict(raw_query or {})
            )
            if builder_step == "filter_dimension":
                patch.setdefault("version", 1)
            option = _option_row(card, rank=score, rationale=reasons, query_patch=patch)
            if any(
                marker in reasons
                for marker in (
                    "recommended for selected measure",
                    "human-readable breakdown",
                    "business grouping",
                    "display label",
                )
            ):
                recommended.append(option)
            else:
                available.append(option)
        if include_blocked:
            for row in base["disabled_dimensions"]:
                card = _object_card(runtime, str(row["id"]), partial_query)
                blocked.append(
                    _option_row(
                        card,
                        rank=0.0,
                        rationale=[str(row.get("reason", ""))],
                        blocked_reason=str(row.get("reason", "")),
                    )
                )

    elif builder_step == "filter_value":
        target_dimension = (
            str(focus_card.get("id", ""))
            if focus_card.get("kind") == "dimension"
            else (
                base["selection"]["selected_dimensions"][0]
                if base["selection"]["selected_dimensions"]
                else ""
            )
        )
        if not target_dimension and focus_terms:
            value_discovery = discover_payload(
                runtime,
                terms=focus_terms,
                partial_query=partial_query,
                kinds=["dimension"],
                stage="post_dimension",
                limit=1,
            )
            if value_discovery["dimensions"]:
                target_dimension = str(value_discovery["dimensions"][0]["id"])
        if target_dimension:
            values = valid_values_payload(
                runtime,
                dimension_id=target_dimension,
                query=partial_query or None,
                search=focus_terms,
                limit=limit,
                allow_live_query=False,
            )
            dim_card = _object_card(runtime, target_dimension, partial_query)
            for value_row in values["values"]:
                patch = dict(raw_query or {})
                patch.setdefault("version", 1)
                patch.setdefault("where", [])
                patch["where"] = [
                    *list(patch.get("where", []) or []),
                    {"dimension": target_dimension, "op": "=", "value": value_row["value"]},
                ]
                option = {
                    "id": f"{target_dimension}={value_row['value']}",
                    "object_type": "dimension_value",
                    "name": str(value_row["value"]),
                    "label": str(value_row.get("label", value_row["value"])),
                    "description": f"filter {dim_card.get('label', target_dimension)} to {value_row.get('label', value_row['value'])}",
                    "root_entity": dim_card.get("root_entity", ""),
                    "default_temporal_role": "",
                    "available": True,
                    "blocked_reason": "",
                    "rank": 10.0,
                    "rationale": ["valid value for selected dimension"],
                    "dimension_id": target_dimension,
                    "value": value_row["value"],
                    "query_patch": patch,
                }
                (recommended if len(recommended) < shortlist_limit else available).append(option)

    elif builder_step == "time":
        preferred_grain = str(
            dict(partial_query.get("time", {}) or {}).get("grain", "")
            or _infer_time_grain_from_text(focus_terms, default="month")
        )
        compatible_role_ids = list(
            dict.fromkeys(list(focus_card.get("compatible_temporal_roles", []) or []))
        )
        default_role_id = str(focus_card.get("default_temporal_role", "") or "")
        time_roles = list(base["valid_temporal_roles"])
        if compatible_role_ids:
            time_roles = [role for role in time_roles if str(role["id"]) in compatible_role_ids]
        if not time_roles:
            time_roles = list(base["valid_temporal_roles"])
        for role in time_roles:
            supported_grains = list(role.get("supported_grains", []) or [])
            patch_grain = (
                preferred_grain
                if preferred_grain in supported_grains
                else (
                    "month"
                    if "month" in supported_grains
                    else str((supported_grains or ["month"])[0])
                )
            )
            patch = _query_patch_with_time(partial_query, str(role["id"]), patch_grain)
            is_default = str(role["id"]) == default_role_id or (
                not default_role_id and role.get("default_query_time_axis")
            )
            option = {
                "id": role["id"],
                "object_type": "temporal_role",
                "name": role.get("name", role["id"]),
                "label": role.get("label", role["id"]),
                "description": "default time axis" if is_default else "compatible time axis",
                "root_entity": base["root_entity"],
                "default_temporal_role": role["id"],
                "available": True,
                "blocked_reason": "",
                "rank": 18.0 if is_default else 10.0,
                "rationale": ["default time axis"] if is_default else ["compatible temporal role"],
                "supported_grains": supported_grains,
                "query_patch": patch,
            }
            (
                recommended if is_default and len(recommended) < shortlist_limit else available
            ).append(option)
        if include_blocked:
            for role in base["disabled_temporal_roles"]:
                blocked.append(
                    {
                        "id": role["id"],
                        "object_type": "temporal_role",
                        "name": role.get("name", role["id"]),
                        "label": role.get("label", role["id"]),
                        "description": str(role.get("reason", "")),
                        "root_entity": base["root_entity"],
                        "default_temporal_role": "",
                        "available": False,
                        "blocked_reason": str(role.get("reason", "")),
                        "rank": 0.0,
                        "rationale": [str(role.get("reason", ""))],
                    }
                )

    else:
        base_patch = dict(raw_query or {})
        base_patch.setdefault("version", 1)
        query_patches = [base_patch]
        recommended.append(
            {
                "id": "review_current_query",
                "object_type": "query_patch",
                "name": "review_current_query",
                "label": "Review current query",
                "description": "validate or execute the current draft",
                "root_entity": base["root_entity"],
                "default_temporal_role": base["selection"]["selected_temporal_role"],
                "available": True,
                "blocked_reason": "",
                "rank": 10.0,
                "rationale": ["current query is ready for review"],
                "query_patch": base_patch,
            }
        )

    recommended = sorted(
        recommended,
        key=lambda row: (-float(row.get("rank", 0.0)), row.get("label", ""), row.get("id", "")),
    )[:shortlist_limit]
    available = sorted(
        available,
        key=lambda row: (-float(row.get("rank", 0.0)), row.get("label", ""), row.get("id", "")),
    )[:shortlist_limit]
    blocked = sorted(blocked, key=lambda row: (row.get("label", ""), row.get("id", "")))[
        :shortlist_limit
    ]
    policy_context = _policy_context(partial_query)
    for row in [*recommended, *available, *blocked]:
        row["why_recommended"] = str(
            (row.get("rationale") or [row.get("blocked_reason", "")])[0] or ""
        )
        row["result_shape_preview"] = (
            _result_shape_preview(dict(row.get("query_patch", {}) or {}))
            if row.get("query_patch")
            else {}
        )
        row["policy_effects"] = (
            policy_effects_for_object(
                config,
                str(row.get("id", "")),
                environment=str(policy_context.get("environment", "")),
                audience=str(policy_context.get("audience", "")),
                roles=policy_context.get("roles", []),
            )
            if str(row.get("id", "")).startswith(
                ("measure.", "metric.", "dimension.", "entity.", "segment.")
            )
            else []
        )
    for row in [*recommended, *available]:
        if row.get("query_patch") and row["query_patch"] not in query_patches:
            query_patches.append(row["query_patch"])

    return {
        "root_entity": base["root_entity"],
        "builder_step": builder_step,
        "next_legal_steps": _next_legal_steps(),
        "stage": stage,
        "verbosity": verbosity,
        "query_state": base["query_state"],
        "selection": base["selection"],
        "focus_object_id": focus_card.get("id", ""),
        "focus_terms": focus_terms,
        "recommended": recommended,
        "available": available,
        "blocked": blocked if include_blocked else [],
        "query_patches": query_patches[:shortlist_limit],
    }


def _select_expr_for_choice(runtime: Runtime, chosen: dict[str, Any]) -> dict[str, Any]:
    if chosen["kind"] == "metric":
        return {"expression": {"metric": chosen["id"]}, "as": chosen["label"]}
    measure = _config_maps(runtime.config)["measures"][chosen["id"]]
    return {
        "expression": {"measure": chosen["id"], "aggregation": measure.default_aggregation},
        "as": chosen["label"],
    }


def _query_patch_with_selection(
    runtime: Runtime, partial_query: dict[str, Any], chosen: dict[str, Any]
) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    query["select"] = [_select_expr_for_choice(runtime, chosen)]
    return query


def _query_patch_with_group_by(partial_query: dict[str, Any], dimension_id: str) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    query["group_by"] = list(dict.fromkeys([*list(query.get("group_by", []) or []), dimension_id]))
    return query


def _query_patch_with_where(
    partial_query: dict[str, Any], dimension_id: str, value: Any, op: str = "="
) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    query["where"] = [
        *list(query.get("where", []) or []),
        {"field": dimension_id, "op": op, "value": value},
    ]
    return query


def _query_patch_with_order_by_time(partial_query: dict[str, Any]) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    query["order_by"] = [
        *list(query.get("order_by", []) or []),
        {"field": "time", "direction": "DESC"},
    ]
    return query


def _query_patch_with_metric_filter(
    partial_query: dict[str, Any], metric_id: str, op: str = ">", value: Any = 0
) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    # Use the MetricRefExpr shape (`{kind: "metric", metric: "..."}`) +
    # top-level op/value on the filter. This validates against
    # `MetricFilterExpression` cleanly without requiring an `entity` or
    # `input` field — the simpler form is the right scaffold for an
    # agent who just wants to filter on a governed metric's value.
    # Agents needing a more complex shape (metric_predicate with entity
    # scope, contextual time grain, etc.) can read
    # capabilities.expression_shapes for the full union.
    query["metric_filters"] = [
        *list(query.get("metric_filters", []) or []),
        {
            "expression": {"kind": "metric", "metric": metric_id},
            "op": op,
            "value": value,
        },
    ]
    return query


def _query_patch_with_time(
    partial_query: dict[str, Any], temporal_role: str, grain: str = ""
) -> dict[str, Any]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    query["time"] = {"temporal_role": temporal_role, "grain": grain or "month"}
    return query


def _first_day_of_next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _time_bounds_from_text(text: str) -> dict[str, Any]:
    lowered = str(text or "").lower()
    relative_match = re.search(r"\blast\s+(\d+)\s+(day|week|month|quarter|year)s?\b", lowered)
    if relative_match:
        return {
            "range": {
                "last": {
                    "unit": relative_match.group(2),
                    "value": int(relative_match.group(1)),
                }
            }
        }
    if re.search(r"\blast\s+(day|week|month|quarter|year)\b", lowered):
        unit = re.search(r"\blast\s+(day|week|month|quarter|year)\b", lowered)
        return {"range": {"last": {"unit": unit.group(1), "value": 1}}} if unit else {}
    q_match = re.search(r"\bq([1-4])\s*['-]?\s*(20\d{2})\b", lowered)
    if q_match:
        quarter = int(q_match.group(1))
        year = int(q_match.group(2))
        start_month = ((quarter - 1) * 3) + 1
        end_year = year + (1 if quarter == 4 else 0)
        end_month = 1 if quarter == 4 else start_month + 3
        return {
            "start": f"{year:04d}-{start_month:02d}-01",
            "end": f"{end_year:04d}-{end_month:02d}-01",
        }
    if re.search(r"\b(?:current|this)\s+month\b", lowered):
        today = date.today()
        start = today.replace(day=1)
        end = _first_day_of_next_month(today)
        return {"start": start.isoformat(), "end": end.isoformat()}
    return {}


def _infer_time_grain_from_text(text: str, default: str = "month") -> str:
    lowered = str(text or "").lower()
    if re.search(r"\bend[- ]of[- ]month\b", lowered):
        return "month"
    if re.search(r"\bq[1-4]\s*['-]?\s*20\d{2}\b", lowered):
        return "quarter"
    adjective_grains = {
        "daily": "day",
        "weekly": "week",
        "monthly": "month",
        "quarterly": "quarter",
        "yearly": "year",
    }
    for marker, candidate_grain in adjective_grains.items():
        if marker in lowered:
            return candidate_grain
    for candidate_grain in ("day", "week", "month", "quarter", "year"):
        if any(
            phrase in lowered
            for phrase in (
                f"by {candidate_grain}",
                f"per {candidate_grain}",
                f"each {candidate_grain}",
                f"{candidate_grain} over time",
            )
        ):
            return candidate_grain
    return default


def _starter_query_patches(
    runtime: Runtime,
    object_id: str,
    partial_query: dict[str, Any] | None = None,
    *,
    card: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    query = dict(partial_query or {})
    query.setdefault("version", 1)
    card = dict(card or {})
    if not card:
        card = _object_card(runtime, object_id, partial_query)
    kind = str(card.get("kind", ""))
    patches: list[dict[str, Any]] = []
    if kind in {"measure", "metric"}:
        select_query = _query_patch_with_selection(
            runtime,
            query,
            {"id": object_id, "kind": kind, "label": card.get("label", "")},
        )
        patches.append({"kind": "select", "query_patch": select_query})
        recommended_dimensions = list(card.get("recommended_dimensions", []) or [])
        if recommended_dimensions:
            patches.append(
                {
                    "kind": "group_by",
                    "query_patch": _query_patch_with_group_by(
                        select_query, recommended_dimensions[0]
                    ),
                }
            )
        default_role = str(card.get("default_temporal_role", "") or "")
        if default_role:
            starter_text = " ".join(
                [
                    str(card.get("label", "") or ""),
                    str(card.get("description", "") or ""),
                    *[str(item) for item in list(card.get("sample_questions", []) or [])[:2]],
                ]
            )
            time_query = _query_patch_with_time(
                select_query,
                default_role,
                _infer_time_grain_from_text(starter_text, default="month"),
            )
            patches.append({"kind": "time", "query_patch": time_query})
            # `order_by.field = "time"` is a stable shortcut whenever the
            # query has a time axis — surface it as a starter so blind
            # agents don't have to guess the shape.
            patches.append(
                {
                    "kind": "order_by",
                    "query_patch": _query_patch_with_order_by_time(time_query),
                }
            )
        # Surface a `where` scaffold using a declared value-domain value
        # on the first non-ID dimension that has one. Skip live warehouse
        # probes — keep `inspect` synchronous. Search order:
        #   1. card.recommended_filters[] (curated, ranked by author)
        #   2. card.recommended_dimensions[] (curated companion dims)
        #   3. any config dimension reachable from the measure/metric
        #      entity (broader fallback so revenue_usd-style cards with
        #      only ID recommended_dimensions still get a scaffold)
        # ID-typed dimensions are skipped at every stage — they don't
        # have value_domains in practice and produce misleading scaffolds.
        config = runtime.config
        candidate_dim_ids: list[str] = []
        for entry in list(card.get("recommended_filters", []) or []):
            dim_id = str(
                (entry.get("dimension_id") if isinstance(entry, dict) else "") or ""
            ).strip()
            if dim_id:
                candidate_dim_ids.append(dim_id)
        for dim_id in list(card.get("recommended_dimensions", []) or []):
            candidate_dim_ids.append(str(dim_id))
        for dim in config.dimensions:
            candidate_dim_ids.append(dim.id)
        seen_dim_ids: set[str] = set()
        chosen_dim_id = ""
        chosen_sample_value: Any = None
        dim_index = {dim.id: dim for dim in config.dimensions}
        for dim_id in candidate_dim_ids:
            if dim_id in seen_dim_ids:
                continue
            seen_dim_ids.add(dim_id)
            dim_obj = dim_index.get(dim_id)
            if dim_obj is None:
                continue
            if (
                str(getattr(dim_obj, "semantic_kind", "") or "").lower() == "id"
                or str(getattr(dim_obj, "data_type", "") or "").lower() == "id"
            ):
                continue
            domain = _value_domain_for_dimension(config, dim_id)
            if domain is None:
                continue
            sample_rows, _, _ = _declared_value_rows(domain, limit=1)
            if not sample_rows:
                continue
            sample_value = sample_rows[0].get("value")
            if sample_value is None:
                continue
            chosen_dim_id = dim_id
            chosen_sample_value = sample_value
            break
        if chosen_dim_id:
            patches.append(
                {
                    "kind": "where",
                    "query_patch": _query_patch_with_where(
                        select_query, chosen_dim_id, chosen_sample_value
                    ),
                    "note": "scaffold — replace value with the filter target before executing",
                }
            )
        # For metric cards, scaffold a `metric_filters` clause so agents
        # see the governed-filter shape regardless of whether the metric
        # itself is predicate-backed. Predicate-backed metrics get this
        # scaffold for free; for non-predicate metrics, the scaffold is
        # how an agent threads "filter on this metric's value" into a
        # query whose select is different. The op/value are placeholders.
        if kind == "metric":
            patches.append(
                {
                    "kind": "metric_filters",
                    "query_patch": _query_patch_with_metric_filter(select_query, object_id),
                    "note": "scaffold — set op/value before executing",
                }
            )
    elif kind == "dimension":
        patches.append(
            {"kind": "group_by", "query_patch": _query_patch_with_group_by(query, object_id)}
        )
    return patches


def _requested_grouping_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    top_by_match = re.search(
        r"^(?:top|highest|lowest)\s+([a-z0-9 _-]+?)\s+by\s+([a-z0-9 _-]+)$", lowered
    )
    if top_by_match:
        raw_terms = top_by_match.group(1).strip()
    else:
        by_match = re.search(
            r"\bby ([a-z0-9 _-]+?)(?:\s+(?:for|from|in|with|during|over|last|this|current|next|prior)\b|$)",
            lowered,
        )
        raw_terms = by_match.group(1).strip() if by_match else ""
    if not raw_terms:
        return []
    return [term.strip() for term in re.split(r"\s*(?:,| and | & )\s*", raw_terms) if term.strip()]


def _is_temporal_grouping_term(term: str) -> bool:
    if term in {"day", "week", "month", "quarter", "year", "delivered month", "ordered month"}:
        return True
    temporal_tokens = set(_tokenize(term))
    return bool(
        temporal_tokens
        and temporal_tokens.issubset(
            {
                "day",
                "week",
                "month",
                "quarter",
                "year",
                "time",
                "times",
                "delivered",
                "ordered",
                "prepared",
                "versus",
                "vs",
            }
        )
    )


def _term_matches_value_domain(config: PackageConfig, term: str) -> bool:
    term_tokens = set(_tokenize(term))
    if not term_tokens:
        return False
    for domain in config.value_domains:
        for row in list(domain.values or []):
            values = [
                str(row.value),
                str(row.label),
                *[str(alias) for alias in list(row.aliases or [])],
            ]
            for value in values:
                value_tokens = set(_tokenize(value))
                if value_tokens and value_tokens.issubset(term_tokens):
                    return True
    return False


def _choose_group_dimensions(
    runtime: Runtime, query: dict[str, Any], text: str, chosen_group_dim: str = ""
) -> list[str]:
    keyword_dimensions = [
        ("customer type", "dimension.jaffle_customer_type"),
        ("customer segment", "dimension.jaffle_customer_history_segment"),
        ("segment", "dimension.jaffle_customer_history_segment"),
        ("store", "dimension.jaffle_store_name"),
        ("stores", "dimension.jaffle_store_name"),
        ("product type", "dimension.jaffle_item_product_type"),
    ]
    if chosen_group_dim:
        return [chosen_group_dim]
    selection = _selection_context(runtime.config, query)
    group_dims: list[str] = []
    covered_terms: set[str] = set()
    for phrase, dim_id in keyword_dimensions:
        if phrase in text and dim_id in _config_maps(runtime.config)["dimensions"]:
            availability = _availability_for_object(
                runtime.config, selection["root_entity"], dim_id, "dimension"
            )
            if availability["available"]:
                group_dims.append(dim_id)
                covered_terms.update(_tokenize(phrase))
    for dimension_terms in _requested_grouping_terms(text):
        if _is_temporal_grouping_term(dimension_terms) or _term_matches_value_domain(
            runtime.config, dimension_terms
        ):
            continue
        dimension_tokens = set(_tokenize(dimension_terms))
        if dimension_tokens and dimension_tokens.issubset(covered_terms):
            continue
        dim_discovery = discover_payload(
            runtime,
            terms=dimension_terms,
            partial_query=query,
            kinds=["dimension"],
            stage="post_measure",
            limit=5,
        )
        matched_rows = [
            row
            for row in dim_discovery["dimensions"]
            if any(
                "label/name matched" in reason
                or "search term matched" in reason
                or "exact name match" in reason
                for reason in row["match_reasons"]
            )
        ]
        # Prefer reachable dimensions; only fall back to an unreachable
        # match (which will be caught at validation) when no reachable
        # match exists.
        chosen: str = ""
        for row in matched_rows:
            availability = _availability_for_object(
                runtime.config, selection["root_entity"], str(row["id"]), "dimension"
            )
            if availability["available"]:
                chosen = str(row["id"])
                break
        if not chosen and matched_rows:
            chosen = str(matched_rows[0]["id"])
        if chosen:
            group_dims.append(chosen)
    return list(dict.fromkeys(group_dims))


def _choose_group_dimension(
    runtime: Runtime, query: dict[str, Any], text: str, chosen_group_dim: str = ""
) -> str:
    dims = _choose_group_dimensions(runtime, query, text, chosen_group_dim)
    return dims[0] if dims else ""


def _apply_time_from_text(
    runtime: Runtime, query: dict[str, Any], text: str, chosen_ids: list[str]
) -> dict[str, Any]:
    time_bounds = _time_bounds_from_text(text)
    wants_time = any(
        phrase in text
        for phrase in (
            "over time",
            "historical",
            "history",
            "trend",
            "trending",
            "end-of-month",
            "end of month",
            "by day",
            "by week",
            "by month",
            "by quarter",
            "by year",
            "daily ",
            "weekly ",
            "monthly ",
            "quarterly ",
            "yearly ",
            " each day",
            " each week",
            " each month",
            " each quarter",
            " each year",
        )
    ) or bool(time_bounds)
    if not wants_time:
        return query
    role = ""
    for object_id in chosen_ids:
        card = _object_card(runtime, object_id)
        role = str(card.get("default_temporal_role", "") or "")
        if role:
            break
    if not role:
        return query
    grain = _infer_time_grain_from_text(text, default="month")
    query["time"] = {"temporal_role": role, "grain": grain, **time_bounds}
    return query
