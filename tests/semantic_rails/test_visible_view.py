"""Properties of the per-caller visible view.

A caller's requests read the package without the objects hidden from them; enforcement reads the
whole package. So visibility never changes a deny, withhold, row filter or constraint, caches
never carry one caller's view to another, an uncertain hidden set refuses before any warehouse
call, and a view never keeps an object that reads or names a hidden one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.catalog_service import resolve_catalog
from semantic_rails.config import load_package_config
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import visible_route
from semantic_rails.manifest import write_manifest
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import handle_jsonrpc_message
from semantic_rails.request_context import TrustedAttributes
from semantic_rails.runtime import Runtime, runtime_request_scope
from semantic_rails.schema import PackageConfig, RelationshipConfig, SemanticPolicyConfig
from tests.semantic_rails import test_route_clarification as diamond
from tests.semantic_rails import test_route_precedence as precedence
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.hidden_absent import (
    ACTIONS,
    CALLER,
    absent,
    access_policy,
    declared_references,
    envelope,
    hidden_tokens,
    http_call,
    leaks,
    mcp_call,
    object_rows,
    outcome,
    visibility_policy,
    with_policies,
)

CUSTOMERS = "metric.sales.customer_count"
AOV = "metric.sales.aov_usd"
ORDERS = "measure.jaffle.order_count"
REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
FOOD = "dimension.jaffle_order_has_food_item"
SEGMENT = "segment.jaffle.high_value_customers"
BY_STORE = {
    "select": [{"expression": {"metric": CUSTOMERS}, "as": "value"}],
    "group_by": [STORE],
}
RANKED = {**BY_STORE, "order_by": [{"field": "value", "direction": "desc"}], "limit": 3}
ORDERS_BY_STORE = {
    "select": [{"expression": {"measure": ORDERS}, "as": "value"}],
    "group_by": [STORE],
}
ROW_FILTER = SemanticPolicyConfig(
    id="policy.test.rows",
    kind="row_filter",
    config={"dimension": FOOD, "attribute": "has_food"},
)
# enforcement -> (its policy on what the query reads, the query)
ENFORCEMENT = {
    "deny": (access_policy("deny", CUSTOMERS), BY_STORE),
    "withhold_values": (
        replace(access_policy("withhold_values", CUSTOMERS), config={"max_rank": 3}),
        RANKED,
    ),
    "row_filter": (ROW_FILTER, ORDERS_BY_STORE),
    "metric_constraint": (
        SemanticPolicyConfig(
            id="policy.test.constrain",
            kind="metric_constraint",
            object_ids=[CUSTOMERS],
            roles=["support"],
            config={"allowed_group_by": [CUSTOMER_TYPE]},
        ),
        BY_STORE,
    ),
}
FOOD_CALLER = {"roles": ["support"], "attributes": TrustedAttributes({"has_food": True})}


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("view"), "jaffle_shop", preseed_db=True)
    return root, load_package_config(str(root))


@pytest.fixture(scope="module")
def runtimes(package: tuple[Path, PackageConfig]) -> Iterator[Callable[..., Runtime]]:
    root, config = package
    made: dict[str, Runtime] = {}

    def runtime(*policies: SemanticPolicyConfig, hide: frozenset[str] | None = None) -> Runtime:
        key = repr((policies, sorted(hide) if hide is not None else None))
        if key not in made:
            governed = with_policies(config, *policies)
            if hide is not None:
                governed = absent(governed, hide)
            made[key] = Runtime.from_config(governed, source_path=str(root))
        # Each test opens (and its teardown closes) its own warehouse connection.
        made[key].close()
        return made[key]

    yield runtime
    for value in made.values():
        value.close()


def _enforced(runtime: Runtime, enforcement: str, surface: str, query: dict[str, Any]) -> Any:
    context = FOOD_CALLER if enforcement == "row_filter" else CALLER
    payload = {**query, "policy_context": context, "verbosity": "full"}
    if surface == "http /query":
        return http_call(runtime, "/query", payload)
    if enforcement == "row_filter":
        # Trusted attributes come from the host, never from a tool argument.
        method = {"run": runtime.query, "validate": runtime.validate, "sql": runtime.compile}
        return outcome(lambda: method[surface](payload))
    arguments = {"query": query, "mode": surface, "verbosity": "full", "policy_context": context}
    return mcp_call(runtime, "execute", arguments)


@pytest.mark.parametrize("surface", ["run", "validate", "sql", "http /query"])
@pytest.mark.parametrize("visibility", ACTIONS)
@pytest.mark.parametrize("enforcement", list(ENFORCEMENT))
def test_visibility_is_invisible_to_enforcement(runtimes, enforcement, visibility, surface):
    if enforcement == "row_filter" and surface == "http /query":
        pytest.skip("HTTP callers cannot carry trusted attributes in the body")
    policy, query = ENFORCEMENT[enforcement]
    alone = _enforced(runtimes(policy), enforcement, surface, query)
    with_visibility = _enforced(
        runtimes(policy, visibility_policy(visibility, REVENUE)), enforcement, surface, query
    )
    assert envelope(with_visibility) == envelope(alone)
    text = json.dumps(alone, default=str)
    if enforcement in {"deny", "metric_constraint"}:
        assert "POLICY_DENIED" in text
    if enforcement == "withhold_values":
        assert '"withheld"' in text


def test_enforcement_never_takes_a_view(runtimes):
    from semantic_rails.policies import (
        enforce_query_policies,
        row_filters_for_context,
        withheld_measure_ids,
        withheld_rank_order,
    )

    runtime = runtimes(visibility_policy("hidden", REVENUE))
    view = runtime.view_for(CALLER)
    assert view is not runtime.package_config
    binding = None
    calls = {
        "enforce_query_policies": lambda: enforce_query_policies(
            view, [CUSTOMERS], roles=["support"]
        ),
        "row_filters_for_context": lambda: row_filters_for_context(view, CALLER),
        "withheld_measure_ids": lambda: withheld_measure_ids(view, roles=["support"]),
        "withheld_rank_order": lambda: withheld_rank_order(
            view, binding, rebind=lambda query: binding, roles=["support"]
        ),
    }
    for name, call in calls.items():
        with pytest.raises(TypeError, match="visible view"):
            call()
        assert name


# One runtime alternating callers must answer each exactly as a fresh runtime does.
ALTERNATING = {
    "restricted_a": {"roles": ["support"]},
    "eligible": {"roles": ["finance"]},
    "no_roles": {},
    "restricted_b": {"roles": ["auditor"]},
}
ROUNDS = ["restricted_a", "eligible", "no_roles", "restricted_a", "restricted_b"]


def _surfaces(runtime: Runtime, context: dict[str, Any]) -> dict[str, Any]:
    adapter = SemanticLayerMCPAdapter(runtime)

    def tool(name: str, **arguments: Any) -> Any:
        return adapter.call_tool(name, {**arguments, "policy_context": context})

    aov_by_store = {"select": [{"expression": {"metric": AOV}, "as": "aov"}], "group_by": [STORE]}
    return {
        "catalog": outcome(lambda: resolve_catalog(runtime, policy_context=context)),
        "catalog_full": outcome(
            lambda: resolve_catalog(
                runtime, view="detailed", verbosity="full", policy_context=context
            )
        ),
        "discover": tool("discover", terms="revenue", verbosity="full", limit=20),
        "inspect_aov": tool("inspect", object_id=AOV, verbosity="full"),
        "inspect_store": tool("inspect", object_id=STORE, verbosity="full"),
        "plan": tool("plan", intent="revenue by store"),
        "validate": tool("execute", query=aov_by_store, mode="validate", verbosity="full"),
        "sql": tool("execute", query=aov_by_store, mode="sql", verbosity="full"),
        "valid_values": tool("valid-values", dimension_id=STORE),
        "segment": tool("segment", segment_id=SEGMENT, action="validate", verbosity="full"),
    }


@pytest.fixture(scope="module")
def on_disk(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A package whose policies hide revenue outside finance and the store name from auditors."""
    root = copy_package_config(tmp_path_factory.mktemp("warm"), "jaffle_shop", preseed_db=True)
    path = root / "policies.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["semantic_policies"] += [
        {
            "id": "policy.test.finance_revenue",
            "kind": "object_visibility",
            "action": "visible_only",
            "object_ids": [REVENUE],
            "roles": ["finance"],
        },
        {
            "id": "policy.test.auditor_store",
            "kind": "object_visibility",
            "action": "hidden",
            "object_ids": [STORE],
            "roles": ["auditor"],
        },
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    write_manifest(Runtime.from_path(str(root)))
    return root


