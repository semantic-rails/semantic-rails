"""No response names, or answers from, an object hidden from the caller who never asked for it.

For every jaffle_shop object, hidden from the support caller by ``hidden`` and by a
``visible_only`` policy naming finance: discovery, inspect of every visible object, segment
validate/explain, execute validate/sql of every visible metric and measure (and five broken
variants of each metric), build-options, valid-values and eleven plan intents. None of these
requests names the hidden object. Each response (i) contains no token of a hidden object and
(ii) equals the response from the package with the hidden objects absent, leaving out authored
prose (which the visible view omits whole) and policy ids and rationales (generic in the view).

Routes are business definitions the view never re-chooses: a pair of visible entities whose
package route reads a hidden entity or relationship has no route for the caller, where the
absent package might find another one. The test computes those pairs from the package's own
route resolution. A query whose unrestricted binding reads a hidden entity or relationship must
refuse; an inspect of an object on such a pair, and a plan while any exists, may differ from the
absent package (still naming nothing hidden).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.compiler import bind_metadata_objects, bind_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import package_route
from semantic_rails.mcp import _MAX_RESULT_CHARS_ENV
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import (
    ACTIONS,
    CALLER,
    absent,
    envelope,
    hidden_tokens,
    http_call,
    leaks,
    mcp_call,
    object_rows,
    visibility_policy,
    with_policies,
)

TERMS = ("", "orders", "revenue", "customers", "store", "time", "spend")
INTENTS = (
    "revenue by store",
    "how many orders last month",
    "average order value by store",
    "high value customers",
    "lifetime spend by customer",
    "orders with food",
    "monthly revenue",
    "orders per customer type",
    "customers by store opened date",
    "sessions converted last week",
    "inventory by store",
)
# Fields the view omits whole when they name a hidden object, and a policy's own words.
PROSE = frozenset(
    {"description", "topics", "example_entries", "examples", "meta", "operational", "rationale"}
)
GRAPH = ("entities", "relationships")


def _public(value: Any, *, in_effects: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            key: _public(item, in_effects=in_effects or key.endswith("policy_effects"))
            for key, item in value.items()
            if key not in PROSE and not (in_effects and key == "policy_id")
        }
    if isinstance(value, list):
        return [_public(item, in_effects=in_effects) for item in value]
    return value


Request = tuple[str, Callable[[Runtime], Any], Any]


def _requests(config: PackageConfig, visible: set[str], near: set[str]) -> list[Request]:
    """(label, call, what the request itself names) for every request the sweep makes. Objects
    more than one hop from the hidden ones are inspected at minimal verbosity only."""
    out: list[Request] = []

    def mcp(tool: str, arguments: dict[str, Any]) -> Callable[[Runtime], Any]:
        return lambda runtime: mcp_call(runtime, tool, {**arguments, "policy_context": CALLER})

    def http(route: str, body: dict[str, Any]) -> Callable[[Runtime], Any]:
        return lambda runtime: http_call(runtime, route, {**body, "policy_context": CALLER})

    for terms in TERMS:
        for verbosity in ("compact", "full"):
            arguments = {"terms": terms, "verbosity": verbosity, "limit": 50}
            out.append((f"mcp discover {terms!r} {verbosity}", mcp("discover", arguments), terms))
        out.append(
            (f"http /discover {terms!r}", http("/discover", {"terms": terms, "limit": 50}), terms)
        )
    for row in object_rows(config):
        if row.id not in visible:
            continue
        for verbosity in ("minimal", "full") if row.id in near else ("minimal",):
            arguments = {"object_id": row.id, "verbosity": verbosity}
            out.append((f"mcp inspect {row.id} {verbosity}", mcp("inspect", arguments), row.id))
    for segment in config.segments:
        if segment.id not in visible:
            continue
        for action in ("validate", "explain"):
            for verbosity in ("minimal", "full"):
                arguments = {"segment_id": segment.id, "action": action, "verbosity": verbosity}
                out.append(
                    (f"mcp segment {action} {verbosity}", mcp("segment", arguments), segment.id)
                )
            body = {"segment_id": segment.id}
            out.append((f"http /segment-{action}", http(f"/segment-{action}", body), segment.id))
    for kind, rows in (("metric", config.metric_recipes), ("measure", config.measures)):
        for row in rows:
            if row.id not in visible:
                continue
            query = {"select": [{"expression": {kind: row.id}, "as": "value"}]}
            for mode in ("validate", "sql"):
                arguments = {"query": query, "mode": mode}
                out.append((f"mcp execute {mode} {row.id}", mcp("execute", arguments), query))
            body = {"focus_object_id": row.id}
            out.append((f"http /build-options {row.id}", http("/build-options", body), row.id))
    for row in config.metric_recipes:
        if row.id not in visible:
            continue
        base = {"select": [{"expression": {"metric": row.id}, "as": "value"}]}
        broken = {
            "unknown time role": {
                **base,
                "time": {"temporal_role": "temporal_role.nope", "grain": "month"},
            },
            "unknown group_by": {**base, "group_by": ["dimension.nope"]},
            "far group_by": {**base, "group_by": ["dimension.jaffle_supply_name"]},
            "unknown where": {**base, "where": [{"field": "nope", "op": "=", "value": "x"}]},
            "bad grain": {**base, "time": {"temporal_role": "", "grain": "fortnight"}},
        }
        for name, query in broken.items():
            arguments = {"query": query, "mode": "validate"}
            out.append((f"mcp execute validate {name} {row.id}", mcp("execute", arguments), query))
    for row in config.dimensions:
        if row.id in visible:
            arguments = {"dimension_id": row.id}
            out.append((f"mcp valid-values {row.id}", mcp("valid-values", arguments), row.id))
    for intent in INTENTS:
        out.append((f"mcp plan {intent!r}", mcp("plan", {"intent": intent}), intent))
    return out


def _call(request: Request, runtime: Runtime) -> Any:
    try:
        return request[1](runtime)
    except SemanticLayerError as exc:  # never expected: every surface answers with an envelope
        return {"raised": exc.code, "message": str(exc), "details": exc.details}


def _footprint(base: PackageConfig, object_id: str) -> set[str]:
    """What an object's default binding reads, itself included; empty when it cannot bind."""
    try:
        return {object_id, *bind_metadata_objects(base, [object_id])}
    except Exception:  # noqa: BLE001 — an unbindable object has no footprint to share
        return {object_id}


