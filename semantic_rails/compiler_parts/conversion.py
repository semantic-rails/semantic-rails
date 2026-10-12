"""Conversion metrics: their base and converted events, the window that matches them, the
operand filters each side reads, and the leaf CTEs that count the matches.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from ..ast import (
    NormalizedQuery,
    normalize_query,
    refuse_child_groups,
    time_output_alias,
)
from ..dialects import SqlDialect, dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ColumnRefExpr,
    ConversionExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    SemanticExpr,
    expr_to_dict,
)
from ..ir import (
    BoundMeasure,
    LogicalPlan,
    PathSelection,
)
from ..schema import (
    MeasureConfig,
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlOrderTerm,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from . import nested
from .bind import (
    _aggregation_expr,
    _bind_measure,
    _expression_alias,
    _resolve_filter_dimension,
)
from .dependencies import (
    binding_cut,
    cut_owners,
    leaf_objects,
    recipe_objects,
    record_temporal_role,
)
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _temporal_role_index,
)
from .leaf_paths import _unique_path_selections
from .leaves import _count_key_expr
from .lowering_common import _query_metric_predicates, _refuse_converted_role
from .paths import (
    _column_ref,
    _join_condition,
    _joins_for_paths,
    _resolve_dimension_expr,
    dimension_filter,
)
from .plan_checks import (
    _path_reverse_count_distinct_safe,
    _predicate_sql_names,
    _unsupported_conversion,
)
from .post_aggregation import (
    _namespace_sql_select,
)
from .predicate import (
    _path_selection,
    _predicate_scope,
    _predicate_where_condition,
    _predicate_window_filters,
    _public_time_spec,
    _require_predicate_over_rows,
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
                    "operand filters are not supported. Constrain the conversion population "
                    "with an entity filter in the call's where, or with having, instead."
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
        with binding_cut():
            dimension_id = _resolve_filter_dimension(field, config)
        items.append(
            {
                "dimension": dimension_id,
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
        with recipe_objects(recipe.id):
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
        bound = _bind_measure(expr, config, query, conversion_operand=True)
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
        source = _measure_conversion_source(measure, bound, config, side=side)
        effective_role = str(source["time_role"])
        _temporal_role_index(config).get(effective_role)
        record_temporal_role(measure.id, effective_role)
        return source
    raise SemanticLayerError(
        "CONVERSION_NOT_SUPPORTED",
        "Conversion execution currently requires base and converted inputs to resolve to event-count measures",
        details={"expression": expr_to_dict(expr)},
    )


def _measure_conversion_source(
    measure: MeasureConfig, bound: BoundMeasure, config: PackageConfig, *, side: str
) -> dict[str, Any]:
    """The conversion source for an operand measure, which must count its entity's rows.

    Conversion lowering keys each event by its entity's key, reads the entity's table
    and never reads the measure's expression or fact relation. A measure that counts an
    expression (``CASE WHEN ... THEN key END``), another column or a fact model's rows
    would silently lose its definition, so it is rejected.
    """
    entity = _entity_index(config).get(measure.entity)
    problem = _conversion_operand_problem(measure, entity)
    if problem is not None:
        what, hint = problem
        raise SemanticLayerError(
            "CONVERSION_NOT_SUPPORTED",
            (
                f"Measure '{measure.id}' {what}, so it can't be the conversion {side} "
                "operand: a conversion operand counts its entity's rows by the entity key, "
                f"which would ignore the measure's definition. {hint}"
            ),
            details={
                "side": side,
                "measure": measure.id,
                "entity": measure.entity,
                "entity_key": list(entity.key) if entity is not None else [],
                "measure_expr": expr_to_dict(measure.expr),
            },
        )
    return {
        "measure": measure,
        "bound_measure": bound,
        "root_entity": measure.entity,
        "time_role": bound.temporal_role or _default_temporal_role(measure),
        "filters": [],
    }


def _conversion_operand_problem(measure: MeasureConfig, entity: Any) -> tuple[str, str] | None:
    """What an operand measure counts instead of its entity's rows, and the fix; None if fine."""
    key = list(entity.key) if entity is not None else []
    table = entity.table if entity is not None else ""
    counted = measure.expr
    # Named so a case difference shows: some warehouses (ClickHouse) are case-sensitive.
    the_key = f"the key '{key[0]}'" if len(key) == 1 else "the key"
    if not isinstance(counted, ColumnRefExpr):
        return (
            f"counts an expression, not {the_key} of '{measure.entity}'",
            "Use a measure that counts the entity key and restrict the operand with "
            "'filter', for example filter: {all: [{field: <dimension id>, op: 'is', "
            "value: ...}]}.",
        )
    events_hint = "Use a measure that counts the key of the entity whose rows are the events."
    if not (
        [counted.column] == key
        and counted.entity in {"", measure.entity}
        and counted.table in {"", table}
    ):
        qualifier = next((q for q in (counted.entity, counted.table) if q), "")
        where = f" of '{qualifier}'" if qualifier not in {"", measure.entity, table} else ""
        what = f"counts column '{counted.column}'{where}, not {the_key} of '{measure.entity}'"
        return what, events_hint
    if measure.source_relation not in {"", table}:
        return (
            f"counts rows of '{measure.source_relation}', not of the '{measure.entity}' "
            f"table '{table}'",
            events_hint,
        )
    return None


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
            dimension_filter(
                _resolve_dimension_expr(str(item["dimension"]), config)[0],
                str(item["dimension"]),
                item["op"],
                item["value"],
                config,
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
                                purpose="conversion_match_entity",
                            )
                        ]
                        if sel is not None
                    ]
                ),
            ]
        ),
        config,
        measure_entity=None,
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
        SqlField(_count_key_expr(source_table, list(event_entity.key)), event_key_alias)
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
        predicate_where_items.extend(_predicate_window_filters(scope["time_spec"], config))
    if predicate_where_items:
        mini_query["where"] = predicate_where_items
    if scope["time_spec"] is not None and not scope["time_spec"].get("entity_only_window"):
        mini_query["time"] = _public_time_spec(scope["time_spec"])

    _require_predicate_over_rows(predicate, config, shape="a conversion metric")
    # Conversion leaves apply only query metric_filters predicates: whole-query cuts.
    with cut_owners():
        with binding_cut():
            _entity_index(config)[predicate.entity]
        predicate_sql = nested.predicate_source_sql(config, mini_query)
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