@pytest.fixture(scope="module")
def cold(on_disk: Path) -> dict[str, dict[str, Any]]:
    """Each caller's responses from a runtime no other caller has used."""
    out = {}
    for name, context in ALTERNATING.items():
        runtime = Runtime.from_path(str(on_disk))
        try:
            out[name] = envelope(_surfaces(runtime, context))
        finally:
            runtime.close()
    return out


def _unechoed(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _unechoed(item)
            for key, item in value.items()
            if key not in {"request_context", "policy_context"}
        }
    if isinstance(value, list):
        return [_unechoed(item) for item in value]
    return value


def test_a_warm_runtime_answers_each_caller_like_a_fresh_one(on_disk, cold):
    runtime = Runtime.from_path(str(on_disk))
    try:
        assert runtime.manifest_catalog(view="summary", verbosity="compact") is not None
        for round_index in range(2):
            for name in ROUNDS:
                assert envelope(_surfaces(runtime, ALTERNATING[name])) == cold[name], (
                    round_index,
                    name,
                )
            runtime.reload()
    finally:
        runtime.close()
    # The support caller sees what a caller with no roles sees; only the echo of who asked differs.
    assert _unechoed(cold["restricted_a"]) == _unechoed(cold["no_roles"])
    assert cold["restricted_a"] != cold["eligible"] != cold["restricted_b"]
    assert REVENUE not in json.dumps(cold["restricted_a"])
    assert REVENUE in json.dumps(cold["eligible"])
    assert STORE not in json.dumps(cold["restricted_b"]["catalog"])