def _near(base: PackageConfig, hidden: set[str]) -> set[str]:
    """Objects one hop from the hidden ones: sharing a read with one of them."""
    reach = set().union(*(_footprint(base, object_id) for object_id in hidden))
    return {row.id for row in object_rows(base) if _footprint(base, row.id) & reach}


def _hidden_routed(base: PackageConfig, hidden: set[str]) -> set[str]:
    """Visible entities on a pair whose package route reads a hidden entity or relationship."""
    graph = {row.id for name in GRAPH for row in getattr(base, name)} & hidden
    if not graph:
        return set()
    ends = {row.id: {row.source_entity, row.target_entity} for row in base.relationships}
    entities = [row.id for row in base.entities if row.id not in hidden]
    out: set[str] = set()
    for start in entities:
        for target in entities:
            if start == target:
                continue
            try:
                route = package_route(base, start=start, target=target).routes[0]
                reads = {*route, *(entity for step in route for entity in ends[step])}
            except SemanticLayerError as exc:
                reads = {
                    object_id
                    for object_id in graph
                    if json.dumps(object_id) in json.dumps(exc.details, default=str)
                }
            if reads & graph:
                out.update((start, target))
    return out


def _route_dependent(base: PackageConfig, label: str, named: Any, routed: set[str]) -> bool:
    """An inspect of an object reading an entity on a hidden-routed pair, or a plan while one
    exists: the package's routes for it are not the absent package's."""
    if not routed:
        return False
    if label.startswith("mcp plan"):
        return True
    return label.startswith("mcp inspect") and bool(_footprint(base, str(named)) & routed)