def _conversion_sources(
    expr: ConversionExpr, config: PackageConfig, query: NormalizedQuery
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both operands' event sources, refusing a pair the window can't apply to.

    An operand that counts the conversion entity itself has one event per entity (a
    row of the entity's own table). When both operands do, on the same clock, each
    base event converts to itself at the same instant, so the window excludes
    nothing and the metric is a share of entities, not a conversion within it.
    """
    base = _resolve_conversion_source(expr.base, config, query, side="base")
    converted = _resolve_conversion_source(expr.converted, config, query, side="converted")
    entity = str(expr.entity or base["root_entity"])
    role = base["time_role"]
    if base["root_entity"] == converted["root_entity"] == entity and converted["time_role"] == role:
        keyed_by = {
            row.source_entity
            for row in config.relationships
            if row.target_entity == entity and row.cardinality == "N:1"
        }
        entities = _entity_index(config)
        candidates = [
            measure.id
            for measure in config.measures
            if measure.entity in keyed_by
            and measure.measure_class in {"event_count", "distinct_population"}
            and _conversion_operand_problem(measure, entities.get(measure.entity)) is None
        ]
        example = " (see `candidate_measures`)" if candidates else ""
        raise SemanticLayerError(
            "CONVERSION_NOT_SUPPORTED",
            (
                f"Both conversion operands count '{entity}' itself on the clock '{role}', so "
                "each base event converts to itself at the same time and the "
                f"{expr.window_value}-{expr.window_unit} window can never apply. Count events "
                f"keyed by '{entity}' in both operands instead{example}."
            ),
            details={
                "conversion_single_event": True,
                "entity": entity,
                "temporal_role": role,
                "base_measure": base["measure"].id,
                "converted_measure": converted["measure"].id,
                "candidate_measures": candidates,
            },
        )
    return base, converted


def _conversion_supported(
    expr: ConversionExpr, config: PackageConfig, query: NormalizedQuery
) -> bool:
    try:
        _conversion_sources(expr, config, query)
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


def _conversion_window_condition(expr: ConversionExpr, dialect: SqlDialect) -> Any:
    """``base <= converted < base + window``: the window is a duration after the base event.

    A 7-day window ends exactly 7 x 24 hours after the base event, not at the end of a
    calendar day; month, quarter and year windows add calendar months. The end is
    exclusive, like query time ranges, and a converted event at the base instant counts.
    """
    base_time = SqlIdentifier(parts=["base_events", "__base_event_time"])
    converted_time = SqlIdentifier(parts=["converted_events", "__converted_event_time"])
    window_end = dialect.date_add(
        expr.window_unit, SqlLiteral(expr.window_value), dialect.timestamp_cast(base_time)
    )
    return SqlBinary(
        SqlBinary(converted_time, ">=", base_time),
        "AND",
        SqlBinary(dialect.timestamp_cast(converted_time), "<", window_end),
    )


def _conversion_leaf_cte(
    expr: ConversionExpr, *, index: int, plan: LogicalPlan, config: PackageConfig
) -> SqlCte:
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    dialect = dialect_for_warehouse(config.package.warehouse)
    query = normalize_query(plan.query)
    with leaf_objects(_expression_alias(expr, config)):
        base_source, converted_source = _conversion_sources(expr, config, query)
        match_entity = str(expr.entity or base_source["root_entity"])
        entities.get(match_entity)
        # A binding dimension decides how the metric answers grouped by it: the metric reads it.
        for dimension_id in [*(expr.constant_properties or []), *(expr.dimension_bindings or {})]:
            if dimension_id not in dimensions:
                raise SemanticLayerError(
                    "OBJECT_NOT_FOUND",
                    f"Unknown dimension '{dimension_id}'",
                    details={"object_id": dimension_id},
                )
            dimensions.get(dimension_id)
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
        _refuse_converted_role(query_role, "conversion_metric")
        raw_expr = _column_ref(entities[query_dim.entity].table, query_dim.column)
        group_time_alias = time_output_alias(plan.time)
        group_time_expr = (
            dialect.date_trunc(plan.time["grain"], raw_expr) if plan.time.get("grain") else raw_expr
        )
        from .lowering_common import _source_time_window

        base_group_where.extend(_source_time_window(raw_expr, plan.time, config))

    for item in refuse_child_groups(query.where, "in a conversion metric"):
        where_target = (
            converted_group_where
            if str(item.field) in converted_side_group_dim_set
            else base_group_where
        )
        dim_expr, _ = _resolve_dimension_expr(str(item.field), config)
        where_target.append(
            dimension_filter(dim_expr, str(item.field), item.op, item.value, config)
        )

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

        join_condition: Any = _conversion_window_condition(expr, dialect)
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

    join_condition = _conversion_window_condition(expr, dialect)
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
