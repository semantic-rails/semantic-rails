from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .. import operators
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
    DistributionExpr,
    EntityValueExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    RatioExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    ShareExpr,
    TotalExpr,
    expr_kind,
)
from ..fanout import (
    analyze_fanout,
    enters_validity_window,
    record_route_choice,
    resolve_path,
    unanchored_time_valid_hop_error,
)
from ..ir import BoundMeasure, PathSelection
from ..schema import PackageConfig, RelationshipConfig
from ..sql_ast import (
    SqlBinary,
    SqlCase,
    SqlCaseWhen,
    SqlIdentifier,
    SqlIn,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlTableRef,
    build_filter_condition,
)
from .dependencies import holding, record_bound_object
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_or_raise,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
    rollup_dimension_entities,
)
from .temporal import (
    _allows_coarse_snapshot_alignment,
    _time_bound_relationship_ids,
    _validate_leaf_window_clock,
)


def _column_ref(table: str, column: str) -> SqlIdentifier:
    return SqlIdentifier(parts=[*str(table).split("."), column])


def _path_selection(
    *,
    config: PackageConfig,
    query: NormalizedQuery,
    start_entity: str,
    target_entity: str,
    purpose: str,
) -> PathSelection | None:
    if target_entity == start_entity:
        return None
    chosen, candidates = resolve_path(config, start=start_entity, target=target_entity)
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


def _split_column_ref(value: str) -> tuple[str, str]:
    table, _, column = str(value).strip().rpartition(".")
    return table or column, column


def unknown_as_null(expr: Any, dim: Any) -> Any:
    """The dimension read with its declared unknown spellings as NULL, so grouping, ``missing``,
    ``not`` and every other filter word treat them as unknown."""
    if not getattr(dim, "unknown_values", None):
        return expr
    return SqlCase(
        [
            SqlCaseWhen(
                SqlIn(expr, [SqlLiteral(value) for value in dim.unknown_values]), SqlLiteral(None)
            )
        ],
        else_expr=expr,
    )


def _resolve_dimension_expr(dim_id: str, config: PackageConfig) -> tuple[Any, str]:
    dim_index = _dimension_index(config)
    dim = dim_index.get(dim_id)
    if dim is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown dimension '{dim_id}'",
            details={"dimension": dim_id},
        )
    table = _entity_index(config)[dim.entity].table
    return unknown_as_null(_column_ref(table, dim.column), dim), dim.id


def dimension_filter(
    expr: Any, field: str, op: Any, value: Any, config: PackageConfig, *, path: str = "where"
) -> Any:
    """:func:`build_filter_condition` for a filter on the dimension ``field``: on a date or
    timestamp dimension, its text values are temporal literals. A text word reads text, or an
    id through a cast to its text; a filter with no field compares each entity's value, a
    number."""
    dim = _dimension_index(config).get(field)
    word = operators.word_of(op)
    if dim is not None:
        operators.check_text_operand(word, dim.data_type, f"{path}.op", dim.id)
    elif not field:
        operators.check_text_operand(word, "number", f"{path}.op", "each entity's value")
    temporal = dim is not None and dim.data_type in {"date", "timestamp"}
    as_text = dim is not None and operators.casts_to_text(dim.data_type)
    return build_filter_condition(expr, op, value, path=path, temporal=temporal, as_text=as_text)


def dimensions_over_column(entity: str, column: str, config: PackageConfig) -> list[Any]:
    """The dimensions of ``entity`` over its physical ``column``, named as the warehouse names
    columns: an exact name first; failing that, a name the SQL leaves unquoted (ASCII letters,
    digits and underscores, not reserved) matches another unquoted name in any ASCII case, as
    every warehouse but ClickHouse reads them. Never a Unicode casefold: "straße" is quoted, so
    it is only itself."""
    from ..renderer import _quote_ident

    dims = [dim for dim in _dimension_index(config).values() if dim.entity == entity]
    exact = [dim for dim in dims if dim.column == column]
    dialect = dialect_for_warehouse(config.package.warehouse)
    if exact or _quote_ident(column) != column or dialect.identifiers_case_sensitive:
        return exact
    return [
        dim
        for dim in dims
        if _quote_ident(dim.column) == dim.column and dim.column.lower() == column.lower()
    ]


