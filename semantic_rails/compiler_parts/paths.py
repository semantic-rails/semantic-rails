from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..ast import NormalizedQuery
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
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
)
from ..ir import BoundMeasure, PathSelection
from ..schema import PackageConfig, RelationshipConfig
from ..sql_ast import SqlBinary, SqlIdentifier, SqlIsNull, SqlJoin, SqlLiteral, SqlTableRef
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
)
from .temporal import _allows_coarse_snapshot_alignment


def _column_ref(table: str, column: str) -> SqlIdentifier:
    return SqlIdentifier(parts=[*str(table).split("."), column])


def _split_column_ref(value: str) -> tuple[str, str]:
    table, _, column = str(value).strip().partition(".")
    return table, column or table


def _resolve_dimension_expr(dim_id: str, config: PackageConfig) -> tuple[SqlIdentifier, str]:
    dim_index = _dimension_index(config)
    dim = dim_index.get(dim_id)
    if dim is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown dimension '{dim_id}'",
            details={"dimension": dim_id},
        )
    table = _entity_index(config)[dim.entity].table
    return _column_ref(table, dim.column), dim.id


def _direct_entity_key_source_expr(
    source_entity: str,
    target_entity: str,
    target_key_col: str,
    config: PackageConfig,
    *,
    source_relation_override: str = "",
) -> SqlIdentifier | None:
    entities = _entity_index(config)
    source = entities.get(source_entity)
    if source is None:
        return None
    source_table = source_relation_override or source.table
    if source_entity == target_entity and target_key_col in set(source.key or [source.primary_key]):
        return _column_ref(source_table, target_key_col)
    for rel in config.relationships:
        if rel.source_entity == source_entity and rel.target_entity == target_entity:
            source_columns = list(rel.source_columns or [rel.source_column])
            target_columns = list(rel.target_columns or [rel.target_column])
            for source_col, target_col in zip(source_columns, target_columns, strict=True):
                if target_col == target_key_col:
                    return _column_ref(source_table, source_col)
        if rel.target_entity == source_entity and rel.source_entity == target_entity:
            source_columns = list(rel.target_columns or [rel.target_column])
            target_columns = list(rel.source_columns or [rel.source_column])
            for source_col, target_col in zip(source_columns, target_columns, strict=True):
                if target_col == target_key_col:
                    return _column_ref(source_table, source_col)
    return None


def _direct_dimension_source_expr(
    source_entity: str,
    dim_id: str,
    config: PackageConfig,
    *,
    source_relation_override: str = "",
) -> tuple[SqlIdentifier, str] | None:
    dim_index = _dimension_index(config)
    dim = dim_index.get(dim_id)
    if dim is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown dimension '{dim_id}'",
            details={"dimension": dim_id},
        )
    target = _entity_index(config).get(dim.entity)
    if target is None or dim.column not in set(target.key or [target.primary_key]):
        return None
    expr = _direct_entity_key_source_expr(
        source_entity,
        dim.entity,
        dim.column,
        config,
        source_relation_override=source_relation_override,
    )
    if expr is None:
        return None
    return expr, dim.id


def _entity_key_dimension_ids(entity_id: str, config: PackageConfig) -> list[str]:
    entity = _entity_index(config).get(entity_id)
    if entity is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown entity '{entity_id}'")
    key_dims: list[str] = []
    for key_col in entity.key:
        dim = next(
            (row for row in config.dimensions if row.entity == entity_id and row.column == key_col),
            None,
        )
        if dim is None:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                f"Entity '{entity_id}' is missing a dimension for key column '{key_col}'",
            )
        key_dims.append(dim.id)
    return key_dims