def _reads_hidden_graph(base: PackageConfig, request: Any, hidden: set[str]) -> bool:
    """Whether an unrestricted caller's binding of a query request reads a hidden entity or
    relationship, or refuses over a route through one: routes are business definitions the
    view never re-chooses, so these refuse naming nothing instead."""
    if not isinstance(request, dict) or "select" not in request:
        return False
    graph = {row.id for name in GRAPH for row in getattr(base, name)} & hidden
    try:
        return bool(bind_query(base, None, request).object_ids & graph)
    except SemanticLayerError as exc:
        details = json.dumps(exc.details, default=str)
        return any(json.dumps(object_id) in details for object_id in graph)
    except Exception:  # noqa: BLE001 — an internal error, which the absent package shares
        return False


@pytest.fixture(autouse=True)
def _whole_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compare whole payloads, never a budget refusal's size: that size counts timings."""
    monkeypatch.setenv(_MAX_RESULT_CHARS_ENV, "100000000")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("sweep"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


TARGETS = [row.id for row in object_rows(load_package_config("configs/semantic_rails/jaffle_shop"))]


def test_the_sweep_covers_every_object_of_every_kind():
    config = load_package_config("configs/semantic_rails/jaffle_shop")
    assert len(TARGETS) == len(set(TARGETS)) == len(object_rows(config))
    assert {type(row).__name__ for row in object_rows(config)} == {
        "EntityConfig",
        "DimensionConfig",
        "TemporalRoleConfig",
        "RelationshipConfig",
        "ValueDomainConfig",
        "MeasureConfig",
        "MetricConfig",
        "SegmentConfig",
    }


@pytest.mark.parametrize("target", TARGETS)
def test_no_response_names_or_answers_from_a_hidden_object(package, target):
    try:  # WIP: the base engine's set, to record the failures there
        from semantic_rails.visible_view import hidden_object_ids
    except ImportError:
        from semantic_rails.policies import hidden_object_ids

    root, config = package
    started = time.perf_counter()
    governed = {
        action: with_policies(config, visibility_policy(action, target)) for action in ACTIONS
    }
    hidden_sets = {
        action: hidden_object_ids(governed[action], roles=["support"]) for action in ACTIONS
    }
    # One rule for both actions, so one absent package answers for both.
    assert hidden_sets["hidden"] == hidden_sets["visible_only"]
    hidden = set(hidden_sets["hidden"])
    assert target in hidden
    visible = {row.id for row in object_rows(config)} - hidden
    requests = _requests(config, visible, _near(config, hidden))
    tokens = hidden_tokens(config, hidden)
    missing = Runtime.from_config(absent(config, hidden), source_path=str(root))
    try:
        expected = {request[0]: _public(envelope(_call(request, missing))) for request in requests}
    finally:
        missing.close()
    exceptions = {
        label for label, _, named in requests if _reads_hidden_graph(config, named, hidden)
    }
    routed = _hidden_routed(config, hidden)
    rerouted = {
        label for label, _, named in requests if _route_dependent(config, label, named, routed)
    }
    failures: list[str] = []
    for action in ACTIONS:
        runtime = Runtime.from_config(governed[action], source_path=str(root))
        try:
            for request in requests:
                label, _, named = request
                response = _call(request, runtime)
                found = leaks(response, tokens, request=named)
                if found:
                    failures.append(f"{action} {label}: names {found}")
                if label in exceptions:
                    if response.get("ok") is not False:
                        failures.append(f"{action} {label}: answered through a hidden route")
                    continue
                if label in rerouted:
                    continue
                if _public(envelope(response)) != expected[label]:
                    failures.append(f"{action} {label}: differs from the absent package")
        finally:
            runtime.close()
    assert not failures, "\n".join(failures[:40])
    # Recorded for the cost budget; a case is expected to stay within seconds.
    assert time.perf_counter() - started < 120
