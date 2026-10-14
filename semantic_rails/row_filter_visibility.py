"""Which rows a row-filtered caller can see, defined once per relation.

A row is visible when it passes its relation's own filters and every owner on its package route
to a filtered model is visible. A model is *covered* by a filter when its package route to the
filter's model (``fanout.package_route``: the package's rungs, never a query's decision) walks
only N:1 hops on single-column keys, with no temporal-validity hop, and the owner it reaches
first (its *parent*) is covered by the rest of that route. A model no such route reaches is
*uncovered*: no filter restricts its rows. Anything else (an ambiguous, composite, temporal or
inconsistent route; models sharing a relation that disagree; two spellings of one relation)
is *unsupported*: it is refused under the filter, never read.

``Visibility.predicate`` is the one builder of a relation's visibility predicate ``V_T``:
its own filters AND, per parent hop, ``fk IN (SELECT key FROM parent WHERE V_parent)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .compiler_parts.indexes import get_package_analysis
from .compiler_parts.paths import _reaches_at_most_one
from .errors import SemanticLayerError
from .fanout import package_route
from .row_filters import ParentHop, RowFilter, Visibility, relation_key
from .schema import PackageConfig, base_of


@dataclass(frozen=True)
class Coverage:
    """One filter's reach over the package's models (entity ids)."""

    filtered: tuple[str, ...]
    covered: Mapping[str, tuple[str, ...]]  # model -> its route to the filter's model
    uncovered: tuple[str, ...]
    unsupported: Mapping[str, str]  # model -> why
    # Relation -> its parent hop; relation -> why it is unsupported.
    hops: Mapping[str, ParentHop]
    refused: Mapping[str, str]


def coverage(config: PackageConfig, entity_id: str) -> Coverage:
    """The coverage of a filter on ``entity_id``'s relation, computed once per package."""
    base = base_of(config)
    analysis = get_package_analysis(base)
    cache = analysis.row_filter_coverage
    if entity_id not in cache:
        cache[entity_id] = _coverage(base, entity_id)
    return cache[entity_id]


def _coverage(base: PackageConfig, entity_id: str) -> Coverage:
    analysis = get_package_analysis(base)
    entities = analysis.entities
    target = relation_key(entities[entity_id].table)
    filtered, uncovered = [], []
    routes: dict[str, tuple[str, ...]] = {}
    first: dict[str, ParentHop] = {}
    unsupported_models: dict[str, str] = {}
    for model, entity in sorted(entities.items()):
        if not entity.table:
            continue
        if relation_key(entity.table) == target:
            filtered.append(model)
            continue
        try:
            route = tuple(package_route(base, start=model, target=entity_id).routes[0])
        except SemanticLayerError as exc:
            options = (exc.details.get("clarification") or {}).get("options") or []
            if exc.code == "AMBIGUOUS_PATH" and any(
                _walk(analysis, model, option.get("relationship_path") or ())[0] != "uncovered"
                for option in options
            ):
                unsupported_models[model] = "its route to the filtered model is undecided"
            else:
                uncovered.append(model)
            continue
        status, hop = _walk(analysis, model, route)
        if status == "covered" and hop is not None:
            routes[model], first[model] = route, hop
        elif status == "uncovered":
            uncovered.append(model)
        else:
            unsupported_models[model] = status
    # A consistent tree: a covered model's parent is filtered, or covered by the rest of the route.
    for model, route in list(routes.items()):
        parent = _far_end(analysis, model, route[0])
        if relation_key(entities[parent].table) != target and routes.get(parent) != route[1:]:
            unsupported_models[model] = "its owner's route to the filtered model is another"
    for model in unsupported_models:
        routes.pop(model, None)
    # Relation-level: models over one relation must agree, and one relation has one spelling.
    hops: dict[str, ParentHop] = {}
    refused: dict[str, str] = {}
    by_relation: dict[str, list[str]] = {}
    for model, entity in entities.items():
        if entity.table:
            by_relation.setdefault(relation_key(entity.table), []).append(model)
    for relation, models in by_relation.items():
        if relation == target:
            continue
        states = {
            first[m] if m in routes else unsupported_models.get(m, "uncovered") for m in models
        }
        if len(states) > 1:
            refused[relation] = "models over this relation disagree on their owner"
        elif isinstance(state := next(iter(states)), ParentHop):
            hops[relation] = state
        elif state != "uncovered":
            refused[relation] = state
    restricted = {target, *hops}
    for relation in by_relation:
        spellings = {
            other
            for other in by_relation
            if other != relation and other.rsplit(".", 1)[-1] == relation.rsplit(".", 1)[-1]
        }
        if spellings and (spellings | {relation}) & restricted:
            refused[relation] = "another model reads this relation under another name"
    return Coverage(
        filtered=tuple(filtered),
        covered=routes,
        uncovered=tuple(uncovered),
        unsupported=unsupported_models,
        hops=hops,
        refused=refused,
    )


def _far_end(analysis: Any, current: str, rel_id: str) -> str:
    rel = analysis.relationships[rel_id]
    return rel.target_entity if current == rel.source_entity else rel.source_entity