def _expression_root_entity(expr: SemanticExpr, config: PackageConfig) -> str:
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        measure = _measure_index(config).get(expr.measure)
        if measure is None:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{expr.measure}'")
        return measure.entity
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expression_root_entity(recipe.expression, config)
    if isinstance(
        expr,
        (
            CumulativeExpr,
            RollingExpr,
            PriorPeriodExpr,
            PeriodToDateExpr,
            OffsetWindowExpr,
            MetricPredicateExpr,
        ),
    ):
        return _expression_root_entity(expr.input, config)
    if isinstance(expr, ConversionExpr):
        return _expression_root_entity(expr.base, config)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        left = _expression_root_entity(expr.left, config)
        right = _expression_root_entity(expr.right, config)
        if left == right:
            return left
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "Expression combines incompatible root entities",
            details={"left": left, "right": right},
        )
    if isinstance(expr, RatioExpr):
        left = _expression_root_entity(expr.numerator, config)
        right = _expression_root_entity(expr.denominator, config)
        if left == right:
            return left
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "Ratio expression combines incompatible root entities",
            details={"left": left, "right": right},
        )
    if isinstance(expr, EntityValueExpr):
        return _expression_root_entity(expr.input, config)
    if isinstance(expr, DistributionExpr):
        return _expression_root_entity(expr.over, config)
    if isinstance(expr, BooleanExpr):
        roots = {
            _expression_root_entity(arg, config)
            for arg in expr.args
            if not isinstance(arg, LiteralExpr)
        }
        if len(roots) == 1:
            return next(iter(roots))
        if not roots:
            raise SemanticLayerError(
                "PREDICATE_INPUT_REQUIRED", "Predicate expressions require a metric input"
            )
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "Boolean expression combines incompatible root entities",
            details={"roots": sorted(roots)},
        )
    if isinstance(expr, CallExpr):
        roots = {
            _expression_root_entity(arg, config)
            for arg in expr.args
            if not isinstance(arg, LiteralExpr)
        }
        if len(roots) == 1:
            return next(iter(roots))
        if not roots:
            raise SemanticLayerError(
                "PREDICATE_INPUT_REQUIRED", "Predicate expressions require a metric input"
            )
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "Call expression combines incompatible root entities",
            details={"roots": sorted(roots)},
        )
    raise SemanticLayerError(
        "PREDICATE_NOT_SUPPORTED",
        f"Expression kind '{expr_kind(expr)}' is not supported for predicate planning",
    )


def _leaf_time_role(bound: BoundMeasure, query: NormalizedQuery, config: PackageConfig) -> str:
    if query.time is None:
        return ""
    requested = query.time.temporal_role
    if not requested:
        return bound.temporal_role
    measure = _measure_index(config)[bound.measure_id]
    compatible = set(measure.compatible_temporal_roles)
    if requested in compatible:
        return requested
    if _allows_coarse_snapshot_alignment(
        requested, compatible or {bound.temporal_role}, query, config
    ):
        return bound.temporal_role or _default_temporal_role(measure)
    return bound.temporal_role or _default_temporal_role(measure)


def _time_anchor_expr(
    time_spec: dict[str, Any] | None, config: PackageConfig
) -> SqlIdentifier | None:
    if not time_spec:
        return None
    temporal_roles = _temporal_role_index(config)
    dimensions = _dimension_index(config)
    entities = _entity_index(config)
    role = temporal_roles.get(str(time_spec.get("temporal_role", "")))
    if role is None:
        return None
    dim = dimensions[role.dimension]
    return _column_ref(entities[dim.entity].table, dim.column)


def _join_on_for_relationship(
    rel: RelationshipConfig,
    current_entity: str,
    config: PackageConfig,
    *,
    time_spec: dict[str, Any] | None,
    table_overrides: dict[str, str] | None = None,
) -> tuple[Any, str, str]:
    entities = _entity_index(config)
    overrides = dict(table_overrides or {})

    def _table(entity_id: str) -> str:
        return overrides.get(entity_id, entities[entity_id].table)

    if rel.source_entity == current_entity:
        next_entity = rel.target_entity
        right_table = _table(rel.target_entity)
        left_columns = rel.source_columns or [rel.source_column]
        right_columns = rel.target_columns or [rel.target_column]
        pairs = [
            (table_col, target_col)
            for table_col, target_col in zip(left_columns, right_columns, strict=True)
        ]
        left_table = _table(rel.source_entity)
    else:
        next_entity = rel.source_entity
        right_table = _table(rel.source_entity)
        left_columns = rel.target_columns or [rel.target_column]
        right_columns = rel.source_columns or [rel.source_column]
        pairs = [
            (table_col, target_col)
            for table_col, target_col in zip(left_columns, right_columns, strict=True)
        ]
        left_table = _table(rel.target_entity)
    left_expr = _column_ref(left_table, pairs[0][0])
    right_expr = _column_ref(right_table, pairs[0][1])
    condition: Any = SqlBinary(left_expr, "=", right_expr)
    for left_col, right_col in pairs[1:]:
        condition = SqlBinary(
            condition,
            "AND",
            SqlBinary(_column_ref(left_table, left_col), "=", _column_ref(right_table, right_col)),
        )
    time_anchor = _time_anchor_expr(time_spec, config)
    if time_anchor is not None and rel.temporal_validity:
        valid_from = str(rel.temporal_validity.get("valid_from", "")).strip()
        valid_to = str(rel.temporal_validity.get("valid_to", "")).strip()
        if valid_from:
            table, column = _split_column_ref(valid_from)
            condition = SqlBinary(
                condition, "AND", SqlBinary(_column_ref(table, column), "<=", time_anchor)
            )
        if valid_to:
            table, column = _split_column_ref(valid_to)
            valid_to_expr = _column_ref(table, column)
            condition = SqlBinary(
                condition,
                "AND",
                SqlBinary(
                    SqlBinary(valid_to_expr, ">", time_anchor), "OR", SqlIsNull(valid_to_expr)
                ),
            )
    return condition, right_table, next_entity