def text_dimension(entity: str, column: str, word: str, config: PackageConfig, path: str) -> Any:
    """The one dimension over ``entity``'s ``column`` that a text word compares, its type text
    or an id.
    The word reads it as a filter on that dimension does; a column that no dimension, or
    several, declares is refused rather than guessed."""
    dims = dimensions_over_column(entity, column, config)
    if len(dims) != 1:
        declared = "several dimensions" if dims else "no dimension"
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{path}: '{word}' compares a dimension's text; {entity}.{column} is the column of "
            f"{declared}",
            details={
                "path": path,
                "dimensions": [dim.id for dim in dims],
                "recovery_hints": [
                    {
                        "code": "USE_FILTER_WORD",
                        "message": "Filter on one dimension: an aggregate's filter "
                        f'{{"all": [{{"field": "<dimension id>", "op": "{word}", "value": ...}}]}}.',
                    }
                ],
            },
        )
    operators.check_text_operand(word, dims[0].data_type, path, dims[0].id)
    return dims[0]


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


def _entity_in_terms_of_parent_relationship(
    source_entity: str, measure_entity: str, config: PackageConfig
) -> RelationshipConfig | None:
    """A child anchor can check only its unique, available relationship to the parent."""
    routes = _pair_orientations(source_entity, measure_entity, config)
    if len(routes) != 1:
        return None
    rel = routes[0][0]
    if (
        rel.source_entity != source_entity
        or rel.target_entity != measure_entity
        or rel.cardinality != "N:1"
        or rel.safety == "unsafe"
        or rel.temporal_validity
        or "forward" not in rel.allowed_directions
    ):
        return None
    pinned = get_package_analysis(config).path_preferences.get((source_entity, measure_entity))
    if pinned is not None and pinned != [rel.id]:
        return None
    return rel


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
) -> SqlIdentifier | None:
    entities = _entity_index(config)
    source = entities.get(source_entity)
    if source is None:
        return None
    source_table = source.table
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
    # A hop into the validity window cannot establish a history version from the source key.
    # Keep its temporal lookup; a window on the source table already has one version per row.
    if enters_validity_window(rel, source.table):
        return None
    # The shortcut only stands when the route resolver picks exactly the one direct
    # relationship found: a row or another route to the target may mean a different row than
    # the source table's own column, and an ambiguous pair falls through to path selection,
    # which refuses.
    try:
        resolved, _candidates = resolve_path(config, start=source_entity, target=target_entity)
    except SemanticLayerError:
        return None
    if resolved != [rel.id]:
        return None
    record_bound_object(rel, config)
    record_route_choice(source_entity, target_entity, resolved)
    return _column_ref(source_table, source_col)


def _direct_dimension_source_expr(
    source_entity: str,
    dim_id: str,
    config: PackageConfig,
) -> tuple[Any, str] | None:
    dim_index = _dimension_index(config)
    dim = dim_index.get(dim_id)
    if dim is None:
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            f"Unknown dimension '{dim_id}'",
            details={"dimension": dim_id},
        )
    target = _entity_index(config).get(dim.entity)
    if target is None or dim.column not in set(target.key_columns):
        return None
    expr = _direct_entity_key_source_expr(source_entity, dim.entity, dim.column, config)
    if expr is None:
        return None
    return unknown_as_null(expr, dim), dim.id


def keyed_entity(config: PackageConfig, dimension_id: str) -> Any:
    """The entity whose one-column key ``dimension_id`` is (never a calendar), else None."""
    dimension = _dimension_index(config).get(dimension_id)
    if dimension is None:
        return None
    entity = _entity_index(config).get(dimension.entity)
    if entity is None or entity.kind == "time" or len(entity.key_columns) != 1:
        return None
    return entity if dimension.column == entity.key_columns[0] else None


