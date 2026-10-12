"""Metric predicates (entity filters): their scope, the clocks and windows they read, and the
CTEs and join that apply one to the outer query.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime
from typing import Any

from .. import operators
from ..ast import (
    NormalizedQuery,
    normalize_query,
    refuse_child_groups,
    time_output_alias,
)
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    expr_to_dict,
)
from ..fanout import (
    analyze_fanout,
    resolve_path,
)
from ..ir import (
    LogicalPlan,
    MeasurePlan,
    PathSelection,
)
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlBinary,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    build_filter_condition,
)
from . import nested
from .bind import (
    _parse_public_expr,
    _scoped_predicate_expr_payload,
    earlier_settlement,
    earlier_settlement_applies,
)
from .dependencies import (
    binding_cut,
)
from .empty_groups import (
    ZERO_MEASURE_CLASSES,
    absent_entities_gate,
    expr_resolves_to_zero,
    require_settled_source,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _recipe_index,
    _temporal_role_index,
)
from .lowering_common import (
    _path_has_temporal_validity,
    _refuse_converted_role,
)
from .paths import (
    _column_ref,
    _direct_entity_key_source_expr,
    _entity_key_dimension_ids,
    _expression_root_entity,
    _leaf_time_role,
    unknown_as_null,
)
from .plan_checks import _LOOKUP_TIME_EXPRS, _predicate_metric_label, _predicate_sql_names
from .post_aggregation import (
    _namespace_sql_select,
)
from .temporal import (
    _add_grain,
    _expr_compatible_temporal_roles,
    _floor_to_grain,
    _is_grain_boundary,
    _parse_time_literal,
    _time_bound_relationship_ids,
    _window_measure_exprs,
)


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


def _predicate_where_condition(op: str, value: Any) -> Any:
    """An entity's aggregate meets the threshold. An unknown (NULL) aggregate meets none, so a
    ``not``, which keeps unknown rows of a row or result filter, keeps no unknown entity here."""
    alias_ref = SqlIdentifier(parts=["predicate_source", "__predicate_value"])
    condition = build_filter_condition(alias_ref, op, value, path="metric_predicate")
    word, value = operators.normalize_condition(op, value, path="metric_predicate", internal=True)
    if operators.rejects_unknown(word, value):
        return condition
    return SqlBinary(condition, "AND", SqlBinary(alias_ref, "IS NOT", SqlLiteral(None)))


# A distinct count of a population is 0 over no rows too; it stays out of the empty-group
# settling of a query's groups, whose null semantics differ.
_ZERO_ON_MISSING_PREDICATE_CLASSES = {*ZERO_MEASURE_CLASSES, "distinct_population"}


def _zero_satisfies_threshold(op: str, value: Any) -> bool:
    """Whether a value of 0 passes the ``op value`` threshold of a metric_predicate."""
    try:
        word, value = operators.normalize_condition(
            op, value, path="metric_predicate", internal=True
        )
    except SemanticLayerError:
        return False
    return operators.holds(word, value, 0)


def _require_predicate_over_rows(
    predicate: MetricPredicateExpr, config: PackageConfig, *, shape: str
) -> None:
    """Refuse, in a shape that only keeps entities with rows, a threshold that zero passes."""
    if _predicate_includes_entities_without_rows(predicate, config):
        raise SemanticLayerError(
            "INVALID_METRIC_PREDICATE",
            (
                f"metric_predicate threshold '{predicate.op} {predicate.value}' is satisfied by "
                f"an entity with no rows, which {shape} cannot count yet, so the query is "
                "refused rather than answered from the entities that have rows."
            ),
            details={"entity": predicate.entity, "predicate": expr_to_dict(predicate)},
        )


def _predicate_input_zero_on_missing(expr: SemanticExpr, config: PackageConfig) -> bool:
    """Whether the predicate input is 0, not NULL, for an entity with no rows.

    A count or sum is, and so is an add or subtract of them: the source query settles each
    operand the way any query does (see ``empty_groups``), so this is the same rule, never a
    second one. Only a bare measure may also be a distinct population.
    """
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return recipe is not None and _predicate_input_zero_on_missing(recipe.expression, config)
    if isinstance(expr, ArithmeticExpr):
        return expr_resolves_to_zero(expr, config)
    return expr_resolves_to_zero(expr, config, _ZERO_ON_MISSING_PREDICATE_CLASSES)


def _predicate_includes_entities_without_rows(
    predicate: MetricPredicateExpr, config: PackageConfig
) -> bool:
    """Whether entities with no input rows must qualify for this predicate.

    An entity with no rows never reaches the predicate's aggregate, so a
    threshold that zero passes (``count = 0``, ``< 3``, ``<= 0``) would
    otherwise match nothing. Only where "no rows" is 0 (a count or sum) does the
    entity qualify, and, as for every entity the source lists, only while the measure has
    data somewhere in the predicate's scope. An average, minimum, maximum or ratio over no
    rows is NULL, which no threshold satisfies, so those entities stay out.
    """
    return _zero_satisfies_threshold(
        predicate.op, predicate.value
    ) and _predicate_input_zero_on_missing(predicate.input, config)


def _predicate_reads_one_measure(expr: SemanticExpr, config: PackageConfig) -> bool:
    """Whether the predicate input is one measure, so a NULL value means that measure's amounts
    are unknown for the entity. An add or subtract is NULL where any operand is, though the
    others have data, which the absent-entity gate can't tell from no data at all."""
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return recipe is not None and _predicate_reads_one_measure(recipe.expression, config)
    return isinstance(expr, MeasureRefExpr | AggregateExpr)


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
        operators.COMPARISON_SQL[operators.word_of(predicate.op)],
        SqlIdentifier(parts=["predicate_threshold", "__threshold"]),
    )
    return [threshold_cte], [cross_join], where_condition


