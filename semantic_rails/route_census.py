"""Which entity pairs need a route decision, and which answers a package change moves.

A route between two entities is a business definition, and ``fanout.package_route`` applies
the package's decisions independently of query overrides. This module asks it about every
pair a question can need: any entity as the start (distinct values and synthetic counts
included), and each other reachable entity as the target (child groups need no dimension
on the child itself).

* :func:`route_census` lists the pairs the resolver refuses until a decision is recorded
  (``undecided``), multi-route pairs answered by the start's own key (``assumed``), and
  resolved routes crossing undeclared child rows (``pass_through``).
* :func:`route_changes` lists the pairs whose resolution differs between two versions of a
  package.
* :func:`unkept_route_changes` lists answered pairs a change moves without an explicit
  decision. Architect refuses these changes rather than recording a decision for the author.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .compiler_parts.indexes import get_package_analysis
from .errors import SemanticLayerError
from .fanout import (
    entity_label,
    package_hop_limit,
    package_route,
    pass_through_disclosure,
    route_reading,
)
from .schema import PackageConfig

Pair = tuple[str, str]


@dataclass(frozen=True)
class RouteOutcome:
    """``package_route``'s answer for a pair: its route and every route considered, or the code
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
    ``target`` is each other entity that the relationships reach from it."""
    graph = get_package_analysis(config).graph
    pairs: list[Pair] = []
    for start in sorted(entity.id for entity in config.entities):
        reached = {start}
        pending = deque([start])
        while pending:
            for neighbor, _rel_id in graph.get(pending.popleft(), []):
                if neighbor not in reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
        pairs.extend((start, target) for target in sorted(reached) if target != start)
    return pairs


def resolve_pairs(config: PackageConfig, pairs: Iterable[Pair]) -> dict[Pair, RouteOutcome]:
    """Each pair's package-only outcome, cached per package regardless of query overrides."""
    outcomes: dict[Pair, RouteOutcome] = {}
    for start, target in pairs:
        try:
            resolution = package_route(config, start=start, target=target)
        except SemanticLayerError as exc:
            outcomes[(start, target)] = RouteOutcome(refused=exc.code, details=exc.details)
        else:
            outcomes[(start, target)] = RouteOutcome(
                path=resolution.routes[0], routes=resolution.routes, basis=resolution.basis
            )
    return outcomes


def route_census(config: PackageConfig) -> dict[str, list[dict[str, Any]]]:
    """The census pairs that need a business decision or a crossing disclosure.

    ``undecided``: pairs refused with ``AMBIGUOUS_PATH``, each with the refusal's ``details``
    as raised (its routes, their meanings and the row that records each). ``assumed``: pairs
    answered by the start's own key, with two or more routes, for the author to confirm.
    ``pass_through``: resolved only or inherited routes crossing undeclared child rows,
    with the same disclosure as an answer; these are not pairs awaiting a decision.
    """
    undecided: list[dict[str, Any]] = []
    assumed: list[dict[str, Any]] = []
    pass_through: list[dict[str, Any]] = []
    for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items():
        ends = {"source_entity": start, "target_entity": target}
        if outcome.refused == "AMBIGUOUS_PATH":
            undecided.append({**ends, "details": outcome.details})
        elif outcome.basis == "colocated_key" and len(outcome.routes) >= 2:
            assumed.append({**ends, "relationship_path": [*outcome.path], "basis": outcome.basis})
        elif outcome.basis in {"only_route", "inherited"}:
            disclosure = pass_through_disclosure(
                config, start, target, outcome.path, route_basis=outcome.basis
            )
            if disclosure:
                pass_through.append({**ends, "message": disclosure[0], "details": disclosure[1]})
    return {"undecided": undecided, "assumed": assumed, "pass_through": pass_through}


@dataclass(frozen=True)
class RouteChange:
    pair: Pair
    base: RouteOutcome
    head: RouteOutcome

    def payload(self) -> dict[str, Any]:
        return {
            "source_entity": self.pair[0],
            "target_entity": self.pair[1],
            "base": self.base.shape(),
            "head": self.head.shape(),
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
        changes.append(RouteChange(pair, old, new))
    return changes


def route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """Every census pair of either package, between entities both declare, that ``head``
    resolves differently from ``base``: ``base`` and ``head`` hold its ``relationship_path``
    or the code it is ``refused`` with. No recovery rows are suggested."""
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
        and (
            not change.head.refused
            or (
                len(change.base.path) <= package_hop_limit(head)
                and _route_exists(head, *change.pair, change.base.path)
            )
        )
        and (
            head_rows.get(change.pair) is None
            or head_rows.get(change.pair) == base_rows.get(change.pair)
        )
    ]


def unkept_route_changes(base: PackageConfig, head: PackageConfig) -> list[dict[str, Any]]:
    """Answered pairs still moved without their own row, including an unkeepable route swap.
    A deliberate cut may refuse a pair whose earlier route cannot be kept."""
    return [change.payload() for change in _unkept(base, head)]


def route_change_lines(
    base: PackageConfig, head: PackageConfig, changes: Iterable[dict[str, Any]]
) -> list[str]:
    """``route_changes`` entries in business words, one Markdown bullet each."""

    def reads(config: PackageConfig, start: str, side: dict[str, Any]) -> str:
        if "refused" in side:
            return f"refused ({side['refused']})"
        return route_reading(config, start, side["relationship_path"])

    lines = []
    for change in changes:
        start, target = change["source_entity"], change["target_entity"]
        lines.append(
            f"- {entity_label(head, start)} to {entity_label(head, target)}: was "
            f"{reads(base, start, change['base'])}, now {reads(head, start, change['head'])}"
        )
    return lines
