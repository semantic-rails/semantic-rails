"""Package-authored segments — derive Query IR from a segment definition.

Exposes :func:`normalize_segment` and :func:`build_segment_query`. A
segment is a named, governed cohort (declared in YAML) that compiles
to a Query IR the runtime can validate / explain / preview. Backs the
``segment-validate``, ``segment-explain``, and ``segment-preview``
surfaces on HTTP and MCP.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

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
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RollingExpr,
    SemanticExpr,
)
from .schema import PackageConfig, SegmentConfig

SEGMENT_PREVIEW_ALIAS = "__segment_basis"


@dataclass(frozen=True)
class NormalizedSegment:
    id: str
    name: str
    label: str
    description: str
    entity: str
    basis_metric: str
    member_key_dimensions: list[str] = field(default_factory=list)
    preview_dimensions: list[str] = field(default_factory=list)
    where: list[dict[str, Any]] = field(default_factory=list)
    metric_filters: list[dict[str, Any]] = field(default_factory=list)
    time: dict[str, Any] = field(default_factory=dict)
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)
    path_policy: dict[str, Any] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _config_maps(config: PackageConfig) -> dict[str, dict[str, Any]]:
    return {
        "entities": {row.id: row for row in config.entities},
        "dimensions": {row.id: row for row in config.dimensions},
        "measures": {row.id: row for row in config.measures},
        "metric_recipes": {row.id: row for row in config.metric_recipes},
        "segments": {row.id: row for row in config.segments},
    }


def _metric_root_entity(config: PackageConfig, expr: SemanticExpr) -> str:
    maps = _config_maps(config)
    measures = maps["measures"]
    recipes = maps["metric_recipes"]
    if isinstance(expr, (MeasureRefExpr, AggregateExpr)):
        if expr.measure not in measures:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{expr.measure}'")
        return measures[expr.measure].entity
    if isinstance(expr, MetricRecipeRefExpr):
        if expr.metric_recipe not in recipes:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown metric '{expr.metric_recipe}'")
        return _metric_root_entity(config, recipes[expr.metric_recipe].expression)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _metric_root_entity(config, expr.left)
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            entity = _metric_root_entity(config, arg)
            if entity:
                return entity
        return ""
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            entity = _metric_root_entity(config, arg)
            if entity:
                return entity
        return ""
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            entity = _metric_root_entity(config, item.when)
            if entity:
                return entity
            entity = _metric_root_entity(config, item.then)
            if entity:
                return entity
        return _metric_root_entity(config, expr.else_expr) if expr.else_expr is not None else ""
    if isinstance(expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return _metric_root_entity(config, expr.input)
    if isinstance(expr, MetricPredicateExpr):
        return expr.entity
    if isinstance(expr, ConversionExpr):
        return expr.entity or _metric_root_entity(config, expr.base)
    if isinstance(expr, LiteralExpr):
        return ""
    return ""


def get_segment(config: PackageConfig, segment_id: str) -> SegmentConfig:
    maps = _config_maps(config)
    if segment_id not in maps["segments"]:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown segment '{segment_id}'")
    return maps["segments"][segment_id]


def member_key_dimensions(config: PackageConfig, entity_id: str) -> list[str]:
    maps = _config_maps(config)
    entity = maps["entities"].get(entity_id)
    if entity is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown entity '{entity_id}'")
    dims = [
        dim.id
        for key_column in entity.key
        for dim in config.dimensions
        if dim.entity == entity_id and dim.column == key_column
    ]
    if len(dims) != len(entity.key):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Entity '{entity_id}' is missing key dimensions required for segment membership",
            details={
                "entity_id": entity_id,
                "entity_key": list(entity.key),
                "member_key_dimensions": dims,
            },
        )
    return dims


def normalize_segment(config: PackageConfig, segment_id: str) -> NormalizedSegment:
    maps = _config_maps(config)
    segment = get_segment(config, segment_id)
    entity = maps["entities"].get(segment.entity)
    if entity is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown entity '{segment.entity}'")
    if not entity.allowed_as_root:
        raise SemanticLayerError(
            "INVALID_SEGMENT",
            f"Segment '{segment.id}' must target an entity that is allowed as a query root",
            details={"segment_id": segment.id, "entity_id": segment.entity},
        )
    recipe = maps["metric_recipes"].get(segment.basis_metric)
    if recipe is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown basis metric recipe '{segment.basis_metric}'",
            details={"segment_id": segment.id, "basis_metric": segment.basis_metric},
        )
    root_entity = _metric_root_entity(config, recipe.expression)
    if root_entity != segment.entity:
        raise SemanticLayerError(
            "INVALID_SEGMENT",
            f"Segment '{segment.id}' basis metric must resolve to the same root entity as the segment",
            details={
                "segment_id": segment.id,
                "segment_entity": segment.entity,
                "basis_metric": segment.basis_metric,
                "basis_root_entity": root_entity,
            },
        )

    key_dimensions = member_key_dimensions(config, segment.entity)
    preview_dimensions: list[str] = []
    for dimension_id in segment.preview_dimensions:
        dimension = maps["dimensions"].get(dimension_id)
        if dimension is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown preview dimension '{dimension_id}'",
                details={"segment_id": segment.id, "dimension_id": dimension_id},
            )
        if dimension.entity != segment.entity:
            raise SemanticLayerError(
                "INVALID_SEGMENT",
                f"Segment '{segment.id}' preview dimensions must belong to the segment entity",
                details={
                    "segment_id": segment.id,
                    "dimension_id": dimension_id,
                    "dimension_entity": dimension.entity,
                    "segment_entity": segment.entity,
                },
            )
        if not dimension.groupable:
            raise SemanticLayerError(
                "INVALID_SEGMENT",
                f"Segment '{segment.id}' preview dimensions must be groupable",
                details={"segment_id": segment.id, "dimension_id": dimension_id},
            )
        if dimension_id not in key_dimensions and dimension_id not in preview_dimensions:
            preview_dimensions.append(dimension_id)

    return NormalizedSegment(
        id=segment.id,
        name=segment.name,
        label=segment.label,
        description=segment.description,
        entity=segment.entity,
        basis_metric=segment.basis_metric,
        member_key_dimensions=key_dimensions,
        preview_dimensions=preview_dimensions,
        where=list(segment.where),
        metric_filters=list(segment.metric_filters),
        time=dict(segment.time),
        temporal_role_overrides=dict(segment.temporal_role_overrides),
        path_policy=dict(segment.path_policy),
        topics=list(segment.topics),
    )


def build_segment_query(
    normalized: NormalizedSegment,
    *,
    include_preview_dimensions: bool,
    limit: int | None = None,
) -> dict[str, Any]:
    group_by = list(normalized.member_key_dimensions)
    if include_preview_dimensions:
        group_by.extend(
            [
                dimension_id
                for dimension_id in normalized.preview_dimensions
                if dimension_id not in group_by
            ]
        )
    query: dict[str, Any] = {
        "version": 1,
        "select": [
            {"expression": {"metric": normalized.basis_metric}, "as": SEGMENT_PREVIEW_ALIAS}
        ],
        "group_by": group_by,
    }
    if normalized.where:
        query["where"] = list(normalized.where)
    if normalized.metric_filters:
        query["metric_filters"] = list(normalized.metric_filters)
    if normalized.time:
        query["time"] = dict(normalized.time)
    if normalized.temporal_role_overrides:
        query["temporal_role_overrides"] = dict(normalized.temporal_role_overrides)
    if normalized.path_policy:
        query["path_policy"] = dict(normalized.path_policy)
    if limit is not None:
        query["limit"] = int(limit)
    return query


def strip_segment_preview_metric(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if key != SEGMENT_PREVIEW_ALIAS} for row in rows
    ]
