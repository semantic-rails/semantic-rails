"""Which entity pairs need a route decision, and which answers a package change moves.

A route between two entities is a business definition, and ``fanout.resolve_route`` is the one
place that applies the package's decisions. This module asks it about every pair a question
can need: any entity as the start (distinct values and synthetic counts included), and an entity
with a dimension, reachable from it, as the target.

* :func:`route_census` lists the pairs the resolver refuses until a decision is recorded
  (``undecided``) and multi-route pairs answered by the start's own key (``assumed``).
* :func:`route_changes` lists the pairs whose resolution differs between two versions of a
  package, with the ``graph.path_preferences`` row that keeps the earlier route.
* :func:`keep_routes` gives the fewest such rows that make a changed package answer every
  pair it answered before, unless the change decides that pair itself;
  :func:`unkept_route_changes` lists the pairs still moved without such a row.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .compiler_parts.indexes import get_package_analysis
from .config_parts.route_rows import conflicting_rows
from .errors import SemanticLayerError
from .fanout import (
    _has_multiple_routes,
    package_hop_limit,
    pair_routes,
    resolve_route,
    route_pin,
    route_reading,
)
from .schema import PackageConfig, PathPreferenceConfig

Pair = tuple[str, str]


@dataclass(frozen=True)
class RouteOutcome:
    """``resolve_route``'s answer for a pair: its route and every route considered, or the code
    and details of its refusal."""

    path: tuple[str, ...] = ()
    routes: tuple[tuple[str, ...], ...] = ()
    basis: str = ""
    refused: str = ""
    details: dict[str, Any] = field(default_factory=dict, compare=False)

    def shape(self) -> dict[str, Any]:
        """The outcome as a ``route_changes`` side: its route or its refusal code."""
        return {"refused": self.refused} if self.refused else {"relationship_path": [*self.path]}


def census_pairs(config: PackageConfig) -> list[Pair]:
    """Every (start, target) a question can need: ``start`` is any entity, and
    ``target`` is another entity with a dimension that the relationships reach from it."""
    graph = get_package_analysis(config).graph
    targets = {dimension.entity for dimension in config.dimensions}
    pairs: list[Pair] = []
    for start in sorted(entity.id for entity in config.entities):
        reached = {start}
        pending = deque([start])
        while pending:
            for neighbor, _rel_id in graph.get(pending.popleft(), []):
                if neighbor not in reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
        pairs.extend((start, target) for target in sorted(targets & reached) if target != start)
    return pairs


def resolve_pairs(config: PackageConfig, pairs: Iterable[Pair]) -> dict[Pair, RouteOutcome]:
    """Each pair's outcome, asking ``resolve_route`` once per pair (it caches per package)."""
    outcomes: dict[Pair, RouteOutcome] = {}
    for start, target in pairs:
        try:
            resolution = resolve_route(config, start=start, target=target)
        except SemanticLayerError as exc:
            outcomes[(start, target)] = RouteOutcome(refused=exc.code, details=exc.details)
        else:
            outcomes[(start, target)] = RouteOutcome(
                path=resolution.routes[0], routes=resolution.routes, basis=resolution.basis
            )
    return outcomes


def route_census(config: PackageConfig) -> dict[str, list[dict[str, Any]]]:
    """The census pairs that need a business decision.

    ``undecided``: pairs refused with ``AMBIGUOUS_PATH``, each with the refusal's ``details``
    as raised (its routes, their meanings and the row that records each). ``assumed``: pairs
    answered by the start's own key, with two or more routes, for the author to confirm.
    """
    undecided: list[dict[str, Any]] = []
    assumed: list[dict[str, Any]] = []
    graph = get_package_analysis(config).graph
    pairs = (
        pair
        for pair in census_pairs(config)
        if _has_multiple_routes(graph, *pair, package_hop_limit(config))
    )
    for (start, target), outcome in resolve_pairs(config, pairs).items():
        ends = {"source_entity": start, "target_entity": target}
        if outcome.refused == "AMBIGUOUS_PATH":
            undecided.append({**ends, "details": outcome.details})
        elif outcome.basis == "colocated_key" and len(outcome.routes) >= 2:
            assumed.append({**ends, "relationship_path": [*outcome.path], "basis": outcome.basis})
    return {"undecided": undecided, "assumed": assumed}


@dataclass(frozen=True)
class RouteChange:
    pair: Pair
    base: RouteOutcome
    head: RouteOutcome
    keep_base: dict[str, Any] | None

    def payload(self) -> dict[str, Any]:
        return {
            "source_entity": self.pair[0],
            "target_entity": self.pair[1],
            "base": self.base.shape(),
            "head": self.head.shape(),
            "keep_base": self.keep_base,
        }


def _route_exists(config: PackageConfig, start: str, target: str, path: Iterable[str]) -> bool:
    """Whether ``path`` still walks from ``start`` to ``target`` in ``config``, so a row can
    record it."""
    graph = get_package_analysis(config).graph
    current = start
    for rel_id in path:
        step = next((entity for entity, edge in graph.get(current, []) if edge == rel_id), None)
        if step is None:
            return False
        current = step
    return current == target


