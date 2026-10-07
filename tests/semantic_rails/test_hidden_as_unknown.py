"""An object hidden from the caller is refused exactly like one that does not exist.

Each request names a jaffle object hidden from the caller by a ``hidden`` or a ``visible_only``
policy, through every MCP tool and HTTP operation that takes an id. Its response must equal,
field for field, the response to the same request where that object carries a fresh random id
instead, so the caller's id names nothing: same code, message, details and suggestions. A
visible object that is denied keeps ``POLICY_DENIED``, and an eligible caller still answers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import inspect_payload
from semantic_rails.request_context import context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig, SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config, opened

CALLER = {"roles": ["support"]}
CUSTOMERS = "metric.sales.customer_count"
ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
REVENUE = "measure.jaffle.revenue_usd"
AOV = "metric.sales.aov_usd"
LIFETIME_SPEND = "measure.jaffle.lifetime_spend_usd"
SEGMENT = "segment.jaffle.high_value_customers"
OBJECTS = {
    "metric": CUSTOMERS,
    "measure": ORDERS,
    "dimension": STORE,
    "temporal_role": "temporal_role.jaffle_order_time",
    "entity": "entity.jaffle_store",
    "segment": SEGMENT,
}
QUERIED = ("metric", "measure", "dimension", "temporal_role", "entity")
SELECTED = ("metric", "measure")  # the part of a seed query discover and plan resolve
ANY = tuple(OBJECTS)
Call = Callable[[Runtime, str, str], dict[str, Any]]


def _query(kind: str, object_id: str) -> dict[str, Any]:
    """A query naming ``object_id`` where an id of ``kind`` goes."""
    if kind in SELECTED:
        return {"select": [{"expression": {kind: object_id}, "as": "value"}]}
    query: dict[str, Any] = {"select": [{"expression": {"metric": CUSTOMERS}, "as": "value"}]}
    if kind == "dimension":
        return {**query, "group_by": [object_id]}
    if kind == "temporal_role":
        return {**query, "time": {"temporal_role": object_id, "grain": "month"}}
    predicate = {"kind": "metric_predicate", "entity": object_id, "scope_mode": "entity_only"}
    predicate |= {"input": {"measure": ORDERS}, "op": ">=", "value": 1}
    return {**query, "metric_filters": [{"expression": predicate, "op": "=", "value": True}]}


def _arguments(kind: str, object_id: str, template: dict[str, Any]) -> dict[str, Any]:
    """``template`` with ``{id}`` filled in and ``query`` built; the caller's context added."""
    filled = {key: value.format(id=object_id) for key, value in template.items()}
    if "query" in filled:
        filled["query"] = _query(kind, object_id)
    return {**filled, "policy_context": CALLER}


def _mcp(tool: str, **template: Any) -> Call:
    def call(runtime: Runtime, kind: str, object_id: str) -> dict[str, Any]:
        adapter = SemanticLayerMCPAdapter(runtime)
        return adapter.call_tool(tool, _arguments(kind, object_id, template))

    return call


def _http(route: str, **template: Any) -> Call:
    def call(runtime: Runtime, kind: str, object_id: str) -> dict[str, Any]:
        service = SemanticHTTPService(runtime)
        try:
            response, status = service.handle("POST", route, _arguments(kind, object_id, template))
        except SemanticLayerError as exc:
            context = context_from_policy_context(CALLER)
            response, status = service.exception_payload(exc, stage="http", context=context)
        return {**response, "http_status": status}

    return call


# surface -> (the kinds of id it takes, call)
SURFACES: dict[str, tuple[tuple[str, ...], Call]] = {
    **{
        f"mcp execute {mode}": (QUERIED, _mcp("execute", mode=mode, query=""))
        for mode in ("run", "validate", "sql")
    },
    "mcp plan": (SELECTED, _mcp("plan", intent="how many", query="")),
    "mcp discover": (SELECTED, _mcp("discover", terms="customers", query="")),
    "mcp inspect": (ANY, _mcp("inspect", object_id="{id}")),
    "mcp valid-values": (ANY, _mcp("valid-values", dimension_id="{id}")),
    **{
        f"mcp segment {action}": (ANY, _mcp("segment", segment_id="{id}", action=action))
        for action in ("validate", "explain", "preview")
    },
    **{
        f"http {route}": (QUERIED, _http(route, query=""))
        for route in ("/validate", "/compile", "/query")
    },
    "http /plan": (SELECTED, _http("/plan", intent="how many", query="")),
    "http /discover": (SELECTED, _http("/discover", terms="customers", query="")),
    "http /inspect": (ANY, _http("/inspect", object_id="{id}")),
    "http /valid-values": (ANY, _http("/valid-values", dimension_id="{id}")),
    "http /build-options": (ANY, _http("/build-options", focus_object_id="{id}")),
    **{
        f"http /segment-{action}": (ANY, _http(f"/segment-{action}", segment_id="{id}"))
        for action in ("validate", "explain", "preview")
    },
}
CASES = [
    (surface, kind) for surface, (kinds, _) in SURFACES.items() for kind in OBJECTS if kind in kinds
]