def _predicate_time_alias(temporal_role: str, grain: str) -> str:
    return time_output_alias({"temporal_role": temporal_role, "grain": grain})


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
    input_root = _predicate_input_root_entity(predicate, config)
    candidates: list[str] = []
    for dim_id in list(query.group_by or []):
        dim = dimensions[dim_id]
        if dim.entity in {predicate.entity, input_root} or dim.entity in candidates:
            continue
        candidates.append(dim.entity)
    return candidates


def _entity_determines(
    *,
    config: PackageConfig,
    source_entity: str,
    target_entity: str,
    time_bound_relationships: set[str],
) -> bool:
    if source_entity == target_entity:
        return True
    try:
        chosen, _candidates = resolve_path(config, start=source_entity, target=target_entity)
    except SemanticLayerError:
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
                time_bound_relationships=time_bound_relationships,
            ):
                redundant = True
                break
        if not redundant and entity_id not in retained:
            retained.append(entity_id)
    return retained


def _cross_clock_predicate_error(
    predicate: MetricPredicateExpr, query: NormalizedQuery, compatible_roles: set[str]
) -> SemanticLayerError:
    assert query.time is not None
    clocks = sorted(compatible_roles)
    return SemanticLayerError(
        "INVALID_TEMPORAL_BINDING",
        (
            f"The contextual metric_predicate measures {_predicate_metric_label(predicate)!r} "
            f"on a different clock (see `compatible`) from the query's {query.time.temporal_role}. "
            "Matching the two by calendar bucket would compare different clocks, so the "
            "query is refused."
        ),
        details={
            "requested": query.time.temporal_role,
            "compatible": clocks,
            "predicate": expr_to_dict(predicate),
            "recovery_hints": [
                {
                    "code": "CHOOSE_PREDICATE_CLOCK",
                    "message": (
                        "To apply the threshold per period, set time.temporal_role "
                        "to one of `compatible`. To apply it over all time, write "
                        "over: all_time."
                    ),
                }
            ],
        },
    )


def _validate_predicate_metric_clocks(
    predicate: MetricPredicateExpr,
    query: NormalizedQuery,
    config: PackageConfig,
    predicate_temporal_role: str,
) -> None:
    """The window clock filters every measure in the input, so none may be bound away from it."""
    for expr in _window_measure_exprs(predicate.input, config):
        # The measure's pin, then the query's override, then its declared clocks.
        bound_roles = _expr_compatible_temporal_roles(expr, config, query)
        if bound_roles and predicate_temporal_role not in bound_roles:
            clocks = sorted(bound_roles)
            raise SemanticLayerError(
                "INVALID_TEMPORAL_BINDING",
                f"Measure '{expr.measure}' in the metric predicate input is bound to "
                f"{', '.join(clocks)}, excluding window clock '{predicate_temporal_role}'",
                details={
                    "requested": predicate_temporal_role,
                    "compatible": clocks,
                    "predicate": expr_to_dict(predicate),
                    "recovery_hints": [
                        {
                            "code": "CHOOSE_PREDICATE_CLOCK",
                            "message": (
                                f"The input is bound to {', '.join(clocks)}; set "
                                "time.temporal_role to one of them, or write over: "
                                "all_time to apply the predicate over all time."
                            ),
                        }
                    ],
                },
            )