def _changes(base: PackageConfig, head: PackageConfig) -> list[RouteChange]:
    entities = {row.id for row in base.entities} & {row.id for row in head.entities}
    pairs = sorted(
        pair
        for pair in {*census_pairs(base), *census_pairs(head)}
        if pair[0] in entities and pair[1] in entities
    )
    before, after = resolve_pairs(base, pairs), resolve_pairs(head, pairs)
    changes: list[RouteChange] = []
    for pair in pairs:
        old, new = before[pair], after[pair]
        if old.shape() == new.shape():
            continue
        kept = (
            not old.refused
            and len(old.path) <= package_hop_limit(head)
            and _route_exists(head, *pair, old.path)
        )
        changes.append(RouteChange(pair, old, new, route_pin(*pair, [*old.path]) if kept else None))
    return changes


def route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """Every census pair of either package, between entities both declare, that ``head``
    resolves differently from ``base``: ``base`` and ``head`` hold its ``relationship_path``
    or the code it is ``refused`` with; ``keep_base`` is the ``graph.path_preferences`` row
    that keeps the base route in ``head``, or ``None`` when the base refused or its route no
    longer exists or exceeds the head's hop ceiling."""
    return [change.payload() for change in _changes(base, head)]


def _unkept(base: PackageConfig, head: PackageConfig) -> list[RouteChange]:
    """The changes a row must undo: ``base`` answered the pair, ``head`` refuses it or takes
    another route, without a newly written row for that pair. Deleting its row is not a
    new route decision. A refusal caused by a deliberate cut need not be undone."""
    base_rows = get_package_analysis(base).path_preferences
    head_rows = get_package_analysis(head).path_preferences
    return [
        change
        for change in _changes(base, head)
        if not change.base.refused
        and (change.keep_base is not None or not change.head.refused)
        and (
            head_rows.get(change.pair) is None
            or head_rows.get(change.pair) == base_rows.get(change.pair)
        )
    ]


def unkept_route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """Answered pairs still moved without their own row, including an unkeepable route swap.
    A deliberate cut may refuse a pair whose earlier route cannot be kept."""
    return [change.payload() for change in _unkept(base, head)]


def keep_routes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """The fewest rows that keep ``base``'s answers in ``head`` (see :func:`_unkept`).

    Rows are added shortest base route first, re-resolving the rest after each, so a row that
    also settles another pair is the only one added. Each entry is ``{"row": ...,
    "new_routes": [...]}``: the routes ``head`` offers besides the base route, to name as
    alternatives or make the default later.
    """
    pending = sorted(
        (change for change in _unkept(base, head) if change.keep_base is not None),
        key=lambda change: (len(change.base.path), change.pair),
    )
    added: list[dict[str, Any]] = []
    kept = head
    while pending:
        change, *pending = pending
        source, target = change.pair
        row = PathPreferenceConfig(source, target, [*change.base.path])
        conflicts = conflicting_rows(
            get_package_analysis(kept).relationships, row, kept.path_preferences
        )
        if conflicts:
            raise SemanticLayerError(
                "ROUTE_DECISION_NOT_RECORDED",
                "The row keeping the earlier route disagrees with existing route rows; "
                "nothing was written. Record each moved pair's route in the change itself.",
                details={
                    "row": change.keep_base,
                    "conflicts_with": [
                        route_pin(item.source_entity, item.target_entity, item.relationship_path)
                        for item in conflicts
                    ],
                    "route_changes": route_changes(base, head),
                },
            )
        kept = replace(
            kept,
            path_preferences=[
                *kept.path_preferences,
                row,
            ],
        )
        added.append(
            {
                "row": change.keep_base,
                "new_routes": [
                    [*route]
                    for route in pair_routes(head, source, target)
                    if tuple(route) != change.base.path
                ],
            }
        )
        outcomes = resolve_pairs(kept, [rest.pair for rest in pending])
        pending = [rest for rest in pending if outcomes[rest.pair].shape() != rest.base.shape()]
    return added


def route_change_lines(
    base: PackageConfig, head: PackageConfig, changes: Iterable[dict[str, Any]]
) -> list[str]:
    """``route_changes`` entries in business words, one Markdown bullet each."""

    def label(config: PackageConfig, entity_id: str) -> str:
        entity = get_package_analysis(config).entities.get(entity_id)
        return (entity.label or entity.name) if entity is not None else entity_id

    def reads(config: PackageConfig, start: str, side: dict[str, Any]) -> str:
        if "refused" in side:
            return f"refused ({side['refused']})"
        return route_reading(config, start, side["relationship_path"])

    lines = []
    for change in changes:
        start, target = change["source_entity"], change["target_entity"]
        keep = "; `keep_base` keeps the earlier route" if change["keep_base"] else ""
        lines.append(
            f"- {label(head, start)} to {label(head, target)}: was "
            f"{reads(base, start, change['base'])}, now {reads(head, start, change['head'])}"
            f"{keep}"
        )
    return lines
