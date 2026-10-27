"""Which entity pairs need a route decision, and which answers a package change moves.

A route between two entities is a business definition, and ``fanout.package_route`` applies
the package's decisions independently of query overrides. This module asks it about every
pair a question can need: any entity as the start (distinct values and synthetic counts
included), and each other reachable entity as the target (child groups need no dimension
on the child itself).

* :func:`route_census` lists the pairs the resolver refuses until a decision is recorded
  (``undecided``), multi-route pairs answered by the start's own key (``assumed``) or by a
  lookup default (``defaults``), and resolved routes crossing undeclared child rows
  (``pass_through``). Each undecided and
  pass-through pair names the package's own queries that walk it (:func:`walked_pairs`): only
  those pairs need anything now, and every other one is asked when a question needs it.
* :func:`route_changes` lists the pairs whose resolution differs between two versions of a
  package.
* :func:`unkept_route_changes` lists answered pairs a change moves without an explicit
  decision. Architect refuses these changes rather than recording a decision for the author.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

# Through the compiler, which registers the nested compiles a binding runs.
from .compiler import bind_query, read_routes
from .compiler_parts.indexes import get_package_analysis
from .compiler_parts.paths import dimensions_over_column, text_dimension
from .compiler_parts.plan_checks import _object_default_query_temporal_role
from .config import package_root_for_source
from .errors import SemanticLayerError
from .fanout import (
    entity_label,
    package_hop_limit,
    package_route,
    pass_through_disclosure,
    route_pin,
    route_reading,
)
from .query_call import read_call, route_entry
from .registry import Registry
from .schema import PackageConfig
from .yaml_loader import load_named_entries

Pair = tuple[str, str]
# A query the package asks: the id of what asks it, its calls (the first one that binds past
# every refusal but a route's is walked) and its ``now``.
PackageQuery = tuple[str, list[dict[str, Any]], Any]
# A metric that reads periods compiles only with a time block: one of these grains.
_GRAINS = ("day", "month", "quarter", "year")
# The walks of the last few packages, by source fingerprint and queries: an Architect write
# parses the same package more than once.
_WALKS: OrderedDict[str, dict[Pair, list[str]]] = OrderedDict()
_WALKS_LOCK = Lock()
_WALKS_KEPT = 8


@dataclass(frozen=True)
class RouteOutcome:
    """``package_route``'s answer for a pair: its route and every route considered, or the code
    and details of its refusal."""

    path: tuple[str, ...] = ()
    routes: tuple[tuple[str, ...], ...] = ()
    basis: str = ""
    refused: str = ""
    by: str = ""
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
                path=resolution.routes[0],
                routes=resolution.routes,
                basis=resolution.basis,
                by=resolution.by,
            )
    return outcomes


def package_queries(config: PackageConfig, source_path: str | None = None) -> list[PackageQuery]:
    """The queries the package's own objects ask: each metric alone (one that reads periods by
    each grain in turn), and each example's and test's query in the package's ``examples/`` and
    ``tests/`` beside ``source_path``. A test that expects a refusal needs no route."""
    queries: list[PackageQuery] = []
    for recipe in config.metric_recipes:
        timed = [{"select": [recipe.id], "time": {"grain": grain}} for grain in _GRAINS]
        queries.append((recipe.id, [{"select": [recipe.id]}, *timed], None))
    if not source_path:
        return queries
    root = Path(package_root_for_source(source_path))
    for plural, singular in (("examples", "example"), ("tests", "test")):
        for entry_id, spec in load_named_entries(
            root / plural, plural_key=plural, singular_key=singular, defaults=("now",)
        ):
            if spec.get("kind") == "validate_fails_with_code":
                continue
            queries.extend(
                (f"{singular}.{entry_id}", [spec[key]], spec.get("now"))
                for key in ("query", "metric_query", "expected_query")
                if isinstance(spec.get(key), dict) and spec[key]
            )
    return queries


def walked_pairs(config: PackageConfig, queries: Iterable[PackageQuery]) -> dict[Pair, list[str]]:
    """Each entity pair the queries walk, with the ids of the queries that walk it.

    Each call is bound as a compile binds it, never run. A pair it is refused on as
    ``AMBIGUOUS_PATH`` is walked; the call is then bound again with that pair's first route in
    its own ``routes``, so the pairs past the fork are walked too. When a call is refused for
    another reason, the forks it met still count and its query's next call is tried.
    """
    registry = Registry(config)
    walked: dict[Pair, list[str]] = {}

    def clock(object_id: str) -> str:
        try:
            return _object_default_query_temporal_role(config, object_id)
        except SemanticLayerError:
            return ""

    def note(pair: Pair, query_id: str) -> None:
        if query_id not in walked.setdefault(pair, []):
            walked[pair].append(query_id)

    def walk(query_id: str, call: dict[str, Any], now: Any) -> bool:
        """Walk ``call``; whether it bound past every refusal but a route's."""
        while True:
            try:
                payload, _ = read_call(
                    call,
                    config=config,
                    now=now,
                    default_clock=clock,
                    text_dimension=text_dimension,
                    column_dimensions=dimensions_over_column,
                )
                bound = bind_query(config, registry, payload)
                # The plan's own paths (a group_by's, a measure's) and those lowering read.
                choices = read_routes(bound.plan, bound.route_choices)
            except (TypeError, ValueError):
                # The expression parser raises these for some malformed values; the
                # examples and tests report their own failures.
                return False
            except SemanticLayerError as exc:
                if exc.code != "AMBIGUOUS_PATH":
                    return False
                pair = (str(exc.details.get("start") or ""), str(exc.details.get("target") or ""))
                if not all(pair):
                    return True
                note(pair, query_id)
                options = (exc.details.get("clarification") or {}).get("options") or []
                routes = [row["relationship_path"] for row in options]
                routes = routes or list(exc.details.get("candidates") or [])
                taken = list(call.get("routes") or [])
                if not routes or any((row["from"], row["to"]) == pair for row in taken):
                    return True
                call = {**call, "routes": [*taken, route_entry(route_pin(*pair, routes[0]))]}
                continue
            for start, target, _route in choices:
                note((start, target), query_id)
            return True

    for query_id, calls, now in queries:
        for call in calls:
            if walk(query_id, call, now):
                break
    return walked