def test_concurrent_callers_get_their_own_responses(on_disk, cold):
    runtime = Runtime.from_path(str(on_disk))
    barrier = threading.Barrier(4)
    callers = list(ALTERNATING)

    def run(name: str) -> list[bool]:
        barrier.wait(timeout=60)
        context = ALTERNATING[name]
        adapter = SemanticLayerMCPAdapter(runtime)
        results = []
        for _ in range(2):
            discovered = adapter.call_tool(
                "discover",
                {"terms": "revenue", "verbosity": "full", "limit": 20, "policy_context": context},
            )
            results.append(envelope(discovered) == cold[name]["discover"])
            for key, object_id in (("inspect_aov", AOV), ("inspect_store", STORE)):
                card = adapter.call_tool(
                    "inspect",
                    {"object_id": object_id, "verbosity": "full", "policy_context": context},
                )
                results.append(envelope(card) == cold[name][key])
            values = adapter.call_tool(
                "valid-values", {"dimension_id": STORE, "policy_context": context}
            )
            results.append(envelope(values) == cold[name]["valid_values"])
            catalog = outcome(lambda: resolve_catalog(runtime, policy_context=context))
            results.append(envelope(catalog) == cold[name]["catalog"])
        return results

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(run, name) for name in callers}
            outcomes = {name: future.result(timeout=120) for name, future in futures.items()}
    finally:
        runtime.close()
    assert all(all(results) for results in outcomes.values()), outcomes


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the warehouse was reached")


def _unresolved(response: Any) -> bool:
    text = json.dumps(response, default=str)
    return "POLICY_DENIED" in text and "visibility_unresolved" in text


@pytest.mark.parametrize("action", ACTIONS)
def test_an_unresolved_hidden_set_refuses_before_the_warehouse(package, monkeypatch, action):
    from semantic_rails import visible_view

    root, config = package
    governed = with_policies(config, visibility_policy(action, REVENUE))
    runtime = Runtime.from_config(governed, source_path=str(root))
    spy = _Spy()
    monkeypatch.setattr(runtime, "_get_adapter", spy)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("visibility unavailable")

    monkeypatch.setattr(visible_view, "hidden_object_ids", boom)
    adapter = SemanticLayerMCPAdapter(runtime)
    ids = [row.id for row in object_rows(config)]
    try:
        responses = {
            "execute": adapter.call_tool(
                "execute", {"query": BY_STORE, "mode": "run", "policy_context": CALLER}
            ),
            "validate": runtime.validate({**BY_STORE, "policy_context": CALLER}),
            "discover": adapter.call_tool(
                "discover", {"terms": "revenue", "policy_context": CALLER}
            ),
            "plan": adapter.call_tool(
                "plan", {"intent": "customers by store", "policy_context": CALLER}
            ),
            "inspect": adapter.call_tool(
                "inspect", {"object_id": CUSTOMERS, "policy_context": CALLER}
            ),
            "catalog": outcome(lambda: resolve_catalog(runtime, policy_context=CALLER)),
            "http catalog": http_call(runtime, "/catalog", {"policy_context": CALLER}),
        }
        for name, response in responses.items():
            assert _unresolved(response), (name, response)
            text = json.dumps(response, default=str)
            assert not [object_id for object_id in ids if object_id in text], name
            assert '"rows"' not in text
        assert spy.calls == 0
    finally:
        runtime.close()


