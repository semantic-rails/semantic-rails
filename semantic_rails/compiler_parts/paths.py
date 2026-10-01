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
from ..fanout import record_route_choice, resolve_path
from ..ir import BoundMeasure, PathSelection
from ..schema import PackageConfig, RelationshipConfig
from ..sql_ast import SqlBinary, SqlIdentifier, SqlIsNull, SqlJoin, SqlLiteral, SqlTableRef
from .dependencies import record_bound_object
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
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


def _pair_orientations(
    source_entity: str, target_entity: str, config: PackageConfig
) -> list[tuple[RelationshipConfig, list[str], list[str]]]:
    """Every relationship between the two entities, in either direction, as
    ``(relationship, source columns, target columns)`` seen from ``source_entity``."""
    found = []
    for rel in config.relationships:
        forward = (
            list(rel.source_columns or [rel.source_column]),
            list(rel.target_columns or [rel.target_column]),
        )
        if rel.source_entity == source_entity and rel.target_entity == target_entity:
            found.append((rel, *forward))
        if rel.target_entity == source_entity and rel.source_entity == target_entity:
            found.append((rel, forward[1], forward[0]))
    return found


def _pair_key_routes(
    source_entity: str,
    target_entity: str,
    target_key_col: str,
    config: PackageConfig,
) -> list[tuple[RelationshipConfig, str]]:
    """Every way the source entity's table reads ``target_key_col`` of the target entity:
    one ``(relationship, source column)`` per distinct source column, in either direction.
    Relationships that agree on the column collapse to the lowest relationship id, so the
    answer never depends on declaration order."""
    by_column: dict[str, RelationshipConfig] = {}
    for rel, source_columns, target_columns in _pair_orientations(
        source_entity, target_entity, config
    ):
        for source_col, target_col in zip(source_columns, target_columns, strict=True):
            if target_col == target_key_col:
                known = by_column.get(source_col)
                if known is None or rel.id < known.id:
                    by_column[source_col] = rel
    return [(rel, column) for column, rel in sorted(by_column.items())]


def _pair_has_several_pairings(
    source_entity: str, target_entity: str, config: PackageConfig
) -> bool:
    """True when the entities are joined on more than one distinct column pairing, whatever
    the target columns are. Relationships that restate one pairing (from either side) count once."""
    pairings = {
        frozenset(zip(source_columns, target_columns, strict=True))
        for _rel, source_columns, target_columns in _pair_orientations(
            source_entity, target_entity, config
        )
    }
    return len(pairings) > 1


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
    # None: not readable here. Several pairings between the entities (role-playing keys, e.g.
    # an origin and a destination airport, even when one role targets a non-key column) mean
    # the source table alone cannot say which role is meant, so callers fall through to path
    # selection, which follows the pin or refuses.
    if _pair_has_several_pairings(source_entity, target_entity, config):
        return None
    routes = _pair_key_routes(source_entity, target_entity, target_key_col, config)
    if len(routes) != 1:
        return None
    rel, source_col = routes[0]
    # The shortcut only stands when the route resolver picks exactly the one direct
    # relationship found: a pin or another route to the target may mean a different row than
    # the source table's own column, and an ambiguous pair falls through to path selection,
    # which refuses. A pin on the reverse pair must name the same relationship.
    try:
        resolved, candidates = resolve_path(config, start=source_entity, target=target_entity)
    except SemanticLayerError:
        return None
    reverse_pin = get_package_analysis(config).path_preferences.get((target_entity, source_entity))
    if resolved != [rel.id] or reverse_pin not in (None, [rel.id]):
        return None
    record_bound_object(rel, config)
    record_route_choice(source_entity, target_entity, candidates)
    return _column_ref(source_table, source_col)


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
        # This dimension was selected by column identity rather than an ID lookup.
        _dimension_index(config).get(dim.id)
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


def _reaches_at_most_one(rel: RelationshipConfig, current_entity: str) -> bool:
    """True when the hop reaches at most one row for each current row (N:1, 1:1)."""
    if ":" not in rel.cardinality:
        return False
    near, far = [part.strip() for part in rel.cardinality.upper().split(":", 1)]
    if current_entity != rel.source_entity:
        near, far = far, near
    return far == "1" and near in ("1", "N")


def _is_lookup_hop(rel: RelationshipConfig, current_entity: str, config: PackageConfig) -> bool:
    """True when the hop reaches at most one row for each current row (N:1, 1:1) and the
    warehouse's outer join reads NULL, not a type default, for an unmatched row (not ClickHouse).
    """
    if not dialect_for_warehouse(config.package.warehouse).outer_lookup_joins:
        return False
    return _reaches_at_most_one(rel, current_entity)


def _joins_for_paths(
    source_entity: str,
    path_selections: Iterable[PathSelection],
    config: PackageConfig,
    *,
    time_spec: dict[str, Any] | None = None,
    table_overrides: dict[str, str] | None = None,
    lookup_selections: frozenset[tuple[str, str]] = frozenset(),
) -> list[SqlJoin]:
    """The joins for ``path_selections``, all INNER unless the path is temporal.

    The one exception is the lookup left join: a selection named in ``lookup_selections``
    (by ``(target_entity, purpose)``) joins its N:1 and 1:1 hops with LEFT, so a row whose
    foreign key is NULL or unmatched stays, with NULL for what the hop looks up. A hop that
    any other selection also walks stays INNER.
    """
    entities = _entity_index(config)
    relationships = _relationship_index(config)
    path_selections = list(path_selections)
    inner_hops: set[tuple[str, str]] = set()
    if lookup_selections:
        for selection in path_selections:
            if (selection.target_entity, selection.purpose) in lookup_selections:
                continue
            current = source_entity
            for rel_id in selection.chosen_path:
                rel = relationships[rel_id]
                inner_hops.add((rel.id, current))
                current = rel.target_entity if current == rel.source_entity else rel.source_entity
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
        record_route_choice(source_entity, selection.target_entity, selection.candidate_paths)
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
                keep_rows = (
                    (selection.target_entity, selection.purpose) in lookup_selections
                    and join_key not in inner_hops
                    and _is_lookup_hop(rel, current_entity, config)
                )
                joins.append(
                    SqlJoin(
                        join_type="LEFT"
                        if nullable_path or rel.temporal_validity or keep_rows
                        else "INNER",
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