def _policy(action: str, *object_ids: str) -> SemanticPolicyConfig:
    kind = "object_visibility" if action in {"hidden", "visible_only"} else "object_access"
    # visible_only names whom its objects are visible to: finance, never the support caller.
    roles = ["finance"] if action == "visible_only" else ["support"]
    return SemanticPolicyConfig(
        id=f"policy.test.{action}", kind=kind, action=action, object_ids=[*object_ids], roles=roles
    )


def _renamed(value: Any, old: str, new: str) -> Any:
    """``value`` with every reference to the id ``old`` made to ``new``."""
    if isinstance(value, str):
        return new if value == old else value
    if is_dataclass(value) and not isinstance(value, type):
        changed = {row.name: _renamed(getattr(value, row.name), old, new) for row in fields(value)}
        return replace(value, **{row.name: changed[row.name] for row in fields(value) if row.init})
    if isinstance(value, list | tuple | set | frozenset):
        return type(value)(_renamed(item, old, new) for item in value)
    if isinstance(value, dict):
        return {_renamed(key, old, new): _renamed(item, old, new) for key, item in value.items()}
    return value


def _envelope(response: dict[str, Any]) -> dict[str, Any]:
    """The response without its per-call request id and timings."""
    if isinstance(response, dict):
        return {
            key: _envelope(value)
            for key, value in response.items()
            if key not in {"request_id", "timing_ms", "cache_lookup_ms"}
        }
    return [_envelope(item) for item in response] if isinstance(response, list) else response