def test_a_nested_call_never_widens_the_pinned_view(runtimes, monkeypatch):
    runtime = runtimes(visibility_policy("visible_only", REVENUE))
    spy = _Spy()
    monkeypatch.setattr(runtime, "_get_adapter", spy)
    query = {"select": [{"expression": {"measure": REVENUE}, "as": "revenue"}]}

    @runtime_request_scope
    def outer(runtime: Runtime, *, policy_context: dict[str, Any]) -> dict[str, Any]:
        # Asks again as an eligible caller from inside a restricted caller's request.
        return runtime.query({**query, "policy_context": {"roles": ["finance"]}})

    response = outcome(lambda: outer(runtime, policy_context=CALLER))
    assert _unresolved(response), response
    assert REVENUE not in json.dumps(response["details"])
    assert spy.calls == 0


def test_failed_enrichment_suggests_nothing(runtimes, monkeypatch):
    runtime = runtimes(visibility_policy("hidden", REVENUE))
    typo = {"select": [{"expression": {"metric": "metric.sales.customer_cont"}, "as": "v"}]}

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("visibility unavailable")

    working_mcp = mcp_call(
        runtime, "execute", {"query": typo, "mode": "sql", "policy_context": CALLER}
    )
    working_http = http_call(runtime, "/compile", {**typo, "policy_context": CALLER})
    assert CUSTOMERS in json.dumps(working_mcp)
    assert CUSTOMERS in json.dumps(working_http)
    monkeypatch.setattr(type(runtime), "view_for", boom)
    from semantic_rails.http_core import SemanticHTTPService

    service = SemanticHTTPService(runtime)
    error = SemanticLayerError(
        "OBJECT_NOT_FOUND", "Unknown metric recipe 'metric.sales.customer_cont'"
    )
    payload, _status = service.exception_payload(
        error, stage="http", context=RequestContext(roles=("support",))
    )
    assert CUSTOMERS not in json.dumps(payload)
    adapter = SemanticLayerMCPAdapter(runtime)
    mcp_payload = adapter._error_response(error, {"policy_context": CALLER})
    assert CUSTOMERS not in json.dumps(mcp_payload)


@pytest.mark.parametrize("action", ACTIONS)
def test_a_hidden_row_kept_by_mistake_is_refused_after_binding(package, monkeypatch, action):
    from semantic_rails import visible_view

    root, config = package
    runtime = Runtime.from_config(
        with_policies(config, visibility_policy(action, REVENUE)), source_path=str(root)
    )
    spy = _Spy()
    monkeypatch.setattr(runtime, "_get_adapter", spy)
    original = visible_view.build_view
    revenue = next(row for row in config.measures if row.id == REVENUE)

    def leaky(base: PackageConfig, hidden: frozenset[str]) -> PackageConfig:
        view = original(base, hidden)
        return replace(view, measures=[*view.measures, revenue])

    monkeypatch.setattr(visible_view, "build_view", leaky)
    query = {
        "select": [{"expression": {"measure": REVENUE}, "as": "revenue"}],
        "policy_context": CALLER,
    }
    try:
        for call in (runtime.query, runtime.compile):
            refused = outcome(lambda call=call: call(query))
            assert (refused["raised"], refused["message"], refused["details"]) == (
                "OBJECT_NOT_FOUND",
                "The requested object was not found.",
                {},
            )
        report = runtime.validate(query)
        assert [issue["code"] for issue in report["errors"]] == ["OBJECT_NOT_FOUND"]
        assert REVENUE not in json.dumps(report["errors"])
        assert spy.calls == 0
    finally:
        runtime.close()


VIEW_TARGETS = [
    REVENUE,
    STORE,
    "entity.jaffle_store",
    "temporal_role.jaffle_order_time",
    "dimension.jaffle_product_type",
    "measure.jaffle.lifetime_spend_usd",
    "relationship.orders_store",
    "value_domain.jaffle_store_store_name",
    CUSTOMERS,
    SEGMENT,
]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("target", VIEW_TARGETS)
def test_nothing_visible_reads_or_names_a_hidden_object(runtimes, package, action, target):
    """An oracle independent of how the engine closes the hidden set."""
    from semantic_rails.visible_view import _object_reads

    _, config = package
    runtime = runtimes(visibility_policy(action, target))
    base = runtime.package_config
    view = runtime.view_for(CALLER)
    reads = _object_reads(base)
    names = declared_references(base)
    visible = {row.id for row in object_rows(view)}
    assert target not in visible
    for object_id in visible:
        assert reads[object_id] is not None, object_id
        assert reads[object_id] <= visible, (object_id, reads[object_id] - visible)
        assert names[object_id] <= visible, (object_id, names[object_id] - visible)
    # The view keeps every row the oracle allows: only closure removes an object.
    allowed = {row.id for row in object_rows(base)} - {target}
    changed = True
    while changed:
        dropped = {
            object_id
            for object_id in allowed
            if reads[object_id] is None
            or not reads[object_id] <= allowed
            or not names[object_id] <= allowed
        }
        changed = bool(dropped)
        allowed -= dropped
    assert visible == allowed


