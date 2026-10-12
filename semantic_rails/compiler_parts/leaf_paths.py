"""Leaf paths: the route each measure takes to the query's dimensions and filters, mixed-grain
refusals, and the root entity a query without a measure reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..ast import (
    ChildGroup,
    Filter,
    NormalizedQuery,
    child_groups,
    plain_filters,
    refuse_child_groups,
)
from ..errors import SemanticLayerError
from ..expressions import (
    MetricPredicateExpr,
)
from ..fanout import (
    UNANCHORED_TIME_VALID_HOP,
    analyze_fanout,
    resolve_path,
)
from ..ir import (
    BoundMeasure,
    PathSelection,
    RewriteStep,
)
from ..schema import (
    PackageConfig,
)
from .bind import (
    _bound_filter_clauses,
    _bound_metric_predicates,
    _measure_required_entities,
    check_conditional_aggregate_path,
    conditional_aggregate_route_refusal,
    is_conditional_aggregate,
)
from .grain_recovery import mixed_grain_pairing_enrichment
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _temporal_role_index,
)
from .paths import (
    _direct_dimension_source_expr,
    _leaf_time_role,
)
from .plan_checks import (
    _can_project_entity_key_from_source,
    _entity_in_terms_of_rewrite_supported,
    _fanout_dedup_refusal,
    _hop_conditions,
    _two_hop_conditions,
    _validate_where_value_type,
    child_group_route,
)
from .predicate import _path_selection
from .temporal import (
    _time_bound_relationship_ids,
)


def _leaf_path_selections(
    bound: BoundMeasure, config: PackageConfig, query: NormalizedQuery
) -> tuple[list[PathSelection], list[str], str]:
    """The leaf's paths, the entities it reads, and how it crosses one-to-many hops: "",
    "entity_in_terms_of" or "fanout_dedup"."""
    measures = _measure_index(config)
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    measure = measures[bound.measure_id]

    selections: list[PathSelection] = []
    required_entities: set[str] = set(_measure_required_entities(measure, config))
    # An aggregate_if's condition joins as a where filter does (a lookup), on the route that
    # filter takes; every leaf that plans it refuses that route unless each hop is many-to-one.
    conditional = is_conditional_aggregate(measure)
    for entity_id in sorted(required_entities):
        try:
            selection = _path_selection(
                config=config,
                query=query,
                start_entity=measure.entity,
                target_entity=entity_id,
                purpose="aggregate_if" if conditional else "measure_expr",
            )
        except SemanticLayerError as exc:
            if conditional and exc.details.get("reason") == UNANCHORED_TIME_VALID_HOP:
                # The aggregate_if's own rule refuses a hop valid over time, with or without one.
                check_conditional_aggregate_path(measure, entity_id, exc.details["path"], config)
            if not conditional or exc.code not in {"AMBIGUOUS_PATH", "PATH_NOT_FOUND"}:
                raise
            raise conditional_aggregate_route_refusal(measure, entity_id, exc) from exc
        if selection is not None:
            if conditional:
                check_conditional_aggregate_path(measure, entity_id, selection.chosen_path, config)
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
                purpose="time",
            )
            if selection is not None:
                selections.append(selection)
    for item in plain_filters(query.where):  # child groups route below
        dim = dimensions[item.field]
        required_entities.add(dim.entity)
        if _direct_dimension_source_expr(measure.entity, item.field, config) is not None:
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=dim.entity,
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
            purpose="metric_filter",
        )
        if selection is not None:
            selections.append(selection)
    groups = child_groups(query.where)
    for group in groups:
        route, lookups = child_group_route(config, query, measure, group)
        required_entities.add(group.child)
        required_entities.update(row.target_entity for row in lookups if row is not None)
        selections.append(route)
    for predicate in _bound_metric_predicates(bound):
        required_entities.add(predicate.entity)
        if _can_project_entity_key_from_source(measure.entity, predicate.entity, config):
            continue
        selection = _path_selection(
            config=config,
            query=query,
            start_entity=measure.entity,
            target_entity=predicate.entity,
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
        # A child group is a filter: only the semi-join leaf applies it.
        if not groups and all(
            _entity_in_terms_of_rewrite_supported(
                measure=measure, bound=bound, selection=row, config=config, query=query
            )
            for row in unsupported
        ):
            conditions = _hop_conditions(bound, unsupported, config, query)
            if len(conditions) <= 1:
                return selections, sorted(required_entities), "entity_in_terms_of"
            why_invalid, row = _two_hop_conditions(conditions)
            raise _mixed_grain_error(
                target_entity=row.target_entity,
                purpose=row.purpose,
                chosen=list(row.chosen_path),
                analysis=row.analysis,
                config=config,
                query=query,
                bound_measures=[bound],
                why_invalid=why_invalid,
            )
        refusal = _fanout_dedup_refusal(
            measure=measure, bound=bound, selections=selections, config=config, query=query
        )
        if refusal is None:
            return selections, sorted(required_entities), "fanout_dedup"
        why_invalid, row = refusal
        raise _mixed_grain_error(
            target_entity=row.target_entity,
            purpose=row.purpose,
            chosen=list(row.chosen_path),
            analysis=row.analysis,
            config=config,
            query=query,
            bound_measures=[bound],
            why_invalid=why_invalid,
        )
    return selections, sorted(required_entities), ""


def _mixed_grain_error(
    *,
    target_entity: str,
    purpose: str,
    chosen: list[str],
    analysis: dict[str, Any],
    config: PackageConfig,
    query: NormalizedQuery,
    bound_measures: list[BoundMeasure],
    why_invalid: str = "",
) -> SemanticLayerError:
    """MIXED_GRAIN_INVALID with recovery enrichment for the failed pairing.

    Beyond the path/analysis evidence and ``why_invalid``, the details carry
    ``compatible_measures`` / ``compatible_dimensions`` /
    ``time_axis_recovery`` so agents see the workable neighbours instead of
    brute-forcing the registry (see ``compiler_parts.grain_recovery``).
    """
    details: dict[str, Any] = {
        "target_entity": target_entity,
        "purpose": purpose,
        "path": list(chosen),
        "analysis": analysis,
    }
    if why_invalid:
        details["why_invalid"] = why_invalid
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


def _checked_where_dimension(
    item: Filter, dimensions: dict[str, Any], measures: dict[str, Any], path: str = "where"
) -> Any:
    """The dimension a where filter compares, once its value suits it."""
    if item.field not in dimensions:
        if item.field in measures:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"where filter field '{item.field}' is a measure, not a dimension",
                details={
                    "dimension": item.field,
                    "measure": item.field,
                    "path": "where.field",
                    "why_invalid": (
                        "where filters compare row-level dimensions to literal values; filter "
                        "entities by a measure with an entity filter, or results with having."
                    ),
                    "recovery_hints": [
                        {
                            "kind": "use_metric_filter_or_segment",
                            "message": (
                                "Keep entities by an aggregate with an entity filter in "
                                "where ({entity, having, over}), or filter by a reachable "
                                "dimension or segment that encodes the governed cohort."
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
    _validate_where_value_type(dimensions[item.field], item, path)
    return dimensions[item.field]


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
    for index, item in enumerate(query.where):
        if isinstance(item, ChildGroup):
            if item.child not in _entity_index(config):
                raise SemanticLayerError(
                    "OBJECT_NOT_FOUND",
                    f"Unknown entity '{item.child}'",
                    details={"entity": item.child, "path": f"where[{index}].entity"},
                )
            for position, condition in enumerate(item.where):
                at = f"where[{index}].{item.match}[{position}]"
                _checked_where_dimension(condition, dimensions, measures, at)
            targets[item.child] = "child_group"
            continue
        dim = _checked_where_dimension(item, dimensions, measures, f"where[{index}]")
        targets[dim.entity] = "where"
    for target_entity, purpose in sorted(targets.items()):
        if target_entity == root_entity:
            continue
        try:
            chosen, candidates = resolve_path(config, start=root_entity, target=target_entity)
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
            if exc.code == "AMBIGUOUS_PATH":
                # A calendar date reached through another fact's rows is not the question's
                # date either way: keep the time-block recovery a calendar group_by gets.
                recovery = mixed_grain_pairing_enrichment(
                    config=config,
                    query=query,
                    measure_ids=[row.measure_id for row in bound_measures],
                    target_entity=target_entity,
                ).get("time_axis_recovery")
                if recovery:
                    exc.details["time_axis_recovery"] = recovery
            raise
        selected_paths[target_entity] = list(chosen)
        candidate_paths[target_entity] = [list(path) for path in candidates]
        try:
            analysis = analyze_fanout(
                config,
                root_entity,
                chosen,
                time_bound_relationships=_time_bound_relationship_ids(query, config),
            )
        except SemanticLayerError as exc:
            if exc.details.get("reason") != UNANCHORED_TIME_VALID_HOP:
                raise
            if purpose != "measure":
                # Left to each leaf, as a rewrite is: one that joins this path refuses it, and
                # one that reads the dimension from its own rows (a shared key) joins nothing.
                continue
            # No leaf joins the path to another measure's entity: each measure aggregates on
            # its own and is reconciled on the grain keys, whose own paths still refuse.
            measure = next(
                row for row in bound_measures if measures[row.measure_id].entity == target_entity
            )
            analyses[target_entity] = {
                "status": "rewrite_required",
                "reason": exc.details["reason"],
            }
            rewrite_steps.append(
                RewriteStep(
                    kind="leaf_preaggregate_join",
                    status="applied",
                    measure_id=measure.measure_id,
                    reason="Independent fact families compiled as separate leaf aggregates and reconciled on final grain keys",
                    details={"target_entity": target_entity, "path": list(chosen)},
                )
            )
            continue
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
        # A group_by or where path that needs a rewrite is left to each measure leaf, which
        # crosses it (and says so in a rewrite step) or refuses (see _leaf_path_selections).
    return selected_paths, candidate_paths, rewrite_steps, analyses


def _query_target_entities(query: NormalizedQuery, config: PackageConfig) -> dict[str, str]:
    dimensions = _dimension_index(config)
    temporal_roles = _temporal_role_index(config)
    targets: dict[str, str] = {}
    for dim_id in query.group_by:
        if dim_id not in dimensions:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND",
                f"Unknown dimension '{dim_id}'",
                details={"dimension": dim_id},
            )
        targets.setdefault(dimensions[dim_id].entity, "group_by")
    rows = refuse_child_groups(query.where, "in a query without a measure")
    for index, item in enumerate(rows):
        dim = _checked_where_dimension(item, dimensions, _measure_index(config), f"where[{index}]")
        targets.setdefault(dim.entity, "where")
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
    for item in refuse_child_groups(query.where, "in a query without a measure"):
        preferred.append(dimensions[item.field].entity)
    return list(dict.fromkeys(preferred))


def _candidate_root_summary(
    root_entity: str, query: NormalizedQuery, config: PackageConfig
) -> dict[str, Any]:
    targets = _query_target_entities(query, config)
    selected_paths: dict[str, list[str]] = {}
    candidate_paths: dict[str, list[list[str]]] = {}
    analyses: dict[str, dict[str, Any]] = {}
    total_hops = 0
    max_hops = 0
    for target_entity, purpose in sorted(targets.items()):
        if target_entity == root_entity:
            continue
        chosen, candidates = resolve_path(config, start=root_entity, target=target_entity)
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
            # Prefer the refusal a query time would lift, so it names the hop and the fix.
            raise next(
                (exc for exc in failures if exc.details.get("reason") == UNANCHORED_TIME_VALID_HOP),
                failures[0],
            )
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
