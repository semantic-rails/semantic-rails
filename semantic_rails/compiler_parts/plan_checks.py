"""The checks a plan passes before it lowers: window shapes and every-period answers, sums that
can't add up across their rows, fan-out across one-to-many hops and child scopes, validity
windows, and a where value's type.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, fields, is_dataclass
from typing import Any

from .. import operators
from ..ast import (
    ChildGroup,
    Filter,
    NormalizedQuery,
    child_groups,
    normalize_query,
    plain_filters,
)
from ..clock import UNITS, known_zone
from ..config_parts.lookup_measures import lookup_links
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ConversionExpr,
    DistributionExpr,
    EntityValueExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    ScopedAggregateExpr,
    ShareExpr,
    expr_kind,
    expr_to_dict,
)
from ..fanout import (
    analyze_fanout,
    filter_only_semijoin,
    one_to_many_descent,
    resolve_path,
    route_pin,
)
from ..ir import (
    BoundMeasure,
    PathSelection,
    RewriteStep,
)
from ..naming import semantic_token as _semantic_token
from ..naming import slug as _slug
from ..query_call import call_where
from ..schema import (
    MeasureConfig,
    PackageConfig,
    require_base,
)
from ..temporal_support import _date_key
from .bind import (
    _bound_filter_clauses,
    _bound_metric_predicates,
    _measure_count_distinct_key_columns,
    is_conditional_aggregate,
)
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_has_source_row_key,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
)
from .lowering_common import _measure_owned_relation
from .paths import (
    _direct_dimension_source_expr,
    _direct_entity_key_source_expr,
    _entity_key_dimension_ids,
    _joins_for_paths,
    _leaf_time_role,
)
from .post_aggregation import (
    _as_offset_window_expr,
    _summing_window_parts,
)
from .temporal import (
    _expr_compatible_temporal_roles,
    _time_bound_relationship_ids,
    _window_measure_exprs,
    series_grain,
    window_total_refusal,
)


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
    query = normalize_query(
        {"version": 1, "select": [{"expression": {"metric": object_id}, "as": recipe.label}]}
    )
    leaf_roles = [
        _expr_compatible_temporal_roles(leaf, config, query)
        for leaf in _window_measure_exprs(recipe.expression, config)
    ]
    if leaf_roles and all(len(roles) == 1 for roles in leaf_roles):
        bound_roles = set.union(*leaf_roles)
        if len(bound_roles) == 1:
            return next(iter(bound_roles))
    if recipe.compatible_temporal_roles:
        return recipe.compatible_temporal_roles[0]
    compatible = sorted(_expr_compatible_temporal_roles(recipe.expression, config, query))
    return compatible[0] if compatible else ""


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


# A window adds periods and a per-entity rollup adds an entity's rows before comparing
# or ranking them, so neither may take a measure that must never be summed.
_SUMMING_WRAPPERS = (
    OffsetWindowExpr,
    EntityValueExpr,
    DistributionExpr,
    MetricPredicateExpr,
    ScopedAggregateExpr,
)


# Constructs whose result depends on a time axis, so a window without a grain can't collapse.
_TIME_AXIS_EXPRS = (*_SUMMING_WRAPPERS, ConversionExpr)


def _time_axis_kind(expr: Any, config: PackageConfig) -> str:
    """The kind of the first construct in ``expr`` that reads a time axis, else ``""``."""
    # An entity's aggregate over all its rows, or over the query window, reads no output time,
    # and neither does a scoped aggregate filtered only by such aggregates.
    if isinstance(expr, MetricPredicateExpr) and expr.scope_mode == "entity_only":
        return ""
    if (
        isinstance(expr, ScopedAggregateExpr)
        and not (expr.anchor or expr.window)
        and all(predicate.get("scope_mode") == "entity_only" for predicate in expr.predicates)
    ):
        return ""
    if isinstance(expr, _TIME_AXIS_EXPRS):
        return expr_kind(expr)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return _time_axis_kind(recipe.expression, config) if recipe is not None else ""
    if not is_dataclass(expr):
        return ""
    for item in fields(expr):
        value = getattr(expr, item.name)
        for child in value if isinstance(value, list) else [value]:
            if kind := _time_axis_kind(child, config):
                return kind
    return ""


def _reads_aggregates(query: NormalizedQuery) -> bool:
    """Whether the query reads an aggregate per period: in its select, or in a result filter
    (``having``) alone."""
    return bool(query.select) or any(
        item.expression is not None and not isinstance(item.expression, MetricPredicateExpr)
        for item in query.metric_filters
    )


def observed_periods_reason(query: NormalizedQuery, config: PackageConfig) -> str:
    """Why a named window under a grain answers only the periods that have rows ("" when it
    answers every period it holds, like ``fill``).

    Every period needs a day spine and the data's loaded edge, which bounds an empty period's
    0, on the warehouse, and a clock zone that names its days. Without them the series keeps
    the periods that have rows, so no period after the edge reads 0, and the answer says so.
    """
    time = query.time
    if not (time is not None and time.named and time.grain in UNITS and _reads_aggregates(query)):
        return ""
    dialect = dialect_for_warehouse(config.package.warehouse)
    if not (dialect.reads_coverage and dialect.has_implicit_calendar):
        return f"warehouse '{dialect.name}' can't list a window's empty periods"
    role = _temporal_role_index(config).get(time.temporal_role)
    zone = str(getattr(role, "timezone", "") or "UTC")
    return "" if known_zone(zone) else f"the clock's zone '{zone}' is not a known time zone"


def _answers_every_period(query: NormalizedQuery, config: PackageConfig) -> bool:
    """A named window under a grain answers one row per period it holds, like ``fill``."""
    time = query.time
    return bool(
        time is not None
        and time.named
        and time.grain in UNITS
        and _reads_aggregates(query)
        and not observed_periods_reason(query, config)
    )


def _time_valued_groups(query: NormalizedQuery, config: PackageConfig) -> list[str]:
    dimensions = _dimension_index(config)
    return [
        dim_id
        for dim_id in query.group_by
        if getattr(dimensions.get(dim_id), "data_type", "") in {"date", "timestamp"}
    ]


def observed_only_groups(query: NormalizedQuery, config: PackageConfig) -> list[str]:
    """The date and time group_by dimensions that keep a named window to its observed rows.

    Filling the window's periods across such a dimension would pad every observed instant
    into every period; the answer keeps the rows that hold data and says so.
    """
    return _time_valued_groups(query, config) if _answers_every_period(query, config) else []


def _window_total(
    query: NormalizedQuery, config: PackageConfig, bound_measures: Iterable[BoundMeasure]
) -> bool:
    """A time window with no grain answers one total per group, never a row per timestamp.

    A predicate taken over each entity's own rows (all time, or the query window) reads no
    output time, so the total takes it. One tested per output row (a package metric's
    contextual predicate) has no total over the window: lowering refuses the request, asking
    for a grain, once it has raised any refusal of its own. A select or result filter that
    reads a time axis (a running total, a comparison, a distribution) has none either, and is
    refused here.
    """
    time = query.time
    if time is None or not time.temporal_role or time.grain or time.fill:
        return False
    if time.start is None and time.end is None:
        return False
    predicates = [
        *(predicate for bound in bound_measures for predicate in _bound_metric_predicates(bound)),
        *(item.expression for item in query.metric_filters),
    ]
    if any(
        isinstance(predicate, MetricPredicateExpr) and predicate.scope_mode != "entity_only"
        for predicate in predicates
    ):
        return False  # lowering refuses it (``_refuse_per_period_total``)
    readers = [(f"'{item.as_}'", item.expression) for item in query.select] + [
        ("a result filter", item.expression) for item in query.metric_filters
    ]
    for reader, expr in readers:
        if expr is not None and (kind := _time_axis_kind(expr, config)):
            raise window_total_refusal(
                asdict(time),
                f"{reader} reads a {kind}, which is answered per period",
                grain=series_grain(expr, config),
                expression=expr_to_dict(expr),
            )
    return bool(query.select or query.group_by)


def _measures_under_summing_wrappers(
    expr: Any, config: PackageConfig, wrapper: str = ""
) -> Iterable[tuple[str, str]]:
    """(measure id, construct) for each measure a window or per-entity rollup reaches."""
    # A prior-period offset reads one other period's value; it adds nothing.
    offset = isinstance(expr, OffsetWindowExpr) and expr.aggregate in {"lag", "lead"}
    if isinstance(expr, _SUMMING_WRAPPERS) and not offset:
        wrapper = wrapper or expr_kind(expr)
    if wrapper and isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        yield expr.measure, wrapper
    if isinstance(expr, ScopedAggregateExpr):
        return  # its predicates reach the check through the bound measure's filter_spec
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is not None:
            yield from _measures_under_summing_wrappers(recipe.expression, config, wrapper)
        return
    if not is_dataclass(expr):
        return
    for item in fields(expr):
        value = getattr(expr, item.name)
        for child in value if isinstance(value, list) else [value]:
            if is_dataclass(child):
                yield from _measures_under_summing_wrappers(child, config, wrapper)


def _single_valued_columns(
    measure: MeasureConfig, bound: BoundMeasure, query: NormalizedQuery, config: PackageConfig
) -> set[str]:
    """Columns of the measure's own model that hold one value in every output row.

    A column counts only when the query groups by its dimension, or by the key
    dimension of the model a direct many-to-one relationship reaches through it, or
    pins that dimension with a top-level ``is`` filter of one value; and the
    measure's own ``date`` clock at ``grain: day``. Metric, measure and segment
    filters never count: they don't split the output rows.
    """
    if measure.lookup_from:
        child, source = lookup_links(measure, config)
        for link in (child, source):
            chosen, _ = resolve_path(config, start=link.source_entity, target=measure.lookup_via)
            if chosen != [link.id]:
                # A via key reached by another route cannot prove the leaf's direct FK fixed,
                # even if the child's own key is grouped. Recheck configs that bypassed load.
                _raise_non_additive_sum(
                    measure, "parent_lookup", child.source_columns or [child.source_column], config
                )
    dimensions = _dimension_index(config)
    pinned = set(query.group_by)
    for item in plain_filters(query.where):  # a child group pins no output row's value
        if operators.pinned_value(item.op, item.value)[0]:
            pinned.add(str(item.field))
    columns: set[str] = set()
    for dimension_id in pinned:
        dimension = dimensions.get(dimension_id)
        if dimension is None:
            continue
        if dimension.entity == measure.entity:
            columns.add(dimension.column)
            continue
        # The base path reads a target key through the first relationship between the two
        # entities, so credit it only when there is just one (no role-playing keys).
        links = [
            rel
            for rel in config.relationships
            if {rel.source_entity, rel.target_entity} == {measure.entity, dimension.entity}
        ]
        rel = links[0] if len(links) == 1 else None
        if (
            rel is not None
            and rel.source_entity == measure.entity
            and rel.cardinality in {"N:1", "1:1"}
            and list(rel.target_columns or [rel.target_column]) == [dimension.column]
        ):
            columns.update(rel.source_columns or [rel.source_column])
    role_id = _leaf_time_role(bound, query, config)
    role = _temporal_role_index(config).get(role_id) if role_id else None
    clock = dimensions.get(role.dimension) if role is not None else None
    if (
        clock is not None
        and query.time is not None
        and str(query.time.grain or "").lower() == "day"
        and clock.entity == measure.entity
        and clock.data_type == "date"
    ):
        columns.add(clock.column)
    return columns


def _validate_non_additive_sums(
    bound_measures: Iterable[BoundMeasure], config: PackageConfig, query: NormalizedQuery
) -> None:
    """Refuse unsafe sums of stored values or of post-aggregation window inputs.

    A flow sums its rows, and a stock sums its series' last (or first) snapshots, so each
    output row must hold one row (for a stock, one series) of the measure's model. Other
    aggregations (avg, min, max, median, percentile) are statistics of the values and
    stay allowed without a summing window. Windows and per-entity rollups over such
    a measure always refuse; summing windows also require additive aggregations.
    """
    measures = _measure_index(config)
    bound_measures = list(bound_measures)
    roots: list[Any] = [item.expression for item in query.select if item.expression is not None]
    roots += [item.expression for item in query.metric_filters if item.expression is not None]
    for bound in bound_measures:
        roots.extend(_bound_metric_predicates(bound))
    for bound in bound_measures:
        _refuse_time_on_lookup(measures[bound.measure_id], bound, roots, config, query)
    wrapped = {
        measure_id: construct
        for root in roots
        for measure_id, construct in _measures_under_summing_wrappers(root, config)
    }
    for measure_id, construct in wrapped.items():
        measure = measures.get(measure_id)
        if measure is not None and not measure.additive:
            _raise_non_additive_sum(measure, construct, [], config)

    def validate_window(expr: Any) -> None:
        if isinstance(expr, MetricRecipeRefExpr):
            recipe = _recipe_index(config).get(expr.metric_recipe)
            if recipe is not None:
                validate_window(recipe.expression)
            return
        if isinstance(expr, ShareExpr) and (
            len(_summing_window_parts(expr.input, config, construct="share")) != 1
        ):
            raise SemanticLayerError(
                "ROLLUP_UNSAFE",
                "A share's input must add up across the rows it shares; a ratio does not. "
                "Share its numerator, or ask for the ratio by group.",
                details={
                    "unsupported_construct": "non_additive_window_input",
                    "construct": "share",
                },
            )
        window = _as_offset_window_expr(expr)
        if window is not None and window.aggregate == "sum":
            _summing_window_parts(window.input, config, construct=window.kind)
            if window.kind == "period_to_date" and query.time is not None:
                role = _temporal_role_index(config).get(query.time.temporal_role)
                dim = _dimension_index(config).get(role.dimension) if role else None
                entity = _entity_index(config).get(dim.entity) if dim else None
                role_calendar = (
                    (entity.calendar_id or "default").strip().lower() if entity else "default"
                )
                if role_calendar != "default":
                    raise SemanticLayerError(
                        "REWRITE_NOT_SUPPORTED",
                        "period_to_date requires the default calendar; authored non-default "
                        "calendar period anchors are not supported. Query the fiscal period "
                        "as exact start/end dates without a period_to_date window.",
                        details={
                            "unsupported_construct": "non_default_calendar_period_to_date",
                            "calendar_id": role_calendar,
                        },
                    )
        if is_dataclass(expr):
            for item in fields(expr):
                value = getattr(expr, item.name)
                for child in value if isinstance(value, list) else [value]:
                    if is_dataclass(child):
                        validate_window(child)

    for root in roots:
        validate_window(root)
    for bound in bound_measures:
        measure = measures[bound.measure_id]
        if measure.additive:
            continue
        aggregation = str(bound.aggregation or measure.default_aggregation or "").lower()
        key = list(measure.row_grain or []) or list(_entity_index(config)[measure.entity].key or [])
        if measure.lookup_from:
            # One carried total per output row: its via foreign key, or its own row, is fixed.
            single_valued = _single_valued_columns(measure, bound, query, config)
            via_key = lookup_links(measure, config)[0]
            via_columns = via_key.source_columns or [via_key.source_column]
            if not (set(via_columns) <= single_valued or set(key) <= single_valued):
                _raise_non_additive_sum(measure, "parent_lookup", via_columns, config)
            continue
        if aggregation not in {"sum", "last_value", "first_value"}:
            continue
        required = key
        if measure.measure_class == "semi_additive":
            # A stock takes one snapshot per series per bucket, then sums the series.
            role = _temporal_role_index(config).get(bound.temporal_role)
            clock = _dimension_index(config).get(role.dimension) if role is not None else None
            required = [column for column in key if clock is None or column != clock.column]
        single_valued = _single_valued_columns(measure, bound, query, config)
        missing = [column for column in required if column not in single_valued]
        if missing:
            _raise_non_additive_sum(measure, aggregation, missing, config)


class NonAdditiveRefusal(SemanticLayerError):
    """``ROLLUP_UNSAFE`` for an ``additive: false`` measure summed above its stored grain.

    Key columns and dimensions stay available to in-process callers. Public
    diagnostics name dimensions only after checking the request's visibility.
    """

    def __init__(
        self, message: str, *, details: dict[str, Any], columns: list[str], dimensions: list[str]
    ) -> None:
        super().__init__("ROLLUP_UNSAFE", message, details=details)
        self.columns = columns
        self.dimensions = dimensions


def _raise_non_additive_sum(
    measure: MeasureConfig, construct: str, missing: list[str], config: PackageConfig
) -> None:
    dimensions = [
        dimension.id
        for dimension in config.dimensions
        if dimension.entity == measure.entity and dimension.column in missing
    ]
    if missing and measure.lookup_from:
        dimensions = _entity_key_dimension_ids(measure.lookup_via, config)
        hint = (
            f"Group by, or filter with = to one value, the key of '{measure.lookup_via}' (the "
            "lookup's via), or each column of the measure's own key."
        )
        message = (
            f"Measure '{measure.id}' carries one total per '{measure.lookup_via}' onto each of "
            f"its rows, and this query would add more than one of them into an output row. {hint}"
        )
    elif missing:
        key = "series key" if measure.measure_class == "semi_additive" else "key"
        statistics = [
            name for name in ("avg", "min", "max", "median") if name in measure.allowed_aggregations
        ]
        instead = f", or use aggregation {' / '.join(statistics)}" if statistics else ""
        message = (
            f"Measure '{measure.id}' is additive: false, and this query would sum more than one "
            f"of its rows into an output row: group by or filter (=) each column of its {key}, "
            f"or query a finer grain{instead}."
        )
        hint = (
            f"Group by, or filter with = to one value, each column of the measure's {key}{instead}."
        )
    else:
        message = (
            f"Measure '{measure.id}' is additive: false, so it can't feed a {construct}: that "
            "adds its values across periods or rows."
        )
        hint = "Query the measure itself at the grain it's stored at."
    raise NonAdditiveRefusal(
        message,
        details={
            "measure_id": measure.id,
            "unsupported_construct": "non_additive_sum",
            "construct": construct,
            "recovery_hints": [{"kind": "stay_at_stored_grain", "message": hint}],
        },
        columns=missing,
        dimensions=dimensions,
    )


# A lookup carries its source's all-time total, so no time axis may bucket, shift or add it.
_LOOKUP_TIME_EXPRS = (OffsetWindowExpr, ConversionExpr)


def _refuse_time_on_lookup(
    measure: MeasureConfig,
    bound: BoundMeasure,
    roots: list[Any],
    config: PackageConfig,
    query: NormalizedQuery,
) -> None:
    """A query's time selects a lookup's own rows by its own clock, and does nothing else."""
    if not measure.lookup_from:
        return
    requested = query.time.temporal_role if query.time is not None else ""
    reason = ""
    if requested and _leaf_time_role(bound, query, config) != requested:
        reason = f"time '{requested}' is not a clock of the measure's own rows"
    elif measure.id in query.temporal_role_overrides:
        reason = "a temporal role override would retime it"
    elif any(measure.id in _measures_inside(root, _LOOKUP_TIME_EXPRS, config) for root in roots):
        reason = "a window, prior period or conversion would bucket, shift or add it over time"
    if reason:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Measure '{measure.id}' carries its source's all-time total per "
            f"'{measure.lookup_via}'; {reason}. Filter or bucket by a clock of its own rows, "
            "which selects the rows and leaves the carried total unchanged.",
            details={"measure_id": measure.id, "unsupported_construct": "lookup_time"},
        )


def _measures_inside(
    expr: Any, kinds: tuple[type, ...], config: PackageConfig, inside: bool = False
) -> set[str]:
    """Measures read anywhere under a construct of ``kinds``, recipes expanded."""
    inside = inside or isinstance(expr, kinds)
    found: set[str] = set()
    if inside and isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        found.add(expr.measure)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        return found | (
            _measures_inside(recipe.expression, kinds, config, inside) if recipe else set()
        )
    if is_dataclass(expr):
        for item in fields(expr):
            value = getattr(expr, item.name)
            for child in value if isinstance(value, list) else [value]:
                if is_dataclass(child):
                    found |= _measures_inside(child, kinds, config, inside)
    return found


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


# Query roles a de-duplicated leaf serves across a one-to-many hop, and the aggregations it
# answers: any one-value-per-row aggregation under a filter, and only a distinct count when the
# child dimension splits the output ("orders that included it").
_FANOUT_DEDUP_PURPOSES = frozenset({"group_by", "where", "metric_filter", "child_group"})


_FANOUT_DEDUP_AGGREGATIONS = frozenset(
    {"sum", "count", "count_distinct", "avg", "min", "max", "median", "percentile"}
)


_FANOUT_GROUPED_AGGREGATIONS = frozenset({"count_distinct"})


def _negated_filter(item: dict[str, Any], dim: Any) -> bool:
    """A filter whose EXISTS reading ("has a row that is not X") differs from "has no row that
    is X": ``not`` or a ``not_`` text word, ``missing: true``, or on a boolean dimension anything
    but ``is true``."""
    op, value = operators.normalize_condition(
        item.get("op", "is"), item.get("value"), path="where", internal=True
    )
    if dim.data_type == "boolean":
        return op != "is" or not all(
            value is True or str(value).lower() == "true"
            for value in operators.values_of(op, value)
        )
    if op == "missing":
        return bool(value)  # "has a row with a value" reads one way
    return op in operators.NEGATED_WORDS


def _fanout_dedup_refusal(
    *,
    measure: MeasureConfig,
    bound: BoundMeasure,
    selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> tuple[str, PathSelection] | None:
    """Why this leaf can't count each measure row once across its one-to-many hops, and the
    path at fault; None when it can.

    ``selections`` are all the leaf's paths. Filter-only paths use correlated
    EXISTS (or a de-duplicated parent leaf on ClickHouse). Grouped paths keep one row per
    (measure-entity key, output grain) before aggregation, so each row counts once in every
    group it has a matching child in.

    Grouped, only a distinct count is answered. Summing (or averaging) an order amount by an
    item dimension reads as the item-level split ("revenue by product type") as often as the
    orders-that-included-it total, and the two differ, so that shape stays refused.

    Every leaf is checked here: an authored measure, a query's ``aggregate_if`` (a synthetic
    measure over its entity's table) and each operand of a ratio or arithmetic, so each one
    gets the same rewrite and the same refusals.
    """
    subject = (
        f"an aggregate_if over '{measure.entity}'"
        if is_conditional_aggregate(measure)
        else f"measure '{measure.id}'"
    )
    selections = [row for row in selections if row.analysis.get("status") != "ok"]
    for row in selections:
        if row.purpose not in _FANOUT_DEDUP_PURPOSES:
            return (
                f"A {row.purpose} path from '{measure.entity}' to '{row.target_entity}' crosses "
                "a one-to-many hop; only filters and distinct-count groupings can.",
                row,
            )
    grouped = [row for row in selections if row.purpose == "group_by"]
    groups = child_groups(query.where)
    if groups and grouped:
        return (
            f"A child group filters '{measure.entity}' rows by their child rows, and grouping "
            f"the same leaf by a dimension of '{grouped[0].target_entity}' splits them by child "
            "rows too; the two together are not supported. Group by a dimension of the "
            "measure's own grain.",
            grouped[0],
        )
    keys = {entity.id: list(entity.key or [entity.primary_key]) for entity in config.entities}
    dialect = dialect_for_warehouse(config.package.warehouse)
    correlated_exists = dialect.correlated_exists
    for row in selections:
        # Preserve existing descent shapes, including the DISTINCT-parent leaf beside lookups
        # of a warehouse without correlated EXISTS. A broader EXISTS path is the route the
        # resolver chose for the pair (a recorded decision, the only route, or one narrowed by
        # the rows), never one picked by hop count, so it can say which children are meant.
        if one_to_many_descent(row.analysis, keys):
            continue
        if not grouped and correlated_exists and filter_only_semijoin(row.analysis):
            continue
        return (
            (
                ""
                if correlated_exists
                else f"{dialect.label} has no correlated EXISTS, so it needs a key-based descent "
                "before any lookup. "
            )
            + f"The path from '{measure.entity}' to '{row.target_entity}' is many-to-many (a "
            "lookup before the one-to-many hop, an M:N or time-bounded relationship, or a "
            "join off the declared key), so no single set of rows belongs to each row.",
            row,
        )
    aggregation = str(bound.aggregation or measure.default_aggregation or "").lower()
    if grouped and aggregation not in _FANOUT_GROUPED_AGGREGATIONS:
        return (
            f"'{aggregation}' of {subject} grouped by a dimension of "
            f"'{grouped[0].target_entity}' is ambiguous across a one-to-many hop: the amount "
            "split over the child rows and the full amount of every row that has one differ. "
            "Use a measure at the child's grain, group by a dimension of the measure's own "
            "grain, or filter on the child dimension (each row then counts once).",
            grouped[0],
        )
    entity = _entity_index(config)[measure.entity]
    if aggregation not in _FANOUT_DEDUP_AGGREGATIONS or not (
        measure.additive
        and measure.measure_class not in {"semi_additive", "snapshot"}
        and _measure_has_source_row_key(measure, entity)
        and (entity.key or entity.primary_key)
    ):
        return (
            f"'{aggregation}' of {subject} is not defined over one row per "
            f"'{measure.entity}' key (a non-additive value, a stock, an ordered aggregation, "
            "or a measure whose rows are finer than its entity's key).",
            (grouped or selections)[0],
        )
    # Without correlated EXISTS, the leaf keeps one row per key before it aggregates. An
    # aggregate_if's rows are one per key only when a measure of its model says so; without
    # one, rows sharing a key could merge, which only a distinct count, a minimum or a maximum
    # ignores.
    if (
        is_conditional_aggregate(measure)
        and not measure.row_grain
        and not correlated_exists
        and aggregation not in {"count_distinct", "min", "max"}
    ):
        return (
            f"{dialect.label} keeps one row per '{measure.entity}' key here, and no "
            f"measure of its model declares the grain of its rows, so '{aggregation}' of "
            f"{subject} could merge rows that share a key. Use count_distinct, min or max, or "
            "declare a measure on that model.",
            (grouped or selections)[0],
        )
    roots: list[Any] = [item.expression for item in query.select if item.expression is not None]
    roots += [item.expression for item in query.metric_filters if item.expression is not None]
    if (
        bound.window_spec
        or _bound_metric_predicates(bound)
        or any(isinstance(root, MetricPredicateExpr) for root in roots)
        or any(
            measure_id == bound.measure_id
            for root in roots
            for measure_id, _ in _measures_under_summing_wrappers(root, config)
        )
    ):
        return (
            "A window, per-entity rollup, distribution or metric predicate over this measure "
            "is not supported across a one-to-many hop.",
            (grouped or selections)[0],
        )
    conditions = _hop_conditions(bound, selections, config, query)
    if groups:
        refusal = _second_child_scope(selections, conditions)
        if refusal is not None:
            return refusal
    elif all(hop.purpose == "where" for _, _, hop in conditions):
        found = _ambiguous_child_scope(measure, selections, config, query)
        if found is not None:
            ambiguity, hop = found
            if isinstance(ambiguity, str):
                return ambiguity, hop
            raise ambiguity
    if len(conditions) > 1:
        return _two_hop_conditions(conditions)
    for dim, item, hop in conditions:
        if item and _negated_filter(item, dim):
            return (
                f"A negated or null test on '{dim.id}' across a one-to-many hop is ambiguous: "
                "'has a row that is not X' and 'has no row that is X' differ. Filter on the "
                "values to keep instead.",
                hop,
            )
    if groups and not correlated_exists:
        return _deduplicated_child_group_refusal(measure, selections, config, query)
    return None


def _second_child_scope(
    selections: list[PathSelection],
    conditions: list[tuple[Any, dict[str, Any], PathSelection]],
) -> tuple[str, PathSelection] | None:
    """One child scope per query: beside a child group, nothing else may cross a one-to-many
    hop, and every group must reach the same child by the same route. Otherwise a condition
    may have to hold under the group's parent row or under any, and no group says which."""
    routes = [row for row in selections if row.purpose == "child_group"]
    if conditions:
        dim, _, hop = conditions[0]
        other = f"'{dim.id}'"
    elif len({(row.target_entity, tuple(row.chosen_path)) for row in routes}) > 1:
        hop = next(row for row in routes if row.target_entity != routes[0].target_entity)
        other = f"a child group on '{hop.target_entity}'"
    else:
        return None
    return (
        f"A child group on '{routes[0].target_entity}' and {other} both cross a one-to-many hop, "
        "and nothing says which rows they must hold on together: one row, rows under one "
        "parent row, or any. Ask one child scope per query: state every condition on that "
        "child in its groups.",
        hop,
    )