def test_every_package_field_is_classified():
    from semantic_rails.visible_view import FIELDS

    assert set(FIELDS) == {field.name for field in fields(PackageConfig)}
    assert set(FIELDS.values()) <= {"filtered", "kept", "prose"}


def _diamond(
    tmp_path: Path, *, decisions: list[dict[str, Any]] | None = None
) -> tuple[Path, PackageConfig]:
    pkg = diamond._write_package(tmp_path, decisions=decisions)
    return pkg, load_package_config(str(pkg))


def _hide(config: PackageConfig, *object_ids: str) -> PackageConfig:
    policy = SemanticPolicyConfig(
        id="policy.test.hide_route",
        kind="object_visibility",
        action="hidden",
        object_ids=list(object_ids),
        audiences=["reader"],
    )
    return replace(config, semantic_policies=[policy])


READER = RequestContext(audience="reader").to_policy_context()


def _route_calls(runtime: Runtime) -> dict[str, Any]:
    adapter = SemanticLayerMCPAdapter(runtime)
    out = {}
    for mode in ("run", "validate", "sql"):
        for verbosity in ("minimal", "full"):
            out[f"{mode} {verbosity}"] = adapter.call_tool(
                "execute",
                {
                    "query": diamond.BALANCE_BY_DISTRICT,
                    "mode": mode,
                    "verbosity": verbosity,
                    "policy_context": READER,
                },
            )
    return out