def _refuse_overridden_predicate_input(
    predicate: MetricPredicateExpr, query: NormalizedQuery, config: PackageConfig
) -> None:
    """The query's temporal_role_overrides never reach a metric predicate input's own query.

    So a conversion, time window or nested predicate inside the input would read an
    overridden measure on its own clock, and the input refuses. A pinned leaf refuses too when
    overridden, though its pin would win: conservative, and never a wrong number.
    """
    if not query.temporal_role_overrides:
        return

    def filter_expressions(spec: Any) -> Iterable[SemanticExpr]:
        # Only expression clauses and boolean containers are expression positions.
        # A field clause's value, even a reference-shaped mapping, stays data.
        if isinstance(spec, list):
            for clause in spec:
                yield from filter_expressions(clause)
        elif isinstance(spec, dict):
            if isinstance(spec.get("expression"), dict):
                yield _parse_public_expr(spec["expression"])
            for key in ("all", "any", "not"):
                yield from filter_expressions(spec.get(key))

    def input_measures(expr: Any, inside: bool = False) -> Iterable[str]:
        inside = inside or isinstance(expr, (*_LOOKUP_TIME_EXPRS, MetricPredicateExpr))
        if inside and isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
            yield expr.measure
        if isinstance(expr, MetricRecipeRefExpr):
            recipe = _recipe_index(config).get(expr.metric_recipe)
            if recipe is not None:
                yield from input_measures(recipe.expression, inside)
        elif isinstance(expr, ScopedAggregateExpr):
            for raw in expr.predicates:
                yield from input_measures(
                    _parse_public_expr(_scoped_predicate_expr_payload(raw)), inside
                )
        elif isinstance(expr, AggregateExpr):
            for child in filter_expressions(expr.filter):
                yield from input_measures(child, inside)
        if is_dataclass(expr):
            for item in fields(expr):
                value = getattr(expr, item.name)
                for child in value if isinstance(value, list) else [value]:
                    if is_dataclass(child):
                        yield from input_measures(child, inside)

    overridden = set(query.temporal_role_overrides) & set(input_measures(predicate.input))
    if not overridden:
        return
    measures = sorted(overridden)
    raise SemanticLayerError(
        "INVALID_TEMPORAL_BINDING",
        "temporal_role_overrides do not apply inside a metric predicate input, so the "
        f"override for {', '.join(f'measure {m!r}' for m in measures)} would be ignored by "
        "the conversion, time window or nested predicate that reads it",
        details={
            "measures": measures,
            "predicate": expr_to_dict(predicate),
            "recovery_hints": [
                {
                    "code": "CHOOSE_PREDICATE_CLOCK",
                    "message": (
                        "Remove the override for this measure, or use a metric whose "
                        "definition binds that clock."
                    ),
                }
            ],
        },
    )


def _predicate_input_clocks(
    predicate: MetricPredicateExpr, query: NormalizedQuery, config: PackageConfig
) -> set[str]:
    """The clocks a predicate's input reads; a metric whose measures all pin one clock reads it."""
    roles = _expr_compatible_temporal_roles(predicate.input, config, query)
    if len(roles) < 2 or (query.time is not None and query.time.temporal_role in roles):
        return roles
    # Only the input's own pins count: the query's overrides never reach its source.
    unpinned = replace(query, temporal_role_overrides={})
    leaves = [
        _expr_compatible_temporal_roles(expr, config, unpinned)
        for expr in _window_measure_exprs(predicate.input, config)
    ]
    pinned = set.intersection(*leaves) & roles if leaves else set()
    return pinned if len(pinned) == 1 else roles


def _predicate_input_clock_error(
    predicate: MetricPredicateExpr, query: NormalizedQuery, clocks: set[str], why: str
) -> SemanticLayerError:
    assert query.time is not None
    return SemanticLayerError(
        "INVALID_TEMPORAL_BINDING",
        f"The entity filter on {_predicate_metric_label(predicate)!r} {why}",
        details={
            "requested": query.time.temporal_role,
            "compatible": sorted(clocks),
            "predicate": expr_to_dict(predicate),
            "recovery_hints": [
                {
                    "code": "CHOOSE_PREDICATE_CLOCK",
                    "message": (
                        "Set time.temporal_role to one of `compatible`, filter by a package "
                        "metric that pins one clock, or use over: all_time."
                    ),
                }
            ],
        },
    )