def _walk(analysis: Any, start: str, route: Iterable[str]) -> tuple[str, ParentHop | None]:
    """Whether ``route`` from ``start`` is ownership ("covered", with its first hop), no
    ownership ("uncovered": a hop may reach several rows) or unsupported (why)."""
    current, hop = start, None
    for rel_id in route:
        rel = analysis.relationships[rel_id]
        if not _reaches_at_most_one(rel, current):
            return "uncovered", None
        if rel.source_entity == rel.target_entity:
            return "its route walks a relationship of a model to itself", None
        if rel_id in analysis.temporal_relationship_ids:
            return "its route has a temporal-validity hop", None
        if (
            len(rel.source_columns or [rel.source_column]) != 1
            or len(rel.target_columns or [rel.target_column]) != 1
        ):
            return "its route has a composite key", None
        forward = current == rel.source_entity
        near, far = (
            (rel.source_column, rel.target_column)
            if forward
            else (rel.target_column, rel.source_column)
        )
        current = rel.target_entity if forward else rel.source_entity
        if hop is None:
            hop = ParentHop(near, analysis.entities[current].table, far)
    return ("covered", hop) if hop is not None else ("uncovered", None)


def visibility_for(config: PackageConfig, filters: Sequence[RowFilter]) -> Visibility:
    """The visibility of ``filters`` (the request's applying row filters) on the package."""
    base = base_of(config)
    if not filters:
        return Visibility(())
    parents: dict[str, set[ParentHop]] = {}
    refused: dict[str, str] = {}
    for row in filters:
        reach = coverage(base, row.entity)
        for relation, hop in reach.hops.items():
            parents.setdefault(relation, set()).add(hop)
        refused.update(reach.refused)
    from .dialects import dialect_for_warehouse
    from .relation_pipelines import pipeline_cte_names

    return Visibility(
        tuple(filters),
        parents={relation: tuple(sorted(hops)) for relation, hops in sorted(parents.items())},
        refused=refused,
        modeled=frozenset(relation_key(row.table) for row in base.entities if row.table),
        load_logs=frozenset(
            relation_key(row.zero_through.relation)
            for row in base.measures
            if row.zero_through is not None and row.zero_through.kind == "loaded"
        ),
        pipelines=frozenset(relation_key(name) for name in pipeline_cte_names(base)),
        subquery_in_join_on=dialect_for_warehouse(base.package.warehouse).subquery_in_join_on,
    )


def coverage_report(config: PackageConfig) -> list[dict[str, Any]]:
    """Per row filter: the models it filters, covers and leaves uncovered (the ones it holds
    unless ``shared``), and the ones it refuses, with why."""
    from .row_filters import is_row_filter, row_filter

    base = base_of(config)
    analysis = get_package_analysis(base)
    out = []
    for policy in base.semantic_policies:
        if not is_row_filter(policy):
            continue
        row = row_filter(base, policy)
        reach = coverage(base, row.entity)
        out.append(
            {
                "policy_id": policy.id,
                "filtered": list(reach.filtered),
                "covered": sorted(reach.covered),
                "uncovered": [
                    model
                    for model in reach.uncovered
                    if model not in row.shared and analysis.entities[model].allowed_as_root
                ],
                "shared": list(row.shared),
                "unsupported": dict(sorted(reach.unsupported.items())),
            }
        )
    return out


def hold_unowned_reads(
    config: PackageConfig,
    visibility: Visibility,
    *,
    roots: Iterable[str],
    routes: Iterable[tuple[str, str, Sequence[str]]],
) -> None:
    """Refuse (POLICY_DENIED) a filtered question the visible database answers but the package
    holds: a value starting from a model no applying filter restricts, unless every one of them
    declares it ``shared``; an N:1 hop into a restricted model that isn't the walking model's
    own owner (its answer would put visible rows under owners the caller can't see); or a
    model the filters can't be traced through. This never authorizes a read: the pass does.
    Refusals name routes and the policies the caller's view shows, never values."""
    base = base_of(config)
    analysis = get_package_analysis(base)
    entities = analysis.entities
    policies = sorted({row.policy_id for row in visibility.filters})

    def table(model: str) -> str:
        return relation_key(entities[model].table) if model in entities else ""

    def hold(reason: str, message: str, **details: Any) -> SemanticLayerError:
        return SemanticLayerError(
            "POLICY_DENIED", message, details={"reason": reason, "policy_ids": policies, **details}
        )

    for model in sorted(roots):
        relation = table(model)
        if relation in visibility.refused:
            raise hold(
                "row_filter_coverage_unsupported",
                f"A row filter applies to this request, and '{model}' can't be traced to the "
                "filtered model by one package route, so it can't be read under it.",
                entity=model,
            )
        if relation in visibility.restricted or not relation:
            continue
        if all(model in row.shared for row in visibility.filters):
            continue
        raise hold(
            "row_filter_uncovered_root",
            f"A row filter applies to this request, and no filter reaches '{model}', so the "
            "package doesn't say which of its rows you may count. Ask about the rows the "
            "filter does reach (for instance those that have records you can see), or ask "
            "the package author to list it as shared.",
            entity=model,
        )
    for start, target, route in routes:
        current = start
        for rel_id in route:
            rel = analysis.relationships.get(rel_id)
            if rel is None:
                break
            far = _far_end(analysis, current, rel_id)
            into = table(far)
            if into in visibility.refused:
                raise hold(
                    "row_filter_coverage_unsupported",
                    f"A row filter applies to this request, and '{far}' can't be traced to "
                    "the filtered model by one package route, so it can't be read under it.",
                    entity=far,
                )
            owned = table(current) == into or _walk(analysis, current, [rel_id])[1] in (
                visibility.parents.get(table(current), ())
            )
            if into in visibility.restricted and _reaches_at_most_one(rel, current) and not owned:
                raise hold(
                    "row_filter_route_not_owned",
                    f"A row filter applies to this request, and the route from '{start}' to "
                    f"'{target}' looks up '{far}' through '{rel_id}', which isn't how "
                    f"'{current}' is owned, so rows you can see would land under '{far}' rows "
                    "you can't. Ask by the route its owner is reached through instead.",
                    source_entity=start,
                    target_entity=target,
                    relationship_path=list(route),
                )
            current = far