def _joins_for_paths(
    source_entity: str,
    path_selections: Iterable[PathSelection],
    config: PackageConfig,
    *,
    time_spec: dict[str, Any] | None = None,
    table_overrides: dict[str, str] | None = None,
) -> list[SqlJoin]:
    entities = _entity_index(config)
    relationships = _relationship_index(config)
    joins: list[SqlJoin] = []
    overrides = dict(table_overrides or {})
    # Each physical table may appear in the FROM clause once, so it can
    # only be reached through one (relationship, direction) per query.
    # Track how each table was first joined; a second path that needs the
    # same table through a *different* relationship would silently reuse
    # the first join's semantics (e.g. ship-to city vs home city), so it
    # must be a structured refusal, not wrong numbers.
    root_table = overrides.get(source_entity, entities[source_entity].table)
    joined_via: dict[str, tuple[str, str]] = {root_table: ("", "root")}
    for selection in path_selections:
        current_entity = source_entity
        nullable_path = False
        for rel_id in selection.chosen_path:
            rel = relationships[rel_id]
            join_on, right_table, next_entity = _join_on_for_relationship(
                rel,
                current_entity,
                config,
                time_spec=time_spec,
                table_overrides=overrides,
            )
            join_key = (rel.id, current_entity)
            existing = joined_via.get(right_table)
            if existing is None:
                joins.append(
                    SqlJoin(
                        join_type="LEFT" if nullable_path or rel.temporal_validity else "INNER",
                        table=SqlTableRef(name=right_table),
                        on=join_on,
                    )
                )
                joined_via[right_table] = join_key
            elif existing != join_key:
                raise SemanticLayerError(
                    "PATH_JOIN_CONFLICT",
                    f"Table '{right_table}' is needed through relationship '{rel.id}' "
                    f"(from '{current_entity}'), but this query already joins it through "
                    + (
                        f"relationship '{existing[0]}' (from '{existing[1]}')"
                        if existing[0]
                        else "the query root"
                    )
                    + ". The two routes have different semantics and one table instance cannot serve both.",
                    details={
                        "table": right_table,
                        "existing_relationship": existing[0],
                        "existing_from_entity": existing[1],
                        "conflicting_relationship": rel.id,
                        "conflicting_from_entity": current_entity,
                        "target_entity": selection.target_entity,
                        "hint": (
                            "Pin one route for every target via path_preferences, or "
                            "model the second role as its own entity over a dedicated "
                            "relation so the planner can join it independently."
                        ),
                    },
                )
            if rel.temporal_validity:
                nullable_path = True
            current_entity = next_entity
    return joins


def _join_condition(
    keys: list[str], left_alias: str, right_alias: str, *, dialect: Any | None = None
) -> Any:
    if not keys:
        return SqlLiteral(True)
    active_dialect = dialect or dialect_for_warehouse("")
    current: Any = active_dialect.null_safe_eq(
        SqlIdentifier(parts=[left_alias, keys[0]]), SqlIdentifier(parts=[right_alias, keys[0]])
    )
    for key in keys[1:]:
        current = SqlBinary(
            current,
            "AND",
            active_dialect.null_safe_eq(
                SqlIdentifier(parts=[left_alias, key]), SqlIdentifier(parts=[right_alias, key])
            ),
        )
    return current
