"""A parent's value on its children's rows: a measure grouped at the grain of rows that name it.

"Each room with its building's area": the call groups by a room's columns and selects a measure
of the building each room row names by its own key. From the building's side that is a
one-to-many walk (refused where the relationship walks forward only) or a question of which
route reaches the rooms; from the room rows' side it is one lookup. So a call its own bind
refuses that way is bound once more (``bind_parent_values``), reading such a measure from the
rows (``parent_rows``): the entity of the call's first ``group_by`` (else ``where``) dimension
that reaches every entity the call groups or filters by in one row at every hop.

A measure of another entity M is read there (``parent_value_route``) when M has a key, the
measure counts each M row with its whole value (a sum, count, average or distinct count of an
additive, non-stock measure, with no filter, window, share or other entity's measure in its
item), no route from M to the rows is recorded (the package's or the call's own: that route is
the business's definition and stands), and the route from the rows to M is the call's own, the
package's recorded one or the rows' own key, one row at every hop. Its leaf
(``parent_value_leaf_select``) keeps one row per (M key, group) from the rows with every hop
joined LEFT, then aggregates: each M row counts once, with its whole value, in every group it
has a row in, and a group whose rows name no M row reads NULL. A total is the call without the
grouping. Anything else keeps the call's own refusal, except a policy's (``POLICY_DENIED``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from ..ast import NormalizedQuery, child_groups, plain_filters
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import ShareExpr
from ..fanout import analyze_fanout, entity_label, resolve_route, route_reading
from ..ir import BoundMeasure, LogicalPlan, MeasurePlan, PathSelection, RewriteStep
from ..schema import PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from .bind import (
    _aggregation_expr,
    _bound_filter_clauses,
    _bound_metric_predicates,
    _config_expr_to_sql,
    _measure_required_entities,
    is_conditional_aggregate,
)
from .indexes import _dimension_index, _entity_index, _measure_index
from .lowering_common import _edge_cap, _filter_condition_for_item
from .paths import (
    _column_ref,
    _direct_dimension_source_expr,
    _joins_for_paths,
    _resolve_dimension_expr,
)
from .plan_checks import (
    _WHOLE_GROUPED_AGGREGATIONS,
    _beside_another_entity,
    _measures_under_summing_wrappers,
    _query_roots,
)
from .temporal import _time_bound_relationship_ids

PARENT_VALUE = "parent_value"
# The refusals a call's own bind gives such a measure: no walk from M to the rows
# (``PATH_NOT_FOUND``), several routes from M (``AMBIGUOUS_PATH``), or one that crosses a
# one-to-many hop no other leaf answers (``MIXED_GRAIN_INVALID``).
_RETRIED = frozenset({"PATH_NOT_FOUND", "AMBIGUOUS_PATH", "MIXED_GRAIN_INVALID"})
# The route rungs that decide a pair without asking (``fanout.resolve_path``): the call's own
# row, the package's recorded row, the start's own key. Any other rung stays the package's.
_DECIDED = frozenset({"query", "decided", "colocated_key"})

_active: ContextVar[bool] = ContextVar("parent_values", default=False)
T = TypeVar("T")


def parent_values_active() -> bool:
    """Whether a call is being bound once more with parent values (``bind_parent_values``)."""
    return _active.get()


@contextmanager
def _binding() -> Iterator[None]:
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)


def bind_parent_values(
    payload: Mapping[str, Any], refusal: SemanticLayerError, bind: Callable[[], T]
) -> T | None:
    """The call bound once more with parent values (``bind``), when ``refusal`` is its own
    bind's refusal of a call that selects something; None when it isn't, or when that bind
    refuses too for any reason but a policy (the call's own refusal stands)."""
    if refusal.code not in _RETRIED or not payload.get("select"):
        return None
    with _binding():
        try:
            return bind()
        except SemanticLayerError as exc:
            if exc.code == "POLICY_DENIED":
                raise
    return None


def _looked_up(config: PackageConfig, query: NormalizedQuery, start: str, target: str) -> bool:
    """Whether ``start`` reaches ``target`` by the package's route in one row at every hop."""
    if start == target:
        return True
    try:
        route = list(resolve_route(config, start=start, target=target).routes[0])
    except SemanticLayerError:
        return False
    analysis = analyze_fanout(
        config, start, route, time_bound_relationships=_time_bound_relationship_ids(query, config)
    )
    return analysis.get("status") == "ok"


def parent_rows(
    query: NormalizedQuery, config: PackageConfig, bound_measures: list[BoundMeasure]
) -> str:
    """The entity whose rows read the call's parent values, or "" when the call has none: the
    first entity of its ``group_by`` dimensions (then its ``where`` fields') that may start a
    query and reaches every entity they read in one row at every hop, when a measure of
    another entity is read there (``parent_value_route``). A call with a time window, a result
    filter or an entity filter has none."""
    if query.time is not None or query.metric_filters or child_groups(query.where):
        return ""
    dimensions, entities = _dimension_index(config), _entity_index(config)
    fields = [*query.group_by, *(item.field for item in plain_filters(query.where))]
    if any(field not in dimensions for field in fields):
        return ""
    reads = list(dict.fromkeys(dimensions[field].entity for field in fields))
    for start in reads:
        entity = entities.get(start)
        if entity is None or not entity.allowed_as_root or entity.kind == "time":
            continue
        if all(_looked_up(config, query, start, target) for target in reads):
            if any(parent_value_route(config, query, start, row) for row in bound_measures):
                return start
            return ""
    return ""


def _recorded_from(config: PackageConfig, start: str, target: str) -> bool:
    """Whether the call or the package records a route from ``start`` to ``target``."""
    try:
        return resolve_route(config, start=start, target=target).basis in {"query", "decided"}
    except SemanticLayerError:
        return False


def parent_value_route(
    config: PackageConfig, query: NormalizedQuery, rows: str, bound: BoundMeasure
) -> list[str]:
    """The route from ``rows`` to the entity of ``bound``'s measure that reads its value on
    each row, or [] when the measure isn't read that way (see the module docstring)."""
    measure = _measure_index(config)[bound.measure_id]
    parent = _entity_index(config).get(measure.entity)
    aggregation = str(bound.aggregation or measure.default_aggregation or "").lower()
    roots = _query_roots(query)
    if (
        parent is None
        or parent.id == rows
        or parent.is_row_grain
        or not parent.key_columns
        or aggregation not in _WHOLE_GROUPED_AGGREGATIONS
        or not measure.additive
        or measure.measure_class in {"semi_additive", "snapshot"}
        or measure.lookup_from
        or is_conditional_aggregate(measure)
        or _measure_required_entities(measure, config) != {parent.id}
        or bound.window_spec
        or _bound_metric_predicates(bound)
        or _bound_filter_clauses(bound, config)
        or _edge_cap(measure, "", config)
        or _beside_another_entity(measure, config, query)
        or any(
            measure_id == bound.measure_id
            for root in roots
            for wrappers in ((), (ShareExpr,))
            for measure_id, _ in _measures_under_summing_wrappers(root, config, wrappers=wrappers)
        )
        or _recorded_from(config, parent.id, rows)
    ):
        return []
    try:
        resolution = resolve_route(config, start=rows, target=parent.id)
    except SemanticLayerError:
        return []
    route = list(resolution.routes[0])
    analysis = analyze_fanout(
        config, rows, route, time_bound_relationships=_time_bound_relationship_ids(query, config)
    )
    if resolution.basis not in _DECIDED or analysis.get("status") != "ok":
        return []
    return route


def parent_value_selections(
    config: PackageConfig, query: NormalizedQuery, rows: str, bound: BoundMeasure
) -> tuple[list[PathSelection], list[str]] | None:
    """The paths of ``bound``'s leaf read from ``rows`` (the route to its entity, then each
    entity the call groups or filters by), and the entities it reads; None when its measure
    isn't read there."""
    route = parent_value_route(config, query, rows, bound)
    if not route:
        return None
    parent = _measure_index(config)[bound.measure_id].entity
    time_bound = _time_bound_relationship_ids(query, config)
    selections = [
        PathSelection(
            target_entity=parent,
            purpose=PARENT_VALUE,
            chosen_path=route,
            candidate_paths=[route],
            analysis=analyze_fanout(config, rows, route, time_bound_relationships=time_bound),
        )
    ]
    dimensions = _dimension_index(config)
    reads = [
        *((field, "group_by") for field in query.group_by),
        *((item.field, "where") for item in plain_filters(query.where)),
    ]
    targets: dict[str, str] = {}
    for field, purpose in reads:
        target = dimensions[field].entity
        if (
            target not in {rows, parent}
            and _direct_dimension_source_expr(rows, field, config) is None
        ):
            targets.setdefault(target, purpose)
    for target, purpose in targets.items():
        chosen = list(resolve_route(config, start=rows, target=target).routes[0])
        selections.append(
            PathSelection(
                target_entity=target,
                purpose=purpose,
                chosen_path=chosen,
                candidate_paths=[chosen],
                analysis=analyze_fanout(config, rows, chosen, time_bound_relationships=time_bound),
            )
        )
    return selections, sorted({rows, parent, *targets})


def parent_value_step(
    config: PackageConfig, query: NormalizedQuery, rows: str, bound: BoundMeasure, route: list[str]
) -> RewriteStep:
    """The disclosure of a parent value: which rows read it, by which route, and that the column
    doesn't add up across groups; a total counts each parent once."""
    measure = _measure_index(config)[bound.measure_id]
    child, parent = entity_label(config, rows), entity_label(config, measure.entity)
    dimensions = _dimension_index(config)
    filtered = any(
        dimensions[item.field].entity != measure.entity for item in plain_filters(query.where)
    )
    population = "" if filtered else f", including any with no {child}"
    return RewriteStep(
        kind=PARENT_VALUE,
        status="applied",
        measure_id=bound.measure_id,
        reason=(
            f"Read on each {child} row from {route_reading(config, rows, route)}: each {parent} "
            f"counts once, with its whole value, in every group it has a {child} in, so the "
            f"column repeats on each of its {child} rows and doesn't add up across groups, and a "
            f"group whose {child} rows have no {parent} reads NULL. A total (totals, or a total "
            f"item) is the call without the grouping: it counts each {parent} once{population}."
        ),
        details={"rows_entity": rows, "path": list(route), "group_by": list(query.group_by)},
    )


def parent_value_leaf_select(
    plan: LogicalPlan, measure_plan: MeasurePlan, config: PackageConfig
) -> SqlSelect:
    """One row per (parent key, group) from the rows, every hop joined LEFT, then aggregated:
    each parent row counts once, with its whole value, in every group it has a row in, and a
    group whose rows name no parent reads NULL (its count of parent keys is 0)."""
    entities = _entity_index(config)
    bound = measure_plan.bound_measure
    measure = _measure_index(config)[bound.measure_id]
    rows, parent = measure_plan.source_entity, entities[measure.entity]
    keys = [
        SqlField(_column_ref(parent.table, column), f"__parent_key_{index + 1}")
        for index, column in enumerate(parent.key_columns)
    ]
    groups = [
        SqlField(
            *(
                _direct_dimension_source_expr(rows, dim_id, config)
                or _resolve_dimension_expr(dim_id, config)
            )
        )
        for dim_id in plan.group_by
    ]
    where = [
        _filter_condition_for_item(rows, item, config)
        for item in plain_filters(plan.query.get("where"))
    ]
    name = f"{measure_plan.cte_name}_parent_rows"
    reached = SqlSelect(
        select=[
            *keys,
            *groups,
            SqlField(_config_expr_to_sql(measure.expr, measure, config), "__parent_value"),
        ],
        from_table=SqlTableRef(name=entities[rows].table),
        joins=_joins_for_paths(rows, measure_plan.path_selections, config, measure_entity=None),
        where=where,
        distinct=True,
    )
    group_by: list[SqlExpr] = [SqlIdentifier(parts=[name, field.alias]) for field in groups]
    value = _aggregation_expr(
        SqlIdentifier(parts=[name, "__parent_value"]),
        str(bound.aggregation or measure.default_aggregation),
        parameters=bound.aggregation_params,
        dialect=dialect_for_warehouse(config.package.warehouse),
    )
    found = SqlCall("COUNT", [SqlIdentifier(parts=[name, keys[0].alias])])
    return SqlSelect(
        ctes=[SqlCte(name=name, query=reached)],
        select=[
            *(SqlField(expr, field.alias) for expr, field in zip(group_by, groups, strict=True)),
            SqlField(
                SqlCase([SqlCaseWhen(SqlBinary(found, ">", SqlLiteral(0)), value)]), bound.alias
            ),
        ],
        from_table=SqlTableRef(name=name),
        group_by=group_by,
    )