@pytest.fixture(autouse=True)
def _external_packages(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def test_a_hidden_entity_on_the_only_route_matches_its_absence(tmp_path):
    pkg, config = _diamond(tmp_path)
    # Without the owner's home district, the branch is the only way to a district.
    config = replace(
        config,
        relationships=[row for row in config.relationships if row.id != diamond.OWNER_ROUTE[1]],
    )
    hidden_config = _hide(config, "entity.bank_branch")
    from semantic_rails.visible_view import hidden_object_ids

    hidden = hidden_object_ids(hidden_config, audience="reader")
    governed = Runtime.from_config(hidden_config, source_path=str(pkg))
    missing = Runtime.from_config(absent(hidden_config, hidden), source_path=str(pkg))
    try:
        seen, expected = _route_calls(governed), _route_calls(missing)
        assert envelope(seen) == envelope(expected)
        assert all(row["ok"] is False for row in seen.values())
        assert not leaks(seen, hidden_tokens(config, hidden), request=diamond.BALANCE_BY_DISTRICT)
    finally:
        governed.close()
        missing.close()


def test_a_hidden_relationship_on_the_chosen_route_is_no_route(tmp_path):
    decision = {**diamond.DIAMOND_ROW, "relationship_path": diamond.OWNER_ROUTE}
    pkg, config = _diamond(tmp_path, decisions=[decision])
    hidden_config = _hide(config, diamond.OWNER_ROUTE[0])
    from semantic_rails.visible_view import hidden_object_ids

    hidden = hidden_object_ids(hidden_config, audience="reader")
    runtime = Runtime.from_config(hidden_config, source_path=str(pkg))
    try:
        for name, response in _route_calls(runtime).items():
            assert response["ok"] is False, name
            assert response["errors"][0]["code"] == "PATH_NOT_FOUND", response
            assert response["errors"][0]["details"]["reason"] == "no_relationship_chain"
            assert "rows" not in response
            assert not leaks(
                response, hidden_tokens(config, hidden), request=diamond.BALANCE_BY_DISTRICT
            )
    finally:
        runtime.close()


def test_an_ambiguous_route_offers_only_visible_options_and_never_answers(tmp_path):
    pkg, config = _diamond(tmp_path)
    hidden_config = _hide(config, diamond.OWNER_ROUTE[0])
    from semantic_rails.visible_view import hidden_object_ids

    hidden = hidden_object_ids(hidden_config, audience="reader")
    runtime = Runtime.from_config(hidden_config, source_path=str(pkg))
    try:
        for name, response in _route_calls(runtime).items():
            assert response["ok"] is False, name
            error = response["errors"][0]
            assert error["code"] == "AMBIGUOUS_PATH", response
            options = error["details"]["clarification"]["options"]
            assert [option["relationship_path"] for option in options] == [diamond.BRANCH_ROUTE]
            assert not leaks(
                response, hidden_tokens(config, hidden), request=diamond.BALANCE_BY_DISTRICT
            )
    finally:
        runtime.close()


def _card(runtime: Runtime, object_id: str) -> dict[str, Any]:
    return mcp_call(
        runtime, "inspect", {"object_id": object_id, "verbosity": "full", "policy_context": CALLER}
    )


def test_prose_naming_a_hidden_object_is_omitted(package):
    root, config = package
    described = {
        CUSTOMERS: f"Counted like {REVENUE}, but per customer.",
        "metric.sales.inventory_on_hand_eop": f"Read beside {ORDERS} for stock planning.",
    }
    config = replace(
        config,
        metric_recipes=[
            replace(row, description=described.get(row.id, row.description))
            for row in config.metric_recipes
        ],
    )
    runtime = Runtime.from_config(
        with_policies(config, visibility_policy("hidden", REVENUE)), source_path=str(root)
    )
    try:
        assert _card(runtime, CUSTOMERS)["card"]["description"] == ""
        kept = _card(runtime, "metric.sales.inventory_on_hand_eop")["card"]["description"]
        assert kept == described["metric.sales.inventory_on_hand_eop"]
        assert REVENUE not in json.dumps(
            mcp_call(
                runtime,
                "discover",
                {"terms": "customer", "verbosity": "full", "policy_context": CALLER},
            )
        )
    finally:
        runtime.close()


def test_a_policy_listing_a_hidden_object_shows_its_generic_form(runtimes):
    deny = replace(
        access_policy("deny", ORDERS, REVENUE),
        id="policy.test.orders_and_revenue",
        rationale="Support never sees orders.",
    )
    runtime = runtimes(visibility_policy("hidden", REVENUE), deny)
    card = _card(runtime, ORDERS)
    effects = [row for row in card["card"]["policy_effects"] if row["kind"] == "object_access"]
    assert [row["action"] for row in effects] == ["deny"]
    assert "policy_id" not in effects[0]
    assert effects[0]["object_ids"] == [ORDERS]
    assert deny.rationale not in json.dumps(card)
    refusal = runtime.validate({**ORDERS_BY_STORE, "policy_context": CALLER})
    public = [
        row
        for row in refusal["errors"][0]["details"]["policy_effects"]
        if row["kind"] == "object_access"
    ]
    assert [row["action"] for row in public] == ["deny"]
    assert "policy_id" not in public[0]
    assert deny.id not in json.dumps(refusal)


def test_a_label_a_visible_object_shares_is_not_a_hidden_token(package):
    """Hidden order counts are labelled "Orders"; visible "Large orders" shares the word."""
    root, config = package
    description = "Orders above the large-order threshold."
    config = replace(
        config,
        measures=[
            replace(row, description=description)
            if row.id == "measure.jaffle.large_order_count"
            else row
            for row in config.measures
        ],
    )
    runtime = Runtime.from_config(
        with_policies(config, visibility_policy("hidden", ORDERS)), source_path=str(root)
    )
    try:
        card = _card(runtime, "measure.jaffle.large_order_count")
        assert card["card"]["description"] == description
    finally:
        runtime.close()


# Routes through a hidden object, in both MCP channels and the direct runtime.


def _mcp(adapter, tool, query, verbosity, audience):
    response = handle_jsonrpc_message(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "execute",
                "arguments": {
                    "query": query,
                    "verbosity": verbosity,
                    "mode": {"execute": "run", "validate": "validate", "compile": "sql"}[tool],
                },
            },
        },
        request_context=RequestContext(audience=audience),
    )
    result = response["result"]
    structured = result["structuredContent"]
    text = result["content"][0]["text"]
    assert json.loads(text) == structured
    return structured, (json.dumps(structured), text)


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("mode", ["refusal", "query_choice", "package_choice"])
@pytest.mark.parametrize("hidden", [diamond.OWNER, diamond.OWNER_ROUTE[0]])
def test_diamond_routes_in_both_mcp_channels(tmp_path, tool, verbosity, mode, hidden):
    decision = {**diamond.DIAMOND_ROW, "relationship_path": diamond.BRANCH_ROUTE}
    pkg = diamond._write_package(
        tmp_path, decisions=[decision] if mode == "package_choice" else None
    )
    config = load_package_config(str(pkg))
    config = replace(
        config,
        entities=[
            replace(entity, label="Private Owner") if entity.id == diamond.OWNER else entity
            for entity in config.entities
        ],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[hidden],
                audiences=["reader"],
            )
        ],
    )
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    query = dict(diamond.BALANCE_BY_DISTRICT)
    if mode == "query_choice":
        query["route_decisions"] = [decision]
    try:
        # Use the same runtime/cache across readers and authors.
        for audience in ("author", "reader", "author"):
            out, channels = _mcp(adapter, tool, query, verbosity, audience)
            assert out["ok"] is (mode != "refusal")
            if mode == "refusal":
                assert out["errors"][0]["code"] == "AMBIGUOUS_PATH"
            elif tool == "execute":
                assert diamond._rows(out, ["dimension.bank_district_name", "v"]) == diamond._gold(
                    diamond.BY_BRANCH
                )
            for channel in channels:
                if audience == "reader":
                    for token in ("Private Owner", diamond.OWNER, *diamond.OWNER_ROUTE):
                        assert token not in channel
                elif mode == "refusal" or (mode == "query_choice" and verbosity != "minimal"):
                    assert diamond.OWNER_ROUTE[0] in channel
            if mode != "refusal":
                direct = getattr(runtime, {"execute": "query"}.get(tool, tool))(
                    {
                        **query,
                        "verbosity": verbosity,
                        "policy_context": RequestContext(audience=audience).to_policy_context(),
                    }
                )
                if audience == "reader":
                    assert all(
                        token not in json.dumps(direct)
                        for token in ("Private Owner", diamond.OWNER, *diamond.OWNER_ROUTE)
                    )
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_an_answerable_route_refuses_when_visibility_fails(tmp_path, monkeypatch, tool, verbosity):
    from semantic_rails import visible_view

    decision = {**diamond.DIAMOND_ROW, "relationship_path": diamond.BRANCH_ROUTE}
    pkg = diamond._write_package(tmp_path, decisions=[decision])
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER],
            )
        ],
    )

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("visibility unavailable")

    monkeypatch.setattr(visible_view, "hidden_object_ids", unavailable)
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        out, channels = _mcp(adapter, tool, diamond.BALANCE_BY_DISTRICT, verbosity, "reader")
        assert out["ok"] is False
        assert _unresolved(out)
        for channel in channels:
            assert "relationship." not in channel
            assert "ROUTE_RECORDED" not in channel
            assert '"rows"' not in channel
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_no_visible_route_option_is_no_route(tmp_path, tool, verbosity):
    pkg = diamond._write_package(tmp_path)
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_routes",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER, "entity.bank_branch"],
            )
        ],
    )
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        # Both of the ambiguous pair's routes read a hidden object: the pair has no route.
        out, channels = _mcp(adapter, tool, diamond.BALANCE_BY_DISTRICT, verbosity, "reader")
        assert out["ok"] is False
        assert out["errors"][0]["code"] == "PATH_NOT_FOUND"
        for channel in channels:
            for token in (*diamond.OWNER_ROUTE, *diamond.BRANCH_ROUTE, "Owner", "Branch"):
                assert token not in channel
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("mode", ["conflict", "inherited", "refusal_conflict"])
def test_related_route_rows_are_visible_before_they_are_named(tmp_path, tool, verbosity, mode):
    # A visible own-key route conflicts with a recorded route through a hidden client.
    # An inherited visible route can instead follow a row whose other endpoint is hidden.
    hidden = precedence.LOAN if mode == "refusal_conflict" else precedence._entity("client")
    rows = {
        "conflict": [precedence.ACCOUNT_OWNER_ROW],
        "inherited": [
            precedence._row(hidden, precedence.DISTRICT, [precedence.OWNER[0], *precedence.BRANCH])
        ],
        "refusal_conflict": [precedence.LOAN_REGION_BY_OWNER],
    }[mode]
    pkg = precedence._write_package(
        tmp_path,
        relationships=precedence.OWN_DISTRICT if mode == "conflict" else precedence.LENDER,
        rows=rows,
    )
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_waypoint",
                kind="object_visibility",
                action="hidden",
                object_ids=[hidden],
                audiences=["reader"],
            )
        ],
    )
    query = precedence._query(
        precedence.LOAN_AMOUNT if mode == "conflict" else precedence.BALANCE,
        group_by=[precedence.DISTRICT_NAME],
    )
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        for audience in ("author", "reader"):
            out, channels = _mcp(adapter, tool, query, verbosity, audience)
            assert out["ok"] is (mode != "refusal_conflict"), out
            if mode == "refusal_conflict":
                assert out["errors"][0]["code"] == "AMBIGUOUS_PATH"
            elif tool == "execute":
                gold = (
                    precedence.OWN_KEY_GOLD
                    if mode == "conflict"
                    else precedence._by_account_route("account", "branch")
                )
                assert precedence._rows(out, [precedence.DISTRICT_NAME, "v"]) == precedence._gold(
                    gold
                )
            if verbosity != "minimal" or mode == "refusal_conflict":
                for channel in channels:
                    token = precedence.OWNER[0] if mode == "conflict" else hidden
                    assert (token in channel) is (audience == "author")
                    if audience == "reader" and mode == "conflict":
                        assert all(rel not in channel for rel in precedence.OWNER)
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["execute", "validate", "compile"])
@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_multi_measure_routes_check_every_relationship_endpoint(tmp_path, tool, verbosity):
    pkg = diamond._write_package(tmp_path)
    seed = pkg / "data" / "seed.sql"
    seed.write_text(
        seed.read_text() + "ALTER TABLE accounts ADD COLUMN district_id INTEGER; "
        "UPDATE accounts SET district_id = b.district_id FROM branches b "
        "WHERE accounts.branch_id = b.branch_id;"
    )
    base = load_package_config(str(pkg))
    config = replace(
        base,
        relationships=[
            replace(
                relationship,
                source_entity=diamond.DISTRICT,
                target_entity=diamond.OWNER,
                source_column="district_id",
                target_column="home_district_id",
                source_columns=["district_id"],
                target_columns=["home_district_id"],
                cardinality="1:N",
            )
            if relationship.id == diamond.OWNER_ROUTE[1]
            else relationship
            for relationship in base.relationships
        ]
        + [
            RelationshipConfig(
                id="relationship.accounts_district",
                source_entity=diamond.ACCOUNT,
                target_entity=diamond.DISTRICT,
                source_column="district_id",
                target_column="district_id",
                cardinality="N:1",
                safety="safe",
            )
        ],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER],
                audiences=["reader"],
            )
        ],
    )
    query = {
        **diamond.BALANCE_BY_DISTRICT,
        "select": [
            {"expression": {"measure": "measure.bank.budget"}, "as": "budget"},
            *diamond.BALANCE_BY_DISTRICT["select"],
        ],
    }
    runtime = Runtime.from_config(config, source_path=str(pkg))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        for audience in ("author", "reader", "author"):
            out, channels = _mcp(adapter, tool, query, verbosity, audience)
            assert out["ok"] is True, out
            direct = getattr(runtime, {"execute": "query"}.get(tool, tool))(
                {
                    **query,
                    "verbosity": verbosity,
                    "policy_context": RequestContext(audience=audience).to_policy_context(),
                }
            )
            assert direct["ok"] is True, direct
            for channel in (*channels, json.dumps(direct)):
                for relationship in diamond.OWNER_ROUTE:
                    if audience == "reader":
                        assert relationship not in channel
                    elif verbosity != "minimal":
                        assert relationship in channel
            if tool == "execute":
                assert diamond._rows(out, ["dimension.bank_district_name", "budget", "v"]) == (
                    diamond._gold(
                        "SELECT d.district_name, d.budget, COALESCE(SUM(a.balance), 0) "
                        "FROM districts d LEFT JOIN branches b USING (district_id) "
                        "LEFT JOIN accounts a USING (branch_id) GROUP BY 1, 2"
                    )
                )
    finally:
        adapter.close()
        runtime.close()