def _predicate_time_spec(
    predicate: MetricPredicateExpr, query: NormalizedQuery, config: PackageConfig
) -> dict[str, Any] | None:
    # Every predicate source reads its clock here: both builders through _predicate_scope, and
    # the anchored entity-set ratio directly. So the override guard runs here, for any alignment.
    _refuse_overridden_predicate_input(predicate, query, config)
    if predicate.scope_mode == "entity_only":
        if predicate.time_grain:
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                "metric_predicate time_grain requires contextual scope with query.time",
            )
        if predicate.time_alignment in {"query_window", "rolling_window_in_period"}:
            if query.time is None or query.time.start is None or query.time.end is None:
                raise SemanticLayerError(
                    "PREDICATE_GRAIN_UNSAFE",
                    "an entity filter over the window needs a bounded time window",
                    details={
                        "recovery_hints": [
                            {
                                "code": "BOUND_THE_WINDOW",
                                "message": (
                                    "Add a window to time (last, this, or start and end), "
                                    "or use over: all_time."
                                ),
                            }
                        ]
                    },
                )
            # The window applies on the input's own clock: the entity's rows inside it.
            compatible_roles = _predicate_input_clocks(predicate, query, config)
            predicate_temporal_role = query.time.temporal_role
            if query.time.temporal_role not in compatible_roles:
                if len(compatible_roles) != 1:
                    raise _predicate_input_clock_error(
                        predicate,
                        query,
                        compatible_roles,
                        "reads several clocks (see `compatible`), so the window cannot "
                        "choose which of the entity's rows fall inside it"
                        if compatible_roles
                        else "reads no clock, so none of the entity's rows fall inside the window",
                    )
                predicate_temporal_role = next(iter(compatible_roles))
            _validate_predicate_metric_clocks(predicate, query, config, predicate_temporal_role)
            return {
                "temporal_role": predicate_temporal_role,
                "output_temporal_role": "",
                "grain": "",
                "start": query.time.start,
                "end": query.time.end,
                "entity_only_window": True,
                "time_alignment": predicate.time_alignment,
                "window": {"unit": predicate.window_unit, "value": predicate.window_value}
                if predicate.window_unit and predicate.window_value
                else {},
            }
        return None
    per_period = predicate.scope_mode == "entity_period"
    if per_period and (query.time is None or not query.time.grain):
        raise SemanticLayerError(
            "PREDICATE_GRAIN_UNSAFE",
            "an entity filter per period needs time.grain",
            details={
                "recovery_hints": [
                    {
                        "code": "SET_TIME_GRAIN",
                        "message": 'Add "grain" to time, or use over: window or all_time.',
                    }
                ]
            },
        )
    if query.time is None:
        return None
    compatible_roles = _predicate_input_clocks(predicate, query, config)
    predicate_temporal_role = query.time.temporal_role
    if query.time.temporal_role not in compatible_roles:
        if not compatible_roles:
            raise SemanticLayerError(
                "PREDICATE_GRAIN_UNSAFE",
                "Metric predicate input does not expose a compatible temporal role",
                details={
                    "compatible": sorted(compatible_roles),
                    "predicate": expr_to_dict(predicate),
                },
            )
        # A contextual predicate matches another clock's calendar buckets only when it asks
        # for it and names exactly one clock; it is never a silent default. A per-period entity
        # filter reads its input on the input's own clock, bucketed by the output period.
        if not per_period and (
            predicate.time_alignment != "same_query_period" or len(compatible_roles) != 1
        ):
            raise _cross_clock_predicate_error(predicate, query, compatible_roles)
        if len(compatible_roles) != 1:
            raise _predicate_input_clock_error(
                predicate,
                query,
                compatible_roles,
                "reads several clocks (see `compatible`), none of them the query's, so its "
                "periods are ambiguous",
            )
        predicate_temporal_role = next(iter(compatible_roles))
    _validate_predicate_metric_clocks(predicate, query, config, predicate_temporal_role)
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
    }