def _deduplicated_child_group_refusal(
    measure: MeasureConfig,
    selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> tuple[str, PathSelection] | None:
    reason = _deduplicated_child_group_reason(measure, config, query)
    if not reason:
        return None
    route = next(row for row in selections if row.purpose == "child_group")
    label = dialect_for_warehouse(config.package.warehouse).label
    return (f"{label} cannot answer this child group: {reason}.", route)


def _deduplicated_child_group_reason(
    measure: MeasureConfig, config: PackageConfig, query: NormalizedQuery
) -> str:
    """A warehouse without correlated EXISTS answers one ``any`` group on the child's own
    columns with its de-duplicated parent leaf. A ``none`` group would need an anti-join whose
    NULL handling is unproven there."""
    groups = child_groups(query.where)
    if any(group.match == "none" for group in groups):
        return "a 'none' child group needs a NULL-safe anti-join, which is unproven there"
    if len(groups) > 1:
        return "it answers one child group, as the only condition across a one-to-many hop"
    if any(child_group_route(config, query, measure, groups[0])[1]):
        return "a child group's conditions must be on the child's own columns there"
    return ""


def _child_scope(start_entity: str, selection: PathSelection) -> tuple[str, tuple[str, ...]]:
    """The child a flat filter's path reaches across its last one-to-many hop, and the route
    there; lookups after that hop read one row of that child."""
    entity, child, route = start_entity, start_entity, 0
    for index, row in enumerate(selection.analysis.get("relationships", []) or []):
        forward = row.get("traversal") == "forward"
        entity = str(row.get("target_entity" if forward else "source_entity", ""))
        if row.get("directional_safety") == "requires_rewrite":
            child, route = entity, index + 1
    return child, tuple(selection.chosen_path[:route])


# A flat filter reaching a child: its where index, the filter, its dimension, its child scope
# (the child, and the route there) and its path.
_FlatFilter = tuple[int, Filter, Any, tuple[str, tuple[str, ...]], PathSelection]


def _ambiguous_child_scope(
    measure: MeasureConfig,
    selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> tuple[SemanticLayerError | str, PathSelection] | None:
    """AMBIGUOUS_CHILD_SCOPE when the query's flat filters leave a child row's scope unsaid,
    with the path of the first; None leaves the query to the refusals it always had.

    Called for a query with no child group whose only conditions across a one-to-many hop are
    flat filters. They must all reach one child by one route, in one of two shapes: two or
    more filters, none negated ("an item that is a beverage and costs over 5": ``same_row`` or
    ``separate_rows``), or one negated filter with an exact complement ("an item that is not a
    beverage": ``any_not`` or ``none``). Each option is the call's whole rewritten ``where``. When a group would not follow a filter's own route, the
    reason (``_route_change``) comes back instead of a question.
    """
    dimensions = _dimension_index(config)
    crossing = {row.target_entity: row for row in selections if row.purpose == "where"}
    flat: list[_FlatFilter] = []
    for index, item in enumerate(query.where):
        if isinstance(item, ChildGroup):
            return None
        dim = dimensions[item.field]
        hop = crossing.get(dim.entity)
        if hop is not None:
            flat.append((index, item, dim, _child_scope(measure.entity, hop), hop))
    negated = [row for row in flat if _negated_filter(asdict(row[1]), row[2])]
    shaped = (len(flat) > 1 and not negated) or (len(flat) == 1 and bool(negated))
    if (
        not shaped
        or len({row[3] for row in flat}) > 1
        or (negated and operators.exact_complement(flat[0][1].op, flat[0][1].value) is None)
    ):
        return None
    scope, hop = flat[0][3], flat[0][4]
    moved = _route_change(measure, config, query, scope, flat)
    if moved:
        return moved, hop
    return _child_scope_clarification(scope[0], flat, config, query), hop


def _route_change(
    measure: MeasureConfig,
    config: PackageConfig,
    query: NormalizedQuery,
    scope: tuple[str, tuple[str, ...]],
    flat: list[_FlatFilter],
) -> str:
    """Why no child group can restate these flat filters: a group reaches its child, and a
    condition's lookup from it, by the routes ``child_group_route`` resolves, and one of the
    filters' own paths takes another. Empty when every route matches; a group's other
    refusals are left to the check of each option in ``bind_query``."""
    child, prefix = scope
    for _, item, _, _, hop in flat:
        wanted = {(measure.entity, child): list(prefix)}
        if len(hop.chosen_path) > len(prefix):
            wanted[(child, hop.target_entity)] = list(hop.chosen_path[len(prefix) :])
        try:
            route, [lookup] = child_group_route(
                config, query, measure, ChildGroup(child=child, match="any", where=[item])
            )
        except SemanticLayerError as exc:
            if exc.code != "AMBIGUOUS_PATH":
                continue
            found: dict[tuple[str, str], list[str]] = {
                (str(exc.details.get("start")), str(exc.details.get("target"))): []
            }
        else:
            found = {(measure.entity, child): list(route.chosen_path)}
            if lookup is not None:
                found[(child, lookup.target_entity)] = list(lookup.chosen_path)
        for (start, target), path in found.items():
            own = wanted.get((start, target))
            if own is not None and path != own:
                taken = f"by {' -> '.join(path)}" if path else "by no recorded route"
                return (
                    f"Each reading of these filters on '{child}' is a child group, and a group "
                    f"would reach '{target}' from '{start}' {taken}, not by {' -> '.join(own)} "
                    f"as '{item.field}' does. Record that route as a graph.path_preferences "
                    f"row to be asked which reading is meant: "
                    f"{json.dumps(route_pin(start, target, own))}"
                )
    return ""


def _rewritten_where(
    query: NormalizedQuery, replace_at: dict[int, dict[str, Any]], drop: set[int]
) -> list[dict[str, Any]]:
    """The call's whole ``where`` with groups in place of flat filters: ready to send."""
    return call_where(
        [
            replace_at.get(index, asdict(item))
            for index, item in enumerate(query.where)
            if index not in drop
        ],
        query.to_dict()["metric_filters"],
    )


def _group_payload(child: str, match: str, conditions: list[Filter]) -> dict[str, Any]:
    return {"child": child, "match": match, "where": [asdict(item) for item in conditions]}


def _condition_text(item: Filter, dim: Any) -> str:
    return operators.describe(dim.label or dim.id, item.op, item.value)


def _scope_error(
    message: str, child: str, paths: list[int], question: str, options: list[dict[str, Any]]
) -> SemanticLayerError:
    return SemanticLayerError(
        "AMBIGUOUS_CHILD_SCOPE",
        message,
        details={
            "child_entity": child,
            "paths": [f"where[{index}]" for index in sorted(paths)],
            "why_invalid": question,
            "clarification": {
                "kind": "child_scope",
                "apply": ["query"],
                "question": question,
                "options": options,
            },
            "recovery_hints": [
                {
                    "kind": "choose_child_scope",
                    "message": f"{option['meaning']} Resend the query with this option's where.",
                    "option": option["id"],
                    "suggested_query_ir_change": {"where": option["where"]},
                }
                for option in options
            ],
        },
    )


def _child_scope_clarification(
    child: str, flat: list[_FlatFilter], config: PackageConfig, query: NormalizedQuery
) -> SemanticLayerError:
    """The two readings of flat filters on one child scope, each as the query's whole
    rewritten where: two or more filters together on one row or each on its own, or one
    negated filter on some row that fails it or on no row that meets its complement."""
    label = _entity_index(config)[child].label or child
    positions = [index for index, *_ in flat]
    texts = [_condition_text(item, dim) for _, item, dim, *_ in flat]
    separate = _rewritten_where(
        query, {index: _group_payload(child, "any", [item]) for index, item, *_ in flat}, set()
    )
    if len(flat) > 1:
        conditions = [item for _, item, *_ in flat]
        together = _group_payload(child, "any", conditions)
        options = [
            {
                "id": "same_row",
                "meaning": f"One {label} meets {' and '.join(texts)}.",
                "where": _rewritten_where(query, {positions[0]: together}, set(positions[1:])),
            },
            {
                "id": "separate_rows",
                "meaning": f"Each of {', '.join(texts)} may hold on a different {label}.",
                "where": separate,
            },
        ]
        question = f"Do {' and '.join(texts)} apply to the same {label} or to separate ones?"
        message = (
            f"Filters on '{child}' cross a one-to-many hop without saying whether one {label} "
            "must meet them together."
        )
    else:
        [(index, item, dim, *_)] = flat
        complement = operators.exact_complement(item.op, item.value)
        assert complement is not None  # _ambiguous_child_scope asks only with one
        positive = Filter(field=item.field, op=complement[0], value=complement[1])
        options = [
            {"id": "any_not", "meaning": f"Has a {label} where {texts[0]}.", "where": separate},
            {
                "id": "none",
                "meaning": f"Has no {label} where {_condition_text(positive, dim)}.",
                "where": _rewritten_where(
                    query, {index: _group_payload(child, "none", [positive])}, set()
                ),
            },
        ]
        question = (
            f"Does '{texts[0]}' mean some {label} fails the condition, or no {label} meets its "
            "opposite?"
        )
        message = (
            f"A negated or null test on '{dim.id}' across a one-to-many hop may mean a {label} "
            f"that fails it or no {label} that meets its opposite."
        )
    return _scope_error(message, child, positions, question, options)


def _hop_conditions(
    bound: BoundMeasure,
    selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> list[tuple[Any, dict[str, Any], PathSelection]]:
    """Each group or filter of the leaf whose path crosses a one-to-many hop: its dimension,
    the filter clause (empty for a group) and the path."""
    crossing = {(row.target_entity, row.purpose): row for row in selections}
    dimensions = _dimension_index(config)
    conditions: list[tuple[Any, dict[str, Any], PathSelection]] = []
    candidates: list[tuple[str, dict[str, Any], str]] = [
        (dim_id, {}, "group_by") for dim_id in query.group_by
    ]
    # A child group says its own scope, and nothing else may cross beside it
    # (_second_child_scope); flat filters on one child may be asked about (_ambiguous_child_scope).
    candidates += [(row.field, asdict(row), "where") for row in plain_filters(query.where)]
    candidates += [
        (str(clause.get("field", "")), clause, "metric_filter")
        for clause in _bound_filter_clauses(bound, config)
    ]
    for dim_id, item, purpose in candidates:
        dim = dimensions.get(dim_id)
        hop = crossing.get((dim.entity, purpose)) if dim is not None else None
        if hop is not None:
            conditions.append((dim, item, hop))
    return conditions


def _two_hop_conditions(
    conditions: list[tuple[Any, dict[str, Any], PathSelection]],
) -> tuple[str, PathSelection]:
    """At most one group or filter may cross a one-to-many hop: with two, one child row may have
    to meet both, or any rows each, and which depends on where their paths part."""
    (first, _, _), (second, _, hop) = conditions[:2]
    return (
        f"'{first.id}' and '{second.id}' both cross a one-to-many hop, so they may have to "
        "match one row or any rows each. Ask one such condition per query, or combine its "
        "values with IN on one dimension.",
        hop,
    )


def _hop_steps(
    crossing: str,
    bound: BoundMeasure,
    path_selections: list[PathSelection],
    config: PackageConfig,
    query: NormalizedQuery,
) -> list[RewriteStep]:
    """Disclose how a leaf crosses its one-to-many hops, so REWRITE_APPLIED reports it."""
    measure = _measure_index(config)[bound.measure_id]
    entity = measure.entity
    if measure.lookup_from:
        child, source = lookup_links(measure, config)
        return [
            RewriteStep(
                kind="parent_lookup",
                status="applied",
                measure_id=bound.measure_id,
                reason=(
                    f"'{measure.lookup_from}' is totalled per '{measure.lookup_via}' over all "
                    f"time and repeated on each '{entity}' row of it, never added across two; "
                    "the query's filters and time select those rows only."
                ),
                details={
                    "from": measure.lookup_from,
                    "via": measure.lookup_via,
                    "path": [child.id],
                    "source_path": [source.id],
                },
            )
        ]
    crossed = {
        row.target_entity: list(row.chosen_path)
        for row in path_selections
        if row.analysis.get("status") != "ok"
    }
    if crossing == "entity_in_terms_of":
        return [
            RewriteStep(
                kind="entity_in_terms_of",
                status="applied",
                measure_id=bound.measure_id,
                reason="Count-distinct parent entity is computed from the qualifying child relation and rolled up by the requested child dimension.",
                details={
                    "target_entity": target,
                    "path": path,
                    "omits_semantic_root_entity": entity,
                },
            )
            for target, path in crossed.items()
        ]
    if crossing != "fanout_dedup":
        return []
    none = any(group.match == "none" for group in child_groups(query.where))
    return [
        RewriteStep(
            kind="fanout_dedup",
            status="applied",
            measure_id=bound.measure_id,
            reason=(
                f"Each '{entity}' counts once per group across the one-to-many hop to "
                f"{', '.join(repr(target) for target in sorted(crossed))}; a filter there means "
                "it has at least one matching row"
                + ("; a 'none' child group means it has none." if none else ".")
            ),
            details={"paths": crossed},
        )
    ]


def _range_crosses_window_boundary(
    *, start: str, end: str, window_from: str, window_to: str
) -> bool:
    if window_from and start and start < window_from and (not end or end > window_from):
        return True
    return bool(window_to and (not start or start < window_to) and end and end > window_to)


def _validate_measure_validity_windows(
    bound_measures: Iterable[BoundMeasure],
    config: PackageConfig,
    query: NormalizedQuery,
    display_config: PackageConfig | None = None,
) -> None:
    require_base(config)
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
        display = get_package_analysis(display_config or config).measures[measure.id]
        for index, window in enumerate(measure.validity_windows):
            if _range_crosses_window_boundary(
                start=start,
                end=end,
                window_from=_date_key(window.from_),
                window_to=_date_key(window.to),
            ):
                window = display.validity_windows[index]
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


_NUMERIC_DATA_TYPES = {"integer", "number"}


def _validate_where_value_type(dim, item, path: str = "where") -> None:
    """Catch common where-value type mismatches before warehouse execution.

    Skip dimensions without a classified catalog data type.
    """
    unknown = [
        value
        for value in operators.values_of(item.op, item.value)
        if value in list(getattr(dim, "unknown_values", None) or [])
    ]
    if unknown:
        # The dimension reads that spelling as unknown, so is / not never sees it.
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{path}.value: {unknown[0]!r} is how {dim.id} records an unknown value",
            details={
                "path": f"{path}.value",
                "unknown_values": list(dim.unknown_values),
                "recovery_hints": [
                    {
                        "code": "FIX_QUERY_CALL",
                        "message": (
                            f'Say missing: {{"field": "{dim.id}", "op": "missing", "value": '
                            f"{'true' if item.op == 'is' else 'false'}}}."
                        ),
                    }
                ],
            },
        )
    # ``missing`` takes a boolean, not a value of the dimension; the text words take text.
    if item.op == "missing" or item.op in operators.TEXT_WORDS:
        return
    if str(getattr(dim, "data_type", "") or "").lower() == "boolean" and item.op in (
        operators.RANGE_WORDS
    ):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{path}.op: a boolean dimension compares with is or not",
            details={
                "path": f"{path}.op",
                "dimension": dim.id,
                "op": item.op,
                "recovery_hints": [
                    {"code": "USE_FILTER_WORD", "message": "Write is true or is false."}
                ],
            },
        )
    values = item.value if isinstance(item.value, list) else [item.value]
    data_type = str(getattr(dim, "data_type", "") or "").lower()
    if not data_type:
        return

    def mismatch(expected: str, value: Any) -> SemanticLayerError:
        return SemanticLayerError(
            "INVALID_QUERY",
            (
                f"where filter on dimension {dim.id!r} expects a {expected} value but "
                f"got {value!r} (type {type(value).__name__})."
            ),
            details={"dimension": dim.id, "data_type": data_type, "value": value, "op": item.op},
        )

    for value in values:
        if value is None:
            continue
        if data_type in _NUMERIC_DATA_TYPES:
            if isinstance(value, (bool, int, float)):
                continue
            # A digit-string is acceptable; warehouses cast it cleanly.
            if isinstance(value, str) and value.strip().lstrip("-").replace(".", "", 1).isdigit():
                continue
            raise mismatch("numeric", value)
        if data_type == "string" and isinstance(value, (bool, int, float)):
            raise mismatch("string", value)
        if data_type == "boolean":
            if isinstance(value, bool):
                continue
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                continue
            raise mismatch("boolean", value)


def _group_path(query: Any, group: ChildGroup) -> str:
    """Where a child group sits in the call: ``where[i].entity``."""
    where = list(getattr(query, "where", None) or [])
    return f"where[{where.index(group)}].entity" if group in where else "where"


def child_group_route(
    config: PackageConfig, query: Any, measure: Any, group: ChildGroup
) -> tuple[PathSelection, list[PathSelection | None]]:
    """The route from ``measure``'s entity to a group's child, and each condition's lookup from
    the child (None for a value the child's row holds).

    Planning and lowering both read a group through this one function. The child must sit
    across a one-to-many hop: otherwise a plain where states the condition. A route tie, or a
    route other than a key-based descent beside another candidate, is ambiguous. Each
    condition is on the child or a declared many-to-one lookup from it, where a missing row
    reads NULL. No table a condition reads may be one the route reads: its name would bind
    to the lookup inside EXISTS and part the subquery from the row it belongs to.
    """
    entities = _entity_index(config)
    dimensions = _dimension_index(config)
    start = measure.entity
    time_bound = _time_bound_relationship_ids(query, config)

    def path(source: str, target: str, purpose: str) -> PathSelection:
        chosen, candidates = resolve_path(config, start=source, target=target)
        return PathSelection(
            target_entity=target,
            purpose=purpose,
            chosen_path=list(chosen),
            candidate_paths=[list(candidate) for candidate in candidates],
            analysis=analyze_fanout(config, source, chosen, time_bound_relationships=time_bound),
        )

    def invalid(message: str, **details: Any) -> SemanticLayerError:
        return SemanticLayerError(
            "INVALID_QUERY",
            message,
            details={"path": _group_path(query, group), "child": group.child, **details},
        )

    if group.child == start:
        raise invalid(
            f"'{group.child}' is the entity of measure '{measure.id}', not a child of it.",
            why_invalid="Filter the measure's own rows with a plain where item.",
        )
    route = path(start, group.child, "child_group")
    if route.analysis.get("status") == "ok":
        raise invalid(
            f"'{start}' reaches '{group.child}' only through lookups, so each row has at most "
            "one child row.",
            why_invalid="A child group is for rows across a one-to-many hop; use a plain where.",
            route=route.chosen_path,
        )
    keys = {entity.id: list(entity.key or [entity.primary_key]) for entity in config.entities}
    if len(route.candidate_paths) > 1 and not one_to_many_descent(route.analysis, keys):
        raise SemanticLayerError(
            "AMBIGUOUS_PATH",
            f"Ambiguous path from '{start}' to '{group.child}': "
            + "; ".join(" -> ".join(candidate) for candidate in route.candidate_paths),
            details={
                "start": start,
                "target": group.child,
                "candidates": route.candidate_paths,
                "hint": (
                    "These routes reach different child rows. Pin the one the question means "
                    "with graph.path_preferences (source_entity, target_entity, "
                    "relationship_path)."
                ),
            },
        )
    lookups: list[PathSelection | None] = []
    for condition in group.where:
        dim = dimensions[condition.field]
        if (
            dim.entity == group.child
            or _direct_dimension_source_expr(group.child, condition.field, config) is not None
        ):
            lookups.append(None)
            continue
        lookup = path(group.child, dim.entity, "child_group_lookup")
        if lookup.analysis.get("status") != "ok" or not filter_only_semijoin(lookup.analysis):
            raise invalid(
                f"'{condition.field}' is not a dimension of '{group.child}' or of a declared "
                "many-to-one lookup from it.",
                why_invalid=(
                    "Every condition in a child group is about one child row: its own "
                    "dimensions, or one row each of its lookups."
                ),
                dimension=condition.field,
                route=lookup.chosen_path,
            )
        lookups.append(lookup)
    route_tables = {_measure_owned_relation(measure, entities)} | {
        join.table.name for join in _joins_for_paths(start, [route], config, measure_entity=None)
    }
    lookup_tables = {
        join.table.name
        for join in _joins_for_paths(
            group.child, [row for row in lookups if row], config, measure_entity=None
        )
    }
    clash = sorted(route_tables & lookup_tables)
    if clash:
        raise invalid(
            f"A condition of the child group reads '{clash[0]}', which its route to "
            f"'{group.child}' already reads.",
            why_invalid=(
                "Inside the group, that table would be the condition's row, not the route's. "
                "Filter the parent with a plain where item."
            ),
            tables=clash,
        )
    return route, lookups