@pytest.mark.parametrize("tool", ["validate", "compile"])
@pytest.mark.parametrize(
    "literal", [{"path": "hello"}, {"relationship_id": "hello"}, {"candidate_paths": ["x"]}]
)
def test_route_projection_preserves_literal_objects(tmp_path, tool, literal):
    pkg = diamond._write_package(tmp_path)
    config = replace(
        load_package_config(str(pkg)),
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_owner",
                kind="object_visibility",
                action="hidden",
                object_ids=[diamond.OWNER],
                audiences=["reader"],
            )
        ],
    )
    query = {
        **diamond.BALANCE_BY_DISTRICT,
        "select": [
            *diamond.BALANCE_BY_DISTRICT["select"],
            {"expression": {"kind": "literal", "value": literal}, "as": "x"},
        ],
        "route_decisions": [{**diamond.DIAMOND_ROW, "relationship_path": diamond.BRANCH_ROUTE}],
        "policy_context": RequestContext(audience="reader").to_policy_context(),
        "verbosity": "full",
    }
    runtime = Runtime.from_config(config, source_path=str(pkg))
    try:
        out = getattr(runtime, tool)(query)
        assert out["ok"] is True, out
        assert out["logical_plan"]["query"]["select"][1]["expression"]["value"] == literal
        assert out["explain"]["normalized_query"]["select"][1]["expression"]["value"] == literal
        assert out["logical_plan"]["post_aggregation_exprs"]["x"]["value"] == literal
    finally:
        runtime.close()


@pytest.mark.parametrize("hidden_ids", [frozenset(), frozenset({diamond.OWNER})])
def test_unknown_relationship_visibility_fails_closed(tmp_path, hidden_ids):
    config = load_package_config(str(diamond._write_package(tmp_path)))
    assert not visible_route(config, diamond.ACCOUNT, ["relationship.unknown"], hidden_ids)