def _public_time_spec(time_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(time_spec or {}).items()
        if key in {"temporal_role", "grain", "start", "end", "fill"}
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
    _refuse_converted_role(role, "predicate_period_join")
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
    context_dimensions = []
    if predicate.scope_mode == "contextual":
        # A fact attribute describes the returned group, not one fact row per entity key.
        context_dimensions = [
            dim_id
            for dim_id in query.group_by
            if _dimension_index(config)[dim_id].entity == input_root
            and input_root != predicate.entity
        ]
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
        for item in refuse_child_groups(query.where, "in a metric predicate's scope"):
            dim = _dimension_index(config)[item.field]
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=input_root,
                target_entity=dim.entity,
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
    group_by_dims.extend(context_dimensions)
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
        "context_dimensions": context_dimensions,
        "time_spec": time_spec,
        "time_alias": time_alias,
        "source_time_alias": source_time_alias,
        "filter_items": list(query.where or []) if predicate.scope_mode == "contextual" else [],
    }


def _predicate_source_query(
    predicate: MetricPredicateExpr, scope: dict[str, Any], config: PackageConfig
) -> dict[str, Any]:
    """The query whose rows are each entity's predicate value, read in the predicate's scope."""
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": expr_to_dict(predicate.input), "as": "__predicate_value"}],
        "group_by": list(scope["group_by_dims"]),
    }
    where = [asdict(item) for item in scope["filter_items"]]
    if scope["time_spec"] is not None and scope["time_spec"].get("entity_only_window"):
        where.extend(_predicate_window_filters(scope["time_spec"], config))
    if where:
        query["where"] = where
    if scope["time_spec"] is not None and not scope["time_spec"].get("entity_only_window"):
        query["time"] = _public_time_spec(scope["time_spec"])
    return query


def _predicate_set_fields(scope: dict[str, Any]) -> tuple[list[SqlField], list[str]]:
    """A predicate set's columns, read from its source, and the keys it projects."""
    keys = list(scope["group_by_dims"])
    fields = [SqlField(SqlIdentifier(parts=["predicate_source", key]), key) for key in keys]
    if scope["time_alias"]:
        fields.append(
            SqlField(
                SqlIdentifier(parts=["predicate_source", scope["source_time_alias"]]),
                scope["time_alias"],
            )
        )
        keys.append(scope["time_alias"])
    return fields, keys


def _predicate_window_filters(time: dict[str, Any], config: PackageConfig) -> list[dict[str, Any]]:
    """Build raw-column window filters only for roles without timezone conversion."""
    from .lowering_common import _source_time_window

    role = _temporal_role_index(config)[str(time["temporal_role"])]
    _refuse_converted_role(role, "entity_only_predicate_window")
    column = SqlIdentifier(parts=[role.dimension])
    filters = []
    for bound in _source_time_window(column, time, config):
        if bound.left != column:
            return [{"field": role.dimension, "op": "IN", "value": []}]
        filters.append({"field": role.dimension, "op": bound.op, "value": bound.right.value})
    return filters