def _walks(
    config: PackageConfig, queries: list[PackageQuery], package: str
) -> dict[Pair, list[str]]:
    """``walked_pairs``, reused for the same ``package`` (its source fingerprint) and queries."""
    if not package:
        return walked_pairs(config, queries)
    key = hashlib.sha256(json.dumps([package, queries], default=str).encode()).hexdigest()
    with _WALKS_LOCK:
        if (walked := _WALKS.get(key)) is not None:
            _WALKS.move_to_end(key)
            return walked
    walked = walked_pairs(config, queries)
    with _WALKS_LOCK:
        _WALKS[key] = walked
        while len(_WALKS) > _WALKS_KEPT:
            _WALKS.popitem(last=False)
    return walked


def route_census(
    config: PackageConfig, *, queries: Iterable[PackageQuery] = (), package: str = ""
) -> dict[str, list[dict[str, Any]]]:
    """The census pairs that need a business decision or a crossing disclosure.

    ``undecided``: pairs refused with ``AMBIGUOUS_PATH``, each with the refusal's ``details``
    as raised (its routes, their meanings and the row that records each). ``assumed``: pairs
    answered by the start's own key, with two or more routes, for the author to confirm.
    ``defaults``: pairs answered by a lookup default (``fanout.resolve_path`` rung 5), each with
    the rung that chose it (``by``), its ``meaning`` and its other routes: the questions that
    answer now where an engine without the default refused them. ``pass_through``: resolved only or inherited routes crossing undeclared child rows,
    with the same disclosure as an answer; these are not pairs awaiting a decision.

    Each undecided and pass-through pair lists in ``needed_by`` the ``queries`` that walk it
    (``walked_pairs``): the package's own (``package_queries``) or a caller's, such as the
    questions people asked. Undecided pairs some query needs come first. ``package``, the
    package's source fingerprint, reuses the walks of an earlier census of the same package and
    queries.
    """
    undecided: list[dict[str, Any]] = []
    assumed: list[dict[str, Any]] = []
    defaults: list[dict[str, Any]] = []
    pass_through: list[dict[str, Any]] = []
    for (start, target), outcome in resolve_pairs(config, census_pairs(config)).items():
        ends = {"source_entity": start, "target_entity": target}
        if outcome.refused == "AMBIGUOUS_PATH":
            undecided.append({**ends, "details": outcome.details})
        elif outcome.basis == "colocated_key" and len(outcome.routes) >= 2:
            assumed.append({**ends, "relationship_path": [*outcome.path], "basis": outcome.basis})
        elif outcome.basis == "default":
            defaults.append(
                {
                    **ends,
                    "relationship_path": [*outcome.path],
                    "by": outcome.by,
                    "meaning": route_reading(config, start, outcome.path),
                    "other_routes": [[*path] for path in outcome.routes[1:]],
                }
            )
        elif outcome.basis in {"only_route", "inherited"}:
            disclosure = pass_through_disclosure(
                config, start, target, outcome.path, route_basis=outcome.basis
            )
            if disclosure:
                pass_through.append({**ends, "message": disclosure[0], "details": disclosure[1]})
    walked = _walks(config, list(queries), package) if undecided or pass_through else {}
    for row in [*undecided, *pass_through]:
        row["needed_by"] = list(walked.get((row["source_entity"], row["target_entity"]), []))
    undecided.sort(key=lambda row: not row["needed_by"])
    return {
        "undecided": undecided,
        "assumed": assumed,
        "defaults": defaults,
        "pass_through": pass_through,
    }


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
