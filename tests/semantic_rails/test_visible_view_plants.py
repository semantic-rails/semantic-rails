"""No authored text naming a hidden object reaches its caller, whichever field holds it.

The test plants ``"<hidden id> via <slot>"`` (in a second pass, the hidden object's unique
label) into every authored slot of every visible record and of the package: each text, list
and mapping field the engine does not read (``hidden_absent.ENGINE_READ``), found by
introspecting the records, independently of the engine's projection table. A caller the object
is hidden from then calls every surface. No response names it, every response is plain JSON,
and the calls answer (a run where every call refuses would prove nothing). Rows and the export
hint are the unrestricted caller's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MeasureExternalDiscontinuity, MeasureValidityWindow, PackageConfig
from semantic_rails.visible_view import hidden_object_ids
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import (
    ACTIONS,
    CALLER,
    ENGINE_READ,
    OBJECT_FIELDS,
    access_policy,
    decoded_strings,
    http_call,
    mcp_call,
    object_rows,
    outcome,
    visibility_policy,
    with_policies,
)

# A measure, and a relationship on the route from orders to stores.
TARGETS = ("measure.jaffle.revenue_usd", "relationship.orders_store")
# Non-ASCII and quoted, so JSON text would escape it: the scan reads decoded strings.
LABEL = 'Salaires "privés"'
NAMES = ("name", "label", "aliases")
REFUSED = ("INVALID_CONFIG", "visibility_unresolved")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("plants"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


def _plant(row: Any, path: str, text: Callable[[str], str], *, names: bool = True) -> Any:
    """``row`` with ``text(slot)`` added to each authored slot: appended to a text, added to a
    list, and as a ``note`` entry of a mapping (and of each mapping in a list)."""
    changes: dict[str, Any] = {}
    for item in fields(row):
        value, slot = getattr(row, item.name), f"{path}.{item.name}"
        kind = str(item.type)
        if item.name in ENGINE_READ or (not names and item.name in NAMES):
            continue
        if isinstance(value, str):
            changes[item.name] = f"{value} {text(slot)}".strip()
        elif is_dataclass(value):
            changes[item.name] = _plant(value, slot, text)
        elif isinstance(value, list) and value and all(is_dataclass(one) for one in value):
            changes[item.name] = [
                _plant(one, f"{slot}[{index}]", text) for index, one in enumerate(value)
            ]
        elif isinstance(value, list) and kind.startswith("list[dict"):
            changes[item.name] = [*value, {"note": text(slot)}]
        elif isinstance(value, list) and kind.startswith("list[str"):
            changes[item.name] = [*value, text(slot)]
        # Whether a join is historical is whether it declares validity: never add one.
        elif isinstance(value, dict) and (value or item.name != "temporal_validity"):
            changes[item.name] = {**value, "note": text(slot)}
    return replace(row, **changes)


def _planted(config: PackageConfig, target: str, mode: str) -> PackageConfig:
    """The package with every visible authored slot naming ``target``: by id, or by its
    label (left out of names: a visible object named like a hidden one shares the name)."""
    config = with_policies(config, visibility_policy("hidden", target))
    hidden = hidden_object_ids(config, roles=["support"])
    if mode == "id":

        def text(slot: str) -> str:
            return f"{target} via {slot}"
    else:

        def text(slot: str) -> str:
            return f"{LABEL} via {slot}"

    measure, other = [row for row in config.measures if row.id not in hidden][:2]
    config = replace(
        config,
        measures=[
            replace(
                row,
                meta={**row.meta, "mnpi": True},
                validity_windows=[MeasureValidityWindow("2001-01-01", "2002-01-01", "")],
                external_discontinuities=[MeasureExternalDiscontinuity("2001-01-01", "", "")],
            )
            if row.id == measure.id
            else row
            for row in config.measures
        ],
    )
    names = mode == "id"
    rows = {
        name: [
            replace(row, label=LABEL)
            if row.id == target
            else row
            if row.id in hidden
            else _plant(row, f"{name}[{row.id}]", text, names=names)
            for row in getattr(config, name)
        ]
        for name in OBJECT_FIELDS
    }
    note = access_policy(
        "withhold_values", other.id, id="policy.test.note", rationale=text("policy")
    )
    note = replace(note, config={"max_rank": 3})
    return replace(
        config,
        **rows,
        package=_plant(config.package, "package", text),
        path_preferences=[_plant(row, "path_preferences", text) for row in config.path_preferences],
        aggregate_relations=[
            _plant(row, "aggregate_relations", text) for row in config.aggregate_relations
        ],
        relations=[_plant(row, "relations", text) for row in config.relations],
        semantic_caveats=[_plant(row, "caveats", text) for row in config.semantic_caveats],
        semantic_policies=[
            *config.semantic_policies,
            note,
        ],
        operational_contract={**config.operational_contract, "note": text("operational")},
        meta_contract={**config.meta_contract, "note": text("meta_contract")},
    )


def _calls(config: PackageConfig, hidden: frozenset[str]) -> dict[str, Callable[[Runtime], Any]]:
    visible = [row for row in object_rows(config) if row.id not in hidden]
    metric = next(
        row.id
        for row in config.metric_recipes
        if row.id not in hidden and row.kind not in {"conversion", "ratio"}
    )
    measure = next(row.id for row in config.measures if row.id not in hidden)
    query = {"select": [{"expression": {"metric": metric}, "as": "value"}]}
    calls: dict[str, Callable[[Runtime], Any]] = {}

    def mcp(tool: str, **arguments: Any) -> Callable[[Runtime], Any]:
        return lambda runtime: mcp_call(runtime, tool, {**arguments, "policy_context": CALLER})

    def http(route: str, **body: Any) -> Callable[[Runtime], Any]:
        return lambda runtime: http_call(runtime, route, {**body, "policy_context": CALLER})

    def get(route: str, **params: Any) -> Callable[[Runtime], Any]:
        def call(runtime: Runtime) -> Any:
            service = SemanticHTTPService(runtime)
            caller = RequestContext(roles=("support",))
            return service.handle("GET", route, query_params=params, context=caller)[0]

        return call

    for verbosity in ("compact", "full"):
        calls[f"mcp discover {verbosity}"] = mcp("discover", terms="", verbosity=verbosity)
    calls["http /discover"] = http("/discover", terms="", limit=500)
    for verbosity in ("minimal", "compact", "full"):
        for view in ("summary", "full"):
            calls[f"http /catalog {view} {verbosity}"] = http(
                "/catalog", view=view, verbosity=verbosity
            )
        calls[f"get /catalog {verbosity}"] = get("/catalog", verbosity=verbosity)
    calls["get /capabilities"] = get("/capabilities")
    for row in visible:
        calls[f"mcp inspect {row.id}"] = mcp("inspect", object_id=row.id, verbosity="full")
    calls[f"http /inspect {measure}"] = http("/inspect", object_id=measure)
    for row in config.dimensions:
        if row.id not in hidden:
            calls[f"mcp valid-values {row.id}"] = mcp("valid-values", dimension_id=row.id)
    for focus in (measure, metric):
        calls[f"http /build-options {focus}"] = http("/build-options", focus_object_id=focus)
    for row in config.segments:
        if row.id not in hidden:
            for action in ("validate", "explain"):
                calls[f"mcp segment {action} {row.id}"] = mcp(
                    "segment", segment_id=row.id, action=action, verbosity="full"
                )
                calls[f"http /segment-{action} {row.id}"] = http(
                    f"/segment-{action}", segment_id=row.id
                )
    variants = {
        "": query,
        "export": {**query, "export": True},
        "unknown group_by": {**query, "group_by": ["dimension.nope"]},
        "unknown where": {**query, "where": [{"field": "nope", "op": "=", "value": "x"}]},
        "bad grain": {**query, "time": {"temporal_role": "", "grain": "fortnight"}},
        "misspelt metric": {"select": [{"expression": {"metric": f"{metric}x"}, "as": "v"}]},
        "by store": {**query, "group_by": ["dimension.jaffle_store_name"]},
    }
    for name, variant in variants.items():
        for mode in ("run", "validate", "sql"):
            calls[f"mcp execute {mode} {name}"] = mcp(
                "execute", query=variant, mode=mode, verbosity="full"
            )
        for route in ("/validate", "/compile", "/query"):
            calls[f"http {route} {name}"] = http(route, **variant)
    for intent in ("revenue by store", "how many orders last month", "average order value"):
        calls[f"mcp plan {intent}"] = mcp("plan", intent=intent)
    return calls


def _codes(response: Any) -> set[str]:
    """Every error code and refusal reason in a response."""
    text = json.dumps(response)
    return set(re.findall(r'"(?:code|raised|reason)": "([A-Za-z_]+)"', text))


@pytest.mark.parametrize("mode", ["id", "label"])
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("action", ACTIONS)
def test_no_planted_slot_reaches_a_caller(package, action, target, mode):
    root, config = package
    planted = _planted(config, target, mode)
    if action == "visible_only":
        policies = [row for row in planted.semantic_policies if row.kind != "object_visibility"]
        planted = replace(planted, semantic_policies=[*policies, visibility_policy(action, target)])
    hidden = hidden_object_ids(planted, roles=["support"])
    assert target in hidden
    token = re.escape(target) if mode == "id" else f"(?i:{re.escape(LABEL)})"
    found = re.compile(token + r"(?: via ([\w.\[\]]+))?")
    runtime = Runtime.from_config(planted, source_path=str(root))
    leaks: dict[str, list[str]] = {}
    refused: list[str] = []
    try:
        calls = _calls(planted, hidden)
        for label, call in calls.items():
            response = outcome(lambda call=call: call(runtime))
            json.dumps(response)  # plain JSON: strict, no default=str
            for text in decoded_strings(response):
                for match in found.finditer(text):
                    leaks.setdefault(match.group(1) or "<bare>", []).append(label)
            if _codes(response) & set(REFUSED):
                refused.append(label)
        # Enforcement is the same caller's without the visibility policy: rows, export hint.
        policies = [row for row in planted.semantic_policies if row.kind != "object_visibility"]
        base = replace(planted, semantic_policies=policies)
        base_runtime = Runtime.from_config(base, source_path=str(root))
        try:
            tagged = next(
                row.id for row in planted.measures if row.id not in hidden and row.meta.get("mnpi")
            )
            query = {"select": [{"expression": {"measure": tagged}, "as": "value"}]}
            mine = runtime.query({**query, "policy_context": CALLER})
            theirs = base_runtime.query({**query, "policy_context": CALLER})
            assert mine["rows"] == theirs["rows"]
            exported = {**query, "export": True}
            hints = [
                {row["code"] for row in out["methodology_hints"]}
                for out in (
                    runtime.validate({**exported, "policy_context": CALLER}),
                    base_runtime.validate({**exported, "policy_context": CALLER}),
                )
            ]
            assert hints[0] == hints[1]
            assert "MNPI_BULK_EXPORT_DISCOURAGED" in hints[0]
        finally:
            base_runtime.close()
    finally:
        runtime.close()
    assert not leaks, "\n".join(
        f"{slot}: {sorted(set(where))[:3]}" for slot, where in leaks.items()
    )
    assert len(refused) <= len(calls) // 10, refused[:10]