def display_key(config: PackageConfig, dimension_id: str) -> str:
    """The key dimension of the entity ``dimension_id`` names rows of (its ``display``), else
    ``""``: grouping by a name groups by the key too, so same-named entities stay apart."""
    entity = next((row for row in config.entities if row.display == dimension_id), None)
    if entity is None or entity.kind == "time" or len(entity.key_columns) != 1:
        return ""
    keys = [
        row
        for row in config.dimensions
        if row.entity == entity.id and row.column == entity.key_columns[0]
    ]
    keys.sort(key=lambda row: row.semantic_kind != "id")
    return keys[0].id if keys else ""


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
        recipe = _recipe_or_raise(config, expr.metric_recipe)
        return _expression_root_entity(recipe.expression, config)
    if isinstance(
        expr,
        (
            OffsetWindowExpr,
            MetricPredicateExpr,
            ShareExpr,
            TotalExpr,
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
    _validate_leaf_window_clock(bound.measure_id, bound.temporal_role, requested, config)
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


def _role_conversion(role: Any) -> tuple[str, str] | None:
    """The role's ``(column_timezone, timezone)`` when they differ, else ``None``."""
    column_tz = str(getattr(role, "column_timezone", "") or "").strip()
    target_tz = str(getattr(role, "timezone", "") or "").strip()
    if not column_tz or not target_tz or column_tz == target_tz:
        return None
    return column_tz, target_tz


def _apply_role_timezone(raw_expr: Any, role: Any, config: PackageConfig) -> Any:
    """Wrap a temporal role's raw column expression with a timezone conversion
    when the role's ``column_timezone`` is set and differs from ``timezone``.

    The wrap is applied to ``raw_expr`` before any ``date_trunc`` and before
    use in WHERE filter comparisons, so truncation and filtering happen in
    the target zone. A DATE clock enters through the dialect's
    ``naive_date_clock``. A path that reads a role's raw column without this
    wrap calls ``_refuse_converted_role`` first.
    """
    zones = _role_conversion(role)
    if zones is None:
        return raw_expr
    dialect = dialect_for_warehouse(config.package.warehouse)
    if _dimension_index(config)[role.dimension].data_type == "date":
        raw_expr = dialect.naive_date_clock(raw_expr)
    return dialect.convert_timezone(*zones, raw_expr)


def _day_end_anchor(time_spec: dict[str, Any], config: PackageConfig) -> Any:
    """The end of each row's day on the query's clock: the next midnight after the day the row's
    time falls in, as ``time.grain: day`` buckets it (in the role's ``timezone``), back in the
    column's own zone so it compares with the validity columns as the row's time does."""
    role = _temporal_role_index(config)[str(time_spec["temporal_role"])]
    dialect = dialect_for_warehouse(config.package.warehouse)
    local = _apply_role_timezone(_time_anchor_expr(time_spec, config), role, config)
    edge = dialect.date_add("day", SqlLiteral(1), dialect.date_trunc("day", local))
    zones = _role_conversion(role)
    return dialect.convert_timezone(zones[1], zones[0], edge) if zones else edge


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
    enters_window = enters_validity_window(rel, entities[current_entity].table)
    if time_anchor is None and enters_window:
        # analyze_fanout refuses such a hop when it plans the path; this is the join's own guard.
        raise unanchored_time_valid_hop_error([(rel.id, next_entity)], [rel.id])
    # Leaving the window table reads an existing version; only entering it selects a version.
    if time_anchor is not None and enters_window:
        valid_from = str(rel.temporal_validity.get("valid_from", "")).strip()
        valid_to = str(rel.temporal_validity.get("valid_to", "")).strip()
        # The version valid at the row's time, or at the last instant of its day: one that
        # starts before the next midnight and hasn't ended by it.
        start, end = "<=", ">"
        if rel.temporal_validity.get("as_of") == "end_of_day":
            time_anchor, start, end = _day_end_anchor(dict(time_spec or {}), config), "<", ">="
        if valid_from:
            table, column = _split_column_ref(valid_from)
            condition = SqlBinary(
                condition, "AND", SqlBinary(_column_ref(table, column), start, time_anchor)
            )
        if valid_to:
            table, column = _split_column_ref(valid_to)
            valid_to_expr = _column_ref(table, column)
            condition = SqlBinary(
                condition,
                "AND",
                SqlBinary(
                    SqlBinary(valid_to_expr, end, time_anchor), "OR", SqlIsNull(valid_to_expr)
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


def clock_hops(
    source_entity: str, clock_entity: str, config: PackageConfig
) -> list[SqlJoin] | None:
    """The joins from each ``source_entity`` row to the one ``clock_entity`` row that dates it,
    on ``resolve_path``'s route (the one a series on that clock joins), as a time role joins it
    (``_joins_for_paths``, which notes the route). None when the pair has no route, or a hop can
    reach more than one row, is valid over time or reads a table twice: then no single row
    dates a source row."""
    try:
        route, _ = resolve_path(config, start=source_entity, target=clock_entity)
    except SemanticLayerError:
        return None
    relationships = _relationship_index(config)
    entities = _entity_index(config)
    tables = {entities[source_entity].table}
    current = source_entity
    for rel_id in route:
        rel = relationships[rel_id]
        if rel.temporal_validity or not _reaches_at_most_one(rel, current):
            return None
        current = rel.target_entity if current == rel.source_entity else rel.source_entity
        if entities[current].table in tables:
            return None
        tables.add(entities[current].table)
    if not route:
        return None
    selection = PathSelection(clock_entity, "time", list(route), [list(route)])
    return _joins_for_paths(source_entity, [selection], config, measure_entity=None)


def _is_lookup_hop(rel: RelationshipConfig, current_entity: str, config: PackageConfig) -> bool:
    """True when the hop reaches at most one row for each current row (N:1, 1:1) and the
    warehouse's outer join reads NULL, not a type default, for an unmatched row (not ClickHouse).
    """
    if not dialect_for_warehouse(config.package.warehouse).outer_lookup_joins:
        return False
    return _reaches_at_most_one(rel, current_entity)


def _outer_joins(config: PackageConfig) -> bool:
    """Whether the warehouse's outer join reads NULL, not a type default, for an unmatched row."""
    return dialect_for_warehouse(config.package.warehouse).outer_lookup_joins


def _is_link_hop(rel: RelationshipConfig, current_entity: str, config: PackageConfig) -> bool:
    """True when the hop walks a link in reverse, from a one-per-key model to the rows of its
    own relation, on a warehouse whose outer join reads NULL (as for a lookup)."""
    return (
        rel.link
        and current_entity == rel.target_entity
        and dialect_for_warehouse(config.package.warehouse).outer_lookup_joins
    )


# The reads that need the looked-up row, so they join every hop of their path INNER: a time
# role (a row with no time has no bucket), a metric predicate's route to the entity its set is
# matched on, a conversion's converted events and the dimensions its metric filter is matched on
# (a conversion's base events keep theirs: they are the rate's denominator), and the measure's
# own entity read back from the anchor of an entity_in_terms_of leaf (a child row whose parent
# has no record counts no entity).
_INNER_LOOKUP_PURPOSES = frozenset(
    {
        "time",
        "metric_predicate",
        "conversion_dimension",
        "conversion_match_entity",
        "entity_in_terms_of_root",
    }
)
_inner_lookups: ContextVar[bool] = ContextVar("inner_lookups", default=False)
# A conversion's base events are its rate's denominator, so these reads keep their rows even
# inside a metric filter's own query (``_inner_lookups``): a rate shown and a rate filtered on
# must count the same events. Their missing rows read NULL and match nothing.
_KEEPS_ROWS_PURPOSES = frozenset(
    {"conversion_base_property", "conversion_base_match_entity", "conversion_base_filter"}
)


@contextmanager
def inner_lookups() -> Iterator[None]:
    """Join every lookup INNER while a metric predicate's own query or a distribution's
    per-entity values lower: each holds the entities that have rows, never a NULL key for rows
    that have none."""
    with holding(_inner_lookups, True):
        yield


def rollup_held_lookups(
    config: PackageConfig, measure_entity: str, path_selections: Iterable[PathSelection]
) -> set[str]:
    """The models ``path_selections`` look up whose dimensions a rollup of ``measure_entity``
    holds: read from that model's own rows, their lookups join INNER, as the rollup was built."""
    held = rollup_dimension_entities(config, measure_entity) - {measure_entity}
    return {
        row.target_entity
        for row in path_selections
        if row.chosen_path and row.target_entity in held
    }


def _joins_for_paths(
    source_entity: str,
    path_selections: Iterable[PathSelection],
    config: PackageConfig,
    *,
    measure_entity: str | None,
    time_spec: dict[str, Any] | None = None,
    table_overrides: dict[str, str] | None = None,
) -> list[SqlJoin]:
    """The joins for ``path_selections``. Every leaf joins its paths here, so this is the one
    place that decides whether a hop can remove a row.

    An N:1 or 1:1 hop never removes a row: it joins LEFT, so a row whose foreign key is NULL
    or unmatched stays, with NULL for everything the hop looks up, whatever reads it (a
    grouping, a filter, the measure's own filter, an aggregate_if's condition or its
    expression). It joins INNER only when a read in ``_INNER_LOOKUP_PURPOSES`` walks the same
    hop, inside a metric predicate's own query or a distribution's per-entity values
    (``inner_lookups``), on the path to a dimension a rollup of the measure's model holds
    pre-joined (so the base answers as the rollup does), or on a warehouse whose outer join
    reads a type default instead of NULL (``_is_lookup_hop``). Hops that fan out join INNER,
    except a listing's (``listings``), which keep their rows as its lookups do, and every hop
    after a temporal-validity hop joins LEFT.

    ``measure_entity`` is the model of the measure the leaf aggregates, or None for dimensions
    alone and conversions. A rollup belongs to the measures of the model it aggregates, so
    only a rollup of ``measure_entity`` changes a join, and only when the leaf reads that
    model's own rows (``source_entity``). Every other leaf joins as the base tables do: a
    rollup of an entity_in_terms_of anchor's model never applies to the measure counted from
    it. Such a leaf that looks up a dimension a rollup of the measure's model holds is refused:
    only the measure's own leaf answers as that rollup does. It must also join the measure's
    own rows INNER: a child whose parent has no record counts no parent. The internal guard
    checks the unique relationship and emitted joins, so a missing, nullable or different
    parent check cannot silently count it.
    """
    entities = _entity_index(config)
    relationships = _relationship_index(config)
    path_selections = list(path_selections)
    prejoined: set[str] = set()
    if measure_entity == source_entity:
        prejoined = rollup_dimension_entities(config, source_entity)
    elif measure_entity is not None and (
        held := rollup_held_lookups(config, measure_entity, path_selections)
    ):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"A measure of '{measure_entity}' read from the rows of '{source_entity}' cannot "
            "join a dimension a rollup of its own model holds as that rollup does.",
            details={
                "measure_entity": measure_entity,
                "source_entity": source_entity,
                "rollup_held_entities": sorted(held),
            },
        )
    inner_hops: set[tuple[str, str]] = set()
    for selection in path_selections:
        if (
            selection.purpose not in _INNER_LOOKUP_PURPOSES
            and selection.target_entity not in prejoined
        ):
            continue
        current = source_entity
        for rel_id in selection.chosen_path:
            rel = relationships[rel_id]
            inner_hops.add((rel.id, current))
            current = rel.target_entity if current == rel.source_entity else rel.source_entity
    keep_rows = not _inner_lookups.get()
    # A hop keeps its rows for the base-event reads only when no other read walks it.
    keeps_rows = {
        rel_id
        for selection in path_selections
        if selection.purpose in _KEEPS_ROWS_PURPOSES
        for rel_id in selection.chosen_path
    } - {
        rel_id
        for selection in path_selections
        if selection.purpose not in _KEEPS_ROWS_PURPOSES
        for rel_id in selection.chosen_path
    }
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
        # A rewrite's anchor reads the root's route from the anchor's rows (the hop back to the
        # root, then the rest of the route): the root's own note names that route.
        if (
            selection.analysis.get("status") != "entity_in_terms_of"
            and selection.purpose != "entity_in_terms_of_root"
        ):
            record_route_choice(source_entity, selection.target_entity, selection.chosen_path)
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
                kept = (keep_rows or rel.id in keeps_rows) and join_key not in inner_hops
                lookup = kept and (
                    _is_lookup_hop(rel, current_entity, config)
                    or (selection.purpose == "listing" and _outer_joins(config))
                )
                # A grouping across a link keeps a key with no row there (an orphan the lookup
                # before it kept): it reads the NULL group, not no group.
                link = (
                    kept
                    and selection.purpose == "group_by"
                    and _is_link_hop(rel, current_entity, config)
                )
                nullable_path = nullable_path or link
                joins.append(
                    SqlJoin(
                        join_type="LEFT"
                        if nullable_path or rel.temporal_validity or lookup
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
    if measure_entity is not None and measure_entity != source_entity:
        measure_table = overrides.get(measure_entity, entities[measure_entity].table)
        parent = _entity_in_terms_of_parent_relationship(source_entity, measure_entity, config)
        if (
            parent is None
            or joined_via.get(measure_table) != (parent.id, source_entity)
            or not any(
                join.table.name == measure_table and join.join_type == "INNER" for join in joins
            )
        ):
            raise SemanticLayerError(
                "REWRITE_NOT_SUPPORTED",
                f"A measure of '{measure_entity}' read from the rows of '{source_entity}' "
                "must require a matching row through the counted parent relationship.",
                details={"measure_entity": measure_entity, "source_entity": source_entity},
            )
    return joins


def _join_condition(keys: list[str], left_alias: str, right_alias: str, *, dialect: Any) -> Any:
    if not keys:
        return SqlLiteral(True)
    current: Any = dialect.null_safe_eq(
        SqlIdentifier(parts=[left_alias, keys[0]]), SqlIdentifier(parts=[right_alias, keys[0]])
    )
    for key in keys[1:]:
        current = SqlBinary(
            current,
            "AND",
            dialect.null_safe_eq(
                SqlIdentifier(parts=[left_alias, key]), SqlIdentifier(parts=[right_alias, key])
            ),
        )
    return current