def _predicate_ctes_and_join(
    predicate: MetricPredicateExpr,
    *,
    index: int,
    plan: LogicalPlan,
    measure_plan: MeasurePlan,
    config: PackageConfig,
) -> tuple[list[SqlCte], list[SqlJoin], list[Any]]:
    """Return the predicate's CTEs, the joins to the outer leaf, and extra WHERE conditions.

    Usually the set holds the qualifying entities and the join is INNER. When entities
    with no rows must qualify, the set holds the entities that fail the threshold, and
    the leaf keeps a row only if that LEFT JOIN finds no failing entity for it, and, for a
    count or sum, only while the source holds a settled value (see ``empty_groups``).
    """
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
    mini_query = _predicate_source_query(predicate, scope, config)
    with binding_cut():
        _entity_index(config)[predicate.entity]
    without_rows = _predicate_includes_entities_without_rows(predicate, config)
    # Over several measures an entity's value is NULL where one operand is unknown, though the
    # others have data, and the gate below would read that as no data in scope. So that
    # source keeps the earlier settlement, which reads an operand's unknown amounts as 0
    # where its measure has data in scope; a source beside a distribution keeps it too.
    several = without_rows and not _predicate_reads_one_measure(predicate.input, config)
    with earlier_settlement(several):
        earlier = earlier_settlement_applies()
        predicate_sql = nested.predicate_source_sql(config, mini_query)
    source_name, set_name = _predicate_sql_names(predicate, index, scope)
    source_cte = SqlCte(
        name=source_name, query=_namespace_sql_select(predicate_sql, f"{source_name}__")
    )
    select_fields, projected_keys = _predicate_set_fields(scope)
    # `source_cte` already produces one row per key combination; the outer
    # GROUP BY would just re-group identical rows. SELECT DISTINCT expresses
    # "set of qualifying ids" idiomatically.
    threshold_ctes, threshold_joins, where_condition = _inline_threshold_cte_and_where(
        predicate=predicate,
        set_name=set_name,
        source_name=source_name,
        config=config,
    )
    if without_rows:
        # The set holds the entities that fail the threshold, and, unless the source keeps the
        # earlier settlement, those whose value is unknown (NULL: their rows have no values),
        # which meets no threshold. Never coalesce the value: the source is settled like any
        # query, so only an entity it doesn't list has no rows, and with no settled value at
        # all the gate below drops every row.
        value = SqlIdentifier(parts=["predicate_source", "__predicate_value"])
        word, threshold = operators.normalize_condition(
            predicate.op, predicate.value, path="metric_predicate", internal=True
        )
        failing = operators.negation(word)
        where_condition = build_filter_condition(value, failing, threshold, path="metric_predicate")
        # ``not`` keeps unknown values; the earlier settlement counts them as passing.
        if earlier and operators.OPERATORS[failing].keeps_unknown:
            where_condition = SqlBinary(
                where_condition, "AND", SqlBinary(value, "IS NOT", SqlLiteral(None))
            )
        elif not earlier and not operators.OPERATORS[failing].keeps_unknown:
            where_condition = SqlBinary(where_condition, "OR", SqlIsNull(value))
    set_query = SqlSelect(
        select=select_fields,
        from_table=SqlTableRef(name=source_name, alias="predicate_source"),
        joins=threshold_joins,
        where=[where_condition],
        distinct=True,
    )
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
    outer_keys = [outer_key_expr]
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
        outer_keys.append(outer_key_expr)
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
            context_key_expr = _column_ref(_entity_index(config)[dim.entity].table, dim.column)
            outer_keys.append(context_key_expr)
            join_condition = SqlBinary(
                join_condition,
                "AND",
                SqlBinary(context_key_expr, "=", SqlIdentifier(parts=[set_name, dim_id])),
            )
    for dim_id in scope["context_dimensions"]:
        dim = _dimension_index(config)[dim_id]
        join_condition = SqlBinary(
            join_condition,
            "AND",
            dialect_for_warehouse(config.package.warehouse).null_safe_eq(
                unknown_as_null(
                    _column_ref(_entity_index(config)[dim.entity].table, dim.column), dim
                ),
                SqlIdentifier(parts=[set_name, dim_id]),
            ),
        )
    if scope["time_spec"] is not None and scope["time_alias"]:
        query = normalize_query(plan.query)
        outer_temporal_role = _leaf_time_role(measure_plan.bound_measure, query, config)
        outer_time_expr = _predicate_time_join_expr(
            source_entity=measure_plan.source_entity,
            role_id=outer_temporal_role,
            grain=str(scope["time_spec"]["grain"]),
            config=config,
        )
        outer_keys.append(outer_time_expr)
        join_condition = SqlBinary(
            join_condition,
            "AND",
            SqlBinary(outer_time_expr, "=", SqlIdentifier(parts=[set_name, scope["time_alias"]])),
        )
    ctes = [source_cte, *threshold_ctes, set_cte]
    if not without_rows:
        return (
            ctes,
            [SqlJoin(join_type="INNER", table=SqlTableRef(name=set_name), on=join_condition)],
            [],
        )
    # A leaf row with no entity, context or period key is not an entity with no rows.
    keep_rows: list[Any] = [
        SqlIsNull(SqlIdentifier(parts=[set_name, key_dim_map[0][1]])),
        *(SqlBinary(key_expr, "IS NOT", SqlLiteral(None)) for key_expr in outer_keys),
    ]
    joins = [SqlJoin(join_type="LEFT", table=SqlTableRef(name=set_name), on=join_condition)]
    if expr_resolves_to_zero(predicate.input, config):
        # An entity absent from the source reads 0 only where the measures have data in scope,
        # the test the guard applied to the entities it lists; a distinct population has none.
        require_settled_source(predicate_sql, {"predicate": expr_to_dict(predicate)})
        gate_cte, gate_join, gate_condition = absent_entities_gate(
            f"{set_name}_gate", source_name, "__predicate_value"
        )
        ctes.append(gate_cte)
        joins.append(gate_join)
        keep_rows.append(gate_condition)
    return ctes, joins, keep_rows