def _codes(response: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in response.get("errors", [])]


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("hidden"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


FOOD = "dimension.jaffle_order_has_food_item"
CONSTRAIN = SemanticPolicyConfig(
    id="policy.test.constrain",
    kind="metric_constraint",
    object_ids=[CUSTOMERS, REVENUE],
    roles=["support"],
    config={"allowed_group_by": [STORE, FOOD]},
)


def _deny(*object_ids: str) -> SemanticPolicyConfig:
    return replace(_policy("deny", *object_ids), id="policy.test.deny")


@pytest.fixture(scope="module")
def governed(
    package: tuple[Path, PackageConfig],
) -> Iterator[Callable[..., Runtime]]:
    """The package with extra policies, cached for refusals (none reaches the warehouse).

    ``fresh`` first gives that object a fresh random id, in the package and the policies.
    """
    root, config = package
    made: dict[str, Runtime] = {}

    def runtime(*policies: SemanticPolicyConfig, fresh: str = "") -> Runtime:
        key = repr((policies, fresh))
        if key not in made:
            base, extra = config, list(policies)
            if fresh:
                new = f"{fresh.split('.')[0]}.{uuid.uuid4().hex}"
                base, extra = _renamed(config, fresh, new), _renamed(extra, fresh, new)
            with_policies = replace(base, semantic_policies=[*base.semantic_policies, *extra])
            made[key] = Runtime.from_config(with_policies, source_path=str(root))
        return made[key]

    yield runtime
    for runtime in made.values():
        runtime.close()


@pytest.mark.parametrize(("surface", "kind"), CASES)
@pytest.mark.parametrize("action", ["hidden", "visible_only"])
def test_a_hidden_id_gets_the_response_of_an_id_that_names_nothing(governed, action, surface, kind):
    object_id = OBJECTS[kind]
    _, call = SURFACES[surface]
    policy = _policy(action, object_id)
    hidden = call(governed(policy), kind, object_id)
    absent = call(governed(policy, fresh=object_id), kind, object_id)
    assert hidden["ok"] is False, hidden
    assert "POLICY_DENIED" not in _codes(hidden), hidden
    assert _envelope(hidden) == _envelope(absent)


@pytest.mark.parametrize("action", ["hidden", "visible_only"])
def test_a_query_naming_a_denied_and_a_hidden_object_reports_only_the_hidden_one(governed, action):
    """The visible object's denial is not reported: the hidden one is unknown, as if absent."""
    query = {
        "select": [
            {"expression": {"measure": ORDERS}, "as": "orders"},
            {"expression": {"metric": CUSTOMERS}, "as": "customers"},
        ],
        "policy_context": CALLER,
    }
    policies = (_deny(ORDERS), _policy(action, CUSTOMERS))
    hidden = governed(*policies).validate(query)
    absent = governed(*policies, fresh=CUSTOMERS).validate(query)
    assert _codes(hidden) == ["OBJECT_NOT_FOUND"]
    assert hidden["policy_effects"] == []
    assert _envelope(hidden) == _envelope(absent)


def test_a_visible_denied_object_keeps_its_policy_denial(governed):
    """``deny`` governs an object the caller can see: the refusal names it and its policy."""
    runtime = governed(_deny(ORDERS))
    query = {**_query("measure", ORDERS), "policy_context": CALLER}
    assert _codes(runtime.validate(query)) == ["POLICY_DENIED"]
    with pytest.raises(SemanticLayerError) as raised:
        runtime.compile(query)
    assert raised.value.code == "POLICY_DENIED"
    assert raised.value.details["blocked_objects"] == [ORDERS]
    assert [row["policy_id"] for row in raised.value.details["policy_effects"]] == [
        "policy.test.deny"
    ]


@pytest.mark.parametrize("action", ["hidden", "visible_only"])
def test_an_eligible_caller_still_answers(package, action):
    """visible_only names finance as eligible; hidden names only the support role."""
    root, config = package
    query = {**_query("metric", CUSTOMERS), "group_by": [STORE]}
    query["policy_context"] = {"roles": ["finance"]}
    rows = []
    for policies in ([], [_policy(action, CUSTOMERS)], [_policy(action, STORE)]):
        with_policies = replace(config, semantic_policies=[*config.semantic_policies, *policies])
        runtime = opened(Runtime.from_config(with_policies, source_path=str(root)))
        try:
            rows.append(sorted(runtime.query(query)["rows"], key=lambda row: row[STORE]))
        finally:
            runtime.close()
    assert rows[0]
    assert rows[1] == rows[2] == rows[0]


def test_forcing_the_binder_past_its_refusal_still_refuses_as_unknown(governed, monkeypatch):
    """The query policy gate refuses a named hidden id itself, never as POLICY_DENIED."""
    query = {**_query("metric", CUSTOMERS), "policy_context": CALLER}
    runtime = governed(_policy("hidden", CUSTOMERS))
    expected = runtime.validate(query)["errors"]
    monkeypatch.setattr("semantic_rails.runtime.refuse_as_unknown", lambda *args: None)
    assert runtime.validate(query)["errors"] == expected
    assert [issue["code"] for issue in expected] == ["OBJECT_NOT_FOUND"]
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query)
    assert (raised.value.code, str(raised.value)) == (
        "OBJECT_NOT_FOUND",
        f"Unknown metric recipe '{CUSTOMERS}'",
    )


@pytest.mark.parametrize(
    ("action", "code"), [("hidden", "POLICY_DENIED"), ("visible_only", "OBJECT_NOT_FOUND")]
)
def test_an_object_read_through_a_named_one_is_never_named(governed, action, code):
    """A metric and a segment reading a hidden measure. ``hidden`` keeps them visible, so
    they are denied; ``visible_only`` hides them too, so they are unknown. Either way the
    refusal names only the caller's own id."""
    query = {**_query("metric", AOV), "policy_context": CALLER}
    report = governed(_policy(action, REVENUE)).validate(query)
    segment = governed(_policy(action, LIFETIME_SPEND)).segment_validate(
        SEGMENT, policy_context=CALLER
    )
    assert _codes(report) == _codes(segment) == [code]
    assert REVENUE not in json.dumps(report)
    assert LIFETIME_SPEND not in json.dumps(segment["errors"])


def test_policy_effects_never_name_a_hidden_object(governed):
    """Policies listing a visible and a hidden object report only the visible one."""
    runtime = governed(_policy("hidden", REVENUE, FOOD), _deny(ORDERS, REVENUE), CONSTRAIN)
    refusal = runtime.validate({**_query("measure", ORDERS), "policy_context": CALLER})
    compiled = runtime.compile(
        {**_query("metric", CUSTOMERS), "group_by": [STORE], "policy_context": CALLER}
    )
    card = inspect_payload(runtime, object_id=CUSTOMERS, partial_query={"policy_context": CALLER})
    assert refusal["errors"][0]["details"]["blocked_objects"] == [ORDERS]
    effects = {row["policy_id"]: row for row in compiled["policy_effects"]}
    assert effects["policy.test.constrain"]["constraints"] == {"allowed_group_by": [STORE]}
    for payload in (refusal, compiled["policy_effects"], card):
        assert REVENUE not in json.dumps(payload), payload
        assert FOOD not in json.dumps(payload), payload
