"""Which entity pairs need a route decision, and which answers a package change moves.

A route between two entities is a business definition, and ``fanout.resolve_path`` is the one
place that applies the package's decisions. This module asks it about every pair a question
can need: the entity of a measure (an entity count included) as the start, and an entity
with a dimension, reachable from it, as the target.

* :func:`route_census` lists the pairs the resolver refuses until a decision is recorded
  (``undecided``) and the pairs it answers by a rule other than a recorded row or the only
  route (``assumed``).
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
from .errors import SemanticLayerError
from .fanout import resolve_path, route_basis, route_meaning, route_pin
from .schema import PackageConfig, PathPreferenceConfig

Pair = tuple[str, str]
# route_basis values that mean nobody decided the route: one route, or none was chosen.
_DECIDED_BASES = frozenset({"", "recorded"})


@dataclass(frozen=True)
class RouteOutcome:
    """``resolve_path``'s answer for a pair: its route and every route considered, or the code
    and details of its refusal."""

    path: tuple[str, ...] = ()
    routes: tuple[tuple[str, ...], ...] = ()
    refused: str = ""
    details: dict[str, Any] = field(default_factory=dict, compare=False)

    def shape(self) -> dict[str, Any]:
        """The outcome as a ``route_changes`` side: its route or its refusal code."""
        return {"refused": self.refused} if self.refused else {"relationship_path": [*self.path]}

    def candidate_routes(self) -> list[tuple[str, ...]]:
        """Every route the resolver weighed for the pair, as the outcome reports them."""
        if not self.refused:
            return list(self.routes)
        if "candidates" in self.details:
            return [tuple(route) for route in self.details["candidates"]]
        options = dict(self.details.get("clarification") or {}).get("options") or []
        return [tuple(option["relationship_path"]) for option in options]


def census_pairs(config: PackageConfig) -> list[Pair]:
    """Every (start, target) a question can need: ``start`` is a measure's entity, and
    ``target`` is another entity with a dimension that the relationships reach from it."""
    graph = get_package_analysis(config).graph
    targets = {dimension.entity for dimension in config.dimensions}
    pairs: list[Pair] = []
    for start in sorted({measure.entity for measure in config.measures if measure.entity}):
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
    """Each pair's outcome, asking ``resolve_path`` once per pair (it caches per package)."""
    outcomes: dict[Pair, RouteOutcome] = {}
    for start, target in pairs:
        try:
            path, routes = resolve_path(config, start=start, target=target)
        except SemanticLayerError as exc:
            outcomes[(start, target)] = RouteOutcome(refused=exc.code, details=exc.details)
        else:
            outcomes[(start, target)] = RouteOutcome(
                path=tuple(path), routes=tuple(tuple(route) for route in routes)
            )
    return outcomes


def route_census(config: PackageConfig) -> dict[str, list[dict[str, Any]]]:
    """The census pairs that need a business decision.

    ``undecided``: pairs refused with ``AMBIGUOUS_PATH``, each with the refusal's ``details``
    as raised (its routes, their meanings and the row that records each). ``assumed``: pairs
    answered by a basis other than a recorded row or the only route (``route_basis``, for
    example the start's own key), with that route, for the author to confirm.
    """
    undecided: list[dict[str, Any]] = []
    assumed: list[dict[str, Any]] = []
    for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items():
        ends = {"source_entity": start, "target_entity": target}
        if outcome.refused == "AMBIGUOUS_PATH":
            undecided.append({**ends, "details": outcome.details})
        elif not outcome.refused:
            basis = route_basis(config, start, target, outcome.routes)
            if basis not in _DECIDED_BASES:
                assumed.append({**ends, "relationship_path": [*outcome.path], "basis": basis})
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
        kept = not old.refused and _route_exists(head, *pair, old.path)
        changes.append(RouteChange(pair, old, new, route_pin(*pair, [*old.path]) if kept else None))
    return changes


def route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """Every census pair of either package, between entities both declare, that ``head``
    resolves differently from ``base``: ``base`` and ``head`` hold its ``relationship_path``
    or the code it is ``refused`` with; ``keep_base`` is the ``graph.path_preferences`` row
    that keeps the base route in ``head``, or ``None`` when the base refused or its route no
    longer exists."""
    return [change.payload() for change in _changes(base, head)]


def _unkept(base: PackageConfig, head: PackageConfig) -> list[RouteChange]:
    """The changes a row must undo: ``base`` answered the pair, ``head`` refuses it or takes
    another route, the base route still exists, and ``head`` leaves the pair's own row as
    ``base`` had it (otherwise the change decides the pair itself)."""
    base_rows = get_package_analysis(base).path_preferences
    head_rows = get_package_analysis(head).path_preferences
    return [
        change
        for change in _changes(base, head)
        if change.keep_base is not None and head_rows.get(change.pair) == base_rows.get(change.pair)
    ]


def unkept_route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """The ``route_changes`` entries that :func:`keep_routes` records a row for; once its rows
    are in ``head``, there are none."""
    return [change.payload() for change in _unkept(base, head)]


def keep_routes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """The fewest rows that keep ``base``'s answers in ``head`` (see :func:`_unkept`).

    Rows are added shortest base route first, re-resolving the rest after each, so a row that
    also settles another pair is the only one added. Each entry is ``{"row": ...,
    "new_routes": [...]}``: the routes ``head`` offers besides the base route, to name as
    alternatives or make the default later.
    """
    pending = sorted(_unkept(base, head), key=lambda change: (len(change.base.path), change.pair))
    added: list[dict[str, Any]] = []
    kept = head
    while pending:
        change, *pending = pending
        source, target = change.pair
        kept = replace(
            kept,
            path_preferences=[
                *kept.path_preferences,
                PathPreferenceConfig(source, target, [*change.base.path]),
            ],
        )
        added.append(
            {
                "row": change.keep_base,
                "new_routes": [
                    [*route]
                    for route in change.head.candidate_routes()
                    if route != change.base.path
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
        return route_meaning(config, start, side["relationship_path"])

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
