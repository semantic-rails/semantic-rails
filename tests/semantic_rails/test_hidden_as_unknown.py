"""An object hidden from the caller is refused exactly like one that does not exist.

The matrix names a jaffle object hidden from the support caller, by ``hidden`` or by a
``visible_only`` policy naming finance, in every position the binder reads a reference
(position x spelling x multiplicity), through every MCP tool and mode and every HTTP route that
takes an id or a query. Each response must equal, whole, the response to the same request
against the package with the hidden objects absent (``hidden_absent.absent``), with no id
rewritten, and must name no token of a hidden object the request did not name itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.config import load_package_config
from semantic_rails.expressions import REFERENCE_KEYS, REFERENCE_LISTS, expr_to_dict
from semantic_rails.http_core import PUBLIC_V1_ROUTES
from semantic_rails.mcp import list_tool_definitions
from semantic_rails.runtime import Runtime
from semantic_rails.schema import PackageConfig, SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.hidden_absent import (
    ACTIONS,
    CALLER,
    absent,
    access_policy,
    envelope,
    hidden_tokens,
    http_call,
    leaks,
    mcp_call,
    visibility_policy,
    with_policies,
)

CUSTOMERS = "metric.sales.customer_count"
AOV = "metric.sales.aov_usd"
CONVERSION = "metric.adoption.signup_to_send_conversion_rate_28d"
ORDERS = "measure.jaffle.order_count"
REVENUE = "measure.jaffle.revenue_usd"
LIFETIME_SPEND = "measure.jaffle.lifetime_spend_usd"
STORE = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_product_type"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
ORDER_TIME = "temporal_role.jaffle_order_time"
STORE_ENTITY = "entity.jaffle_store"
CUSTOMER = "entity.jaffle_customer"
SEGMENT = "segment.jaffle.high_value_customers"
ORDERS_STORE = "relationship.orders_store"
STORE_NAMES = "value_domain.jaffle_store_store_name"
STORE_ALIAS = "store_alias"
OBJECTS = {
    "metric": CUSTOMERS,
    "measure": ORDERS,
    "dimension": STORE,
    "temporal_role": ORDER_TIME,
    "entity": STORE_ENTITY,
    "segment": SEGMENT,
    "relationship": ORDERS_STORE,
    "value_domain": STORE_NAMES,
}
CARRIER = {"expression": {"metric": "metric.sales.inventory_on_hand_eop"}, "as": "value"}


def _select(expression: dict[str, Any], alias: str = "value") -> dict[str, Any]:
    return {"select": [{"expression": expression, "as": alias}]}


def _carried(**parts: Any) -> dict[str, Any]:
    """A visible measure the position decorates, so the hidden id is the only problem."""
    return {"select": [{"expression": {"measure": ORDERS}, "as": "value"}], **parts}


def _conversion(**changes: Any) -> dict[str, Any]:
    expression = expr_to_dict(_JAFFLE.metric(CONVERSION).expression)
    return {**expression, **changes}


class _Jaffle:
    """The authored jaffle package, read once for expression templates."""

    _config: PackageConfig | None = None

    def metric(self, metric_id: str) -> Any:
        if self._config is None:
            self._config = load_package_config("configs/semantic_rails/jaffle_shop")
        return next(row for row in self._config.metric_recipes if row.id == metric_id)


_JAFFLE = _Jaffle()

# Each position: (the binder key it exercises, the kind of id it takes, the query holding ``x``).
POSITIONS: dict[str, tuple[str, str, Callable[[str], dict[str, Any]]]] = {
    "select.metric": ("metric", "metric", lambda x: _select({"metric": x})),
    "select.measure": ("measure", "measure", lambda x: _select({"measure": x})),
    "select.metric_recipe": (
        "metric_recipe",
        "metric",
        lambda x: _select({"kind": "metric", "metric_recipe": x}),
    ),
    "select.dimension": (
        "dimension",
        "dimension",
        lambda x: {"select": [{"expression": {"measure": ORDERS}, "as": "v"}, {"dimension": x}]},
    ),
    "group_by": ("group_by", "dimension", lambda x: _carried(group_by=[x])),
    "where.field": (
        "field",
        "dimension",
        lambda x: _carried(where=[{"field": x, "op": "=", "value": "Philadelphia"}]),
    ),
    "order_by.field": (
        "field",
        "dimension",
        lambda x: _carried(order_by=[{"field": x, "direction": "desc"}], limit=3),
    ),
    "time.temporal_role": (
        "temporal_role",
        "temporal_role",
        lambda x: _carried(time={"temporal_role": x, "grain": "month"}),
    ),
    "select.measure.temporal_role": (
        "temporal_role",
        "temporal_role",
        lambda x: _select({"measure": ORDERS, "temporal_role": x}),
    ),
    "metric_filters.entity": (
        "entity",
        "entity",
        lambda x: _carried(
            metric_filters=[
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": x,
                        "scope_mode": "entity_only",
                        "input": {"measure": ORDERS},
                        "op": ">=",
                        "value": 1,
                    },
                    "op": "=",
                    "value": True,
                }
            ]
        ),
    ),
    "metric_filters.input.measure": (
        "measure",
        "measure",
        lambda x: _carried(
            metric_filters=[
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": CUSTOMER,
                        "scope_mode": "entity_only",
                        "input": {"measure": x},
                        "op": ">=",
                        "value": 1,
                    },
                    "op": "=",
                    "value": True,
                }
            ]
        ),
    ),
    "select.aggregate.filter.field": (
        "field",
        "dimension",
        lambda x: _select(
            {
                "kind": "aggregate",
                "measure": ORDERS,
                "aggregation": "count",
                "filter": {"all": [{"field": x, "op": "=", "value": "Philadelphia"}]},
            }
        ),
    ),
    "select.entity_value.entity": (
        "entity",
        "entity",
        lambda x: _select(
            {
                "kind": "distribution",
                "function": "avg",
                "over": {"kind": "entity_value", "entity": x, "input": {"measure": ORDERS}},
            }
        ),
    ),
    "select.cumulative.partition_by": (
        "partition_by",
        "dimension",
        lambda x: {
            **_select({"kind": "cumulative", "input": {"measure": ORDERS}, "partition_by": [x]}),
            "time": {"temporal_role": ORDER_TIME, "grain": "month"},
        },
    ),
    "select.conversion.constant_properties": (
        "constant_properties",
        "dimension",
        lambda x: _select(_conversion(constant_properties=[x], dimension_bindings={})),
    ),
    "select.conversion.dimension_bindings": (
        "dimension_bindings",
        "dimension",
        lambda x: {
            **_select(
                _conversion(
                    dimension_bindings={x: {"side": "converted", "denominator": "all_base_events"}}
                )
            ),
            "group_by": [x],
        },
    ),
    "temporal_role_overrides.key": (
        "temporal_role_overrides",
        "measure",
        lambda x: _carried(temporal_role_overrides={x: ORDER_TIME}),
    ),
    "temporal_role_overrides.value": (
        "temporal_role_overrides",
        "temporal_role",
        lambda x: _carried(temporal_role_overrides={ORDERS: x}),
    ),
    "route_decisions.relationship_path": (
        "route_decisions",
        "relationship",
        lambda x: _carried(
            group_by=[STORE],
            route_decisions=[
                {
                    "source_entity": "entity.jaffle_order",
                    "target_entity": STORE_ENTITY,
                    "relationship_path": [x],
                }
            ],
        ),
    ),
    "route_decisions.target_entity": (
        "route_decisions",
        "entity",
        lambda x: _carried(
            route_decisions=[
                {
                    "source_entity": "entity.jaffle_order",
                    "target_entity": x,
                    "relationship_path": [ORDERS_STORE],
                }
            ],
        ),
    ),
}
# Keys a request has no place for: an object holding them is reached through the segment that
# reads it (its basis metric, preview dimensions and membership filters).
SEGMENT_KEYS = {"basis_metric", "preview_dimensions"}


def test_the_matrix_covers_every_reference_key_the_binder_reads():
    covered = {key for key, _, _ in POSITIONS.values()} | SEGMENT_KEYS
    assert set(REFERENCE_KEYS) | set(REFERENCE_LISTS) | {
        "dimension_bindings",
        "temporal_role_overrides",
    } <= covered | {"route_decisions"}


def _spellings(config: PackageConfig, position: str, object_id: str) -> dict[str, str]:
    spellings = {"id": object_id, "padded": f" {object_id} "}
    if POSITIONS[position][1] == "dimension":
        row = next(row for row in config.dimensions if row.id == object_id)
        spellings |= {"name": row.name, "label": row.label, "alias": row.aliases[0]}
    return spellings


def _repeated(query: dict[str, Any], object_id: str, *, reverse: bool) -> dict[str, Any]:
    """The same hidden id selected twice, plain and padded, under distinct output names."""
    rows = [
        {"expression": query["select"][0]["expression"], "as": "plain"},
        {
            "expression": json.loads(
                json.dumps(query["select"][0]["expression"]).replace(
                    json.dumps(object_id), json.dumps(f" {object_id} ")
                )
            ),
            "as": "padded",
        },
    ]
    return {**query, "select": rows[::-1] if reverse else rows}


def _variants(config: PackageConfig) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(position, spelling, multiplicity, query) for every query position."""
    out = []
    for position, (_key, kind, build) in POSITIONS.items():
        object_id = OBJECTS[kind]
        for spelling, text in _spellings(config, position, object_id).items():
            query = build(text)
            out.append((position, spelling, "once", query))
            if (
                position.startswith("select.")
                and spelling == "id"
                and kind in {"metric", "measure"}
            ):
                out.append(
                    (position, spelling, "twice", _repeated(query, object_id, reverse=False))
                )
                out.append(
                    (
                        position,
                        spelling,
                        "twice_reversed",
                        _repeated(query, object_id, reverse=True),
                    )
                )
        if position in {"select.metric", "group_by"}:
            denied = {
                **build(object_id),
                "select": [
                    *build(object_id).get("select", []),
                    {"expression": {"measure": "measure.jaffle.item_count"}, "as": "denied"},
                ],
            }
            out.append((position, "id", "with_denied", denied))
    return out


# surface -> (takes a query or an id, call(runtime, kind, value) -> response)
Call = Callable[[Runtime, dict[str, Any] | str], dict[str, Any]]


def _mcp(tool: str, **fixed: Any) -> Call:
    def call(runtime: Runtime, value: Any) -> dict[str, Any]:
        arguments = {**fixed, "policy_context": CALLER}
        key = next(name for name, text in fixed.items() if text is None)
        arguments[key] = value
        return mcp_call(runtime, tool, arguments)

    return call


def _http(route: str, **fixed: Any) -> Call:
    def call(runtime: Runtime, value: Any) -> dict[str, Any]:
        body = {**fixed, "policy_context": CALLER}
        key = next(name for name, text in fixed.items() if text is None)
        if key == "query" and route in {"/validate", "/compile", "/query"}:
            body = {**value, "policy_context": CALLER}
        else:
            body[key] = value
        return http_call(runtime, route, body)

    return call


QUERY_SURFACES: dict[str, Call] = {
    **{
        f"mcp execute {mode} {verbosity}": _mcp(
            "execute", mode=mode, verbosity=verbosity, query=None
        )
        for mode in ("run", "validate", "sql")
        for verbosity in ("minimal", "compact", "full")
    },
    "mcp plan": _mcp("plan", intent="how many orders", query=None),
    "mcp discover": _mcp("discover", terms="orders", query=None),
    **{f"http {route}": _http(route, query=None) for route in ("/validate", "/compile", "/query")},
    "http /plan": _http("/plan", intent="how many orders", query=None),
    "http /discover": _http("/discover", terms="orders", query=None),
    "http /build-options": _http("/build-options", query=None),
}
ID_SURFACES: dict[str, tuple[tuple[str, ...], Call]] = {
    **{
        f"mcp inspect {verbosity}": (
            tuple(OBJECTS),
            _mcp("inspect", verbosity=verbosity, object_id=None),
        )
        for verbosity in ("minimal", "compact", "full")
    },
    "mcp valid-values": (tuple(OBJECTS), _mcp("valid-values", dimension_id=None)),
    **{
        f"mcp segment {action} {verbosity}": (
            tuple(OBJECTS),
            _mcp("segment", action=action, verbosity=verbosity, segment_id=None),
        )
        for action in ("validate", "explain", "preview")
        for verbosity in ("minimal", "full")
    },
    "http /inspect": (tuple(OBJECTS), _http("/inspect", object_id=None)),
    "http /valid-values": (tuple(OBJECTS), _http("/valid-values", dimension_id=None)),
    "http /build-options focus": (tuple(OBJECTS), _http("/build-options", focus_object_id=None)),
    "http /catalog entity": (("entity",), _http("/catalog", entity=None)),
    **{
        f"http /segment-{action}": (tuple(OBJECTS), _http(f"/segment-{action}", segment_id=None))
        for action in ("validate", "explain", "preview")
    },
}


def test_the_surfaces_are_every_tool_mode_and_route_that_takes_an_id_or_a_query():
    tools: set[str] = set()
    for tool in list_tool_definitions():
        properties = tool["inputSchema"]["properties"]
        modes = properties.get("mode", properties.get("action", {})).get("enum") or [""]
        tools.update(f"mcp {tool['name']} {mode}".strip() for mode in modes)
    covered = {" ".join(name.split()[:3]) for name in [*QUERY_SURFACES, *ID_SURFACES]}
    covered |= {" ".join(name.split()[:2]) for name in [*QUERY_SURFACES, *ID_SURFACES]}
    assert tools <= covered, tools - covered
    routes = {
        row["path"].removeprefix("/api/v1") for row in PUBLIC_V1_ROUTES if row["method"] == "POST"
    }
    named = {name.split()[1] for name in [*QUERY_SURFACES, *ID_SURFACES] if name.startswith("http")}
    assert routes == named


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PackageConfig]:
    root = copy_package_config(tmp_path_factory.mktemp("hidden"), "jaffle_shop", preseed_db=True)
    config = load_package_config(str(root))
    config = replace(
        config,
        dimensions=[
            replace(row, aliases=[*row.aliases, STORE_ALIAS]) if row.id == STORE else row
            for row in config.dimensions
        ],
    )
    return root, config


def engine_hidden(config: PackageConfig, context: dict[str, Any]) -> frozenset[str]:
    from semantic_rails.request_context import context_from_policy_context

    try:  # WIP: the base engine's set, to record the failures there
        from semantic_rails.visible_view import hidden_object_ids
    except ImportError:
        from semantic_rails.policies import hidden_object_ids

    caller = context_from_policy_context(context)
    return frozenset(
        hidden_object_ids(
            config, environment=caller.environment, audience=caller.audience, roles=caller.roles
        )
    )


@pytest.fixture(scope="module")
def runtimes(package: tuple[Path, PackageConfig]) -> Iterator[Callable[..., Runtime]]:
    """Runtimes over the package with extra policies, cached; ``hide`` makes the absent one."""
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


def _pair(runtimes, package, action: str, object_id: str, *extra: SemanticPolicyConfig):
    """The governed runtime, the runtime over the absent package, and the hidden ids."""
    policy = visibility_policy(action, object_id)
    governed = runtimes(policy, *extra)
    hidden = engine_hidden(governed.config, CALLER)
    assert object_id in hidden
    return governed, runtimes(*extra, hide=hidden), hidden


def _check(hidden_response, absent_response, config, hidden, request) -> None:
    assert envelope(hidden_response) == envelope(absent_response)
    assert not leaks(hidden_response, hidden_tokens(config, hidden), request=request)
    assert "POLICY_DENIED" not in json.dumps(hidden_response) or "item_count" in json.dumps(request)


def _query_cases() -> list[Any]:
    config = load_package_config("configs/semantic_rails/jaffle_shop")
    config = replace(
        config,
        dimensions=[
            replace(row, aliases=[*row.aliases, STORE_ALIAS]) if row.id == STORE else row
            for row in config.dimensions
        ],
    )
    cases = []
    for position, spelling, multiplicity, query in _variants(config):
        for surface in QUERY_SURFACES:
            # Verbosity changes only the shape of one envelope: one spelling covers it.
            short = "execute" in surface and not surface.endswith("full")
            if short and (spelling != "id" or multiplicity != "once"):
                continue
            # Seeds reach the binder through the same partial-query normalization: the
            # id, padded and alias spellings cover it (label and name: the named repros).
            seeded = surface.split()[1] in {"plan", "discover", "/plan", "/discover"}
            if (seeded or surface.endswith("/build-options")) and spelling in {"name", "label"}:
                continue
            cases.append(
                pytest.param(
                    surface,
                    query,
                    POSITIONS[position][1],
                    id=f"{surface}-{position}-{spelling}-{multiplicity}",
                )
            )
    return cases


QUERY_CASES = _query_cases()


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(("surface", "query", "kind"), QUERY_CASES)
def test_a_hidden_reference_in_a_query_gets_the_absent_response(
    runtimes, package, action, surface, query, kind
):
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, OBJECTS[kind])
    call = QUERY_SURFACES[surface]
    denied = access_policy("deny", "measure.jaffle.item_count")
    if "item_count" in json.dumps(query):
        governed, missing, hidden = _pair(runtimes, package, action, OBJECTS[kind], denied)
    response = call(governed, query)
    _check(response, call(missing, query), config, hidden, query)


ID_CASES = [
    pytest.param(surface, kind, id=f"{surface}-{kind}")
    for surface, (kinds, _) in ID_SURFACES.items()
    for kind in kinds
]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(("surface", "kind"), ID_CASES)
def test_a_hidden_id_gets_the_absent_response(runtimes, package, action, surface, kind):
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, OBJECTS[kind])
    _, call = ID_SURFACES[surface]
    for spelling in (OBJECTS[kind], f" {OBJECTS[kind]} "):
        response = call(governed, spelling)
        _check(response, call(missing, spelling), config, hidden, spelling)


@pytest.mark.parametrize("action", ACTIONS)
def test_a_visible_denied_object_keeps_its_policy_denial(runtimes, package, action):
    """``deny`` governs an object the caller can see: the refusal names it and its policy."""
    deny = access_policy("deny", ORDERS)
    runtime = runtimes(deny, visibility_policy(action, REVENUE))
    report = runtime.validate({**_select({"measure": ORDERS}), "policy_context": CALLER})
    assert [issue["code"] for issue in report["errors"]] == ["POLICY_DENIED"]
    details = report["errors"][0]["details"]
    assert details["blocked_objects"] == [ORDERS]
    assert [row["policy_id"] for row in details["policy_effects"]] == [deny.id]


@pytest.mark.parametrize("action", ACTIONS)
def test_an_eligible_caller_still_answers(package, action):
    """visible_only names finance as eligible; hidden names only the support role."""
    root, config = package
    query = {
        **_select({"metric": CUSTOMERS}),
        "group_by": [STORE],
        "policy_context": {"roles": ["finance"]},
    }
    rows = []
    for policies in (
        [],
        [visibility_policy(action, CUSTOMERS)],
        [visibility_policy(action, STORE)],
    ):
        runtime = opened(
            Runtime.from_config(with_policies(config, *policies), source_path=str(root))
        )
        try:
            rows.append(sorted(runtime.query(query)["rows"], key=lambda row: row[STORE]))
        finally:
            runtime.close()
    assert rows[0]
    assert rows[1] == rows[2] == rows[0]


# Named repros: each request once found a hidden object's existence or values.

WITHHOLD = replace(
    access_policy("withhold_values", CUSTOMERS), id="policy.test.withhold", config={"max_rank": 3}
)


def _ranked(alias: str) -> dict[str, Any]:
    return {
        "select": [{"expression": {"metric": CUSTOMERS}, "as": alias}],
        "group_by": [STORE],
        "order_by": [{"field": alias, "direction": "desc"}],
        "limit": 1,
    }


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "surface", ["mcp run", "mcp validate", "mcp sql", "http /query", "http /validate"]
)
def test_an_output_alias_named_like_a_hidden_id_keeps_values_withheld(runtimes, action, surface):
    runtime = runtimes(WITHHOLD, visibility_policy(action, REVENUE))

    def call(alias: str) -> dict[str, Any]:
        query = _ranked(alias)
        if surface.startswith("mcp"):
            mode = surface.split()[1]
            arguments = {
                "query": query,
                "mode": mode,
                "verbosity": "full",
                "policy_context": CALLER,
            }
            return mcp_call(runtime, "execute", arguments)
        return http_call(runtime, surface.split()[1], {**query, "policy_context": CALLER})

    def modulo(response: dict[str, Any], alias: str) -> Any:
        """The response with the caller's output name, quoted in SQL or not, as one marker."""
        text = json.dumps(envelope(response)).replace(f'\\"{alias}\\"', "@alias@")
        return json.loads(text.replace(alias, "@alias@"))

    named, plain = call(REVENUE), call("rank_value")
    assert named["ok"] is True, named
    assert named["withheld"] == [CUSTOMERS]
    for row in named.get("rows", []):
        assert REVENUE not in row and "rank_value" not in row
    assert modulo(named, REVENUE) == modulo(plain, "rank_value")


PLAN_SEEDS = {
    "where_id": {"where": [{"field": STORE, "op": "=", "value": "Philadelphia"}]},
    "where_padded": {"where": [{"field": f" {STORE} ", "op": "=", "value": "Philadelphia"}]},
    "where_label": {"where": [{"field": "Store name", "op": "=", "value": "Philadelphia"}]},
    "where_alias": {"where": [{"field": STORE_ALIAS, "op": "=", "value": "Philadelphia"}]},
    "order_by": {"order_by": [{"field": STORE, "direction": "asc"}]},
    "order_by_padded": {"order_by": [{"field": f" {STORE} ", "direction": "asc"}]},
}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("seed", list(PLAN_SEEDS))
@pytest.mark.parametrize("surface", ["mcp", "http"])
def test_plan_seed_filters_and_orderings_on_a_hidden_dimension_match_an_absent_one(
    runtimes, package, action, seed, surface
):
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, STORE)
    query = {**_select({"metric": CUSTOMERS}), **PLAN_SEEDS[seed]}

    def call(runtime: Runtime) -> dict[str, Any]:
        if surface == "mcp":
            arguments = {"intent": "how many customers", "query": query, "policy_context": CALLER}
            return mcp_call(runtime, "plan", arguments)
        body = {"intent": "how many customers", "query": query, "policy_context": CALLER}
        return http_call(runtime, "/plan", body)

    _check(call(governed), call(missing), config, hidden, query)


@pytest.mark.parametrize("hide", ["metric", "dimension"])
def test_a_dimension_alias_equal_to_a_hidden_metric_id_does_not_resolve_the_metric(
    package, tmp_path, hide
):
    """The store dimension is aliased by the customer-count metric's id. Hiding the metric
    leaves the dimension; hiding the dimension leaves the metric answering."""
    root, config = package
    config = replace(
        config,
        dimensions=[
            replace(row, aliases=[*row.aliases, CUSTOMERS]) if row.id == STORE else row
            for row in config.dimensions
        ],
    )
    query = {**_select({"metric": CUSTOMERS}), "policy_context": CALLER}
    for action in ACTIONS:
        target = CUSTOMERS if hide == "metric" else STORE
        governed = with_policies(config, visibility_policy(action, target))
        hidden = engine_hidden(governed, CALLER)
        runtime = Runtime.from_config(governed, source_path=str(root))
        missing = Runtime.from_config(absent(governed, hidden), source_path=str(root))
        try:
            for mode in ("run", "validate", "sql"):
                arguments = {"query": query, "mode": mode, "policy_context": CALLER}
                response = mcp_call(runtime, "execute", arguments)
                assert envelope(response) == envelope(mcp_call(missing, "execute", arguments))
                assert response["ok"] is (hide == "dimension"), response
        finally:
            runtime.close()
            missing.close()


_SPELLINGS_SCRIPT = """
import json, sys
from dataclasses import replace
from semantic_rails.config import load_package_config
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime
from tests.semantic_rails.hidden_absent import CALLER, absent, envelope, visibility_policy, with_policies
from semantic_rails.visible_view import hidden_object_ids
root, action, reverse = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
metric = "metric.sales.customer_count"
rows = [
    {"expression": {"metric": metric}, "as": "plain"},
    {"expression": {"metric": f" {metric} "}, "as": "padded"},
]
query = {"select": rows[::-1] if reverse else rows}
config = with_policies(load_package_config(root), visibility_policy(action, metric))
hidden = hidden_object_ids(config, roles=["support"])
out = []
for package in (config, absent(config, hidden)):
    runtime = Runtime.from_config(package, source_path=root)
    out.append([
        envelope(SemanticLayerMCPAdapter(runtime).call_tool(
            "execute", {"query": query, "mode": mode, "policy_context": CALLER}
        ))
        for mode in ("run", "validate", "sql")
    ])
print(json.dumps(out))
"""


@pytest.mark.parametrize("seed", ["0", "1"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("action", ACTIONS)
def test_several_spellings_of_one_hidden_id_match_an_absent_id(package, seed, reverse, action):
    root, _ = package
    completed = subprocess.run(
        [sys.executable, "-c", _SPELLINGS_SCRIPT, str(root), action, "1" if reverse else "0"],
        capture_output=True,
        text=True,
        timeout=240,
        env={**os.environ, "PYTHONHASHSEED": seed},
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    hidden, missing = json.loads(completed.stdout.strip().splitlines()[-1])
    assert hidden == missing
    import re

    assert not re.search(r"\b[0-9a-f]{32}\b", json.dumps(hidden))


@pytest.mark.parametrize(
    "surface", ["mcp run", "mcp validate", "mcp sql", "http /query", "http /validate"]
)
def test_a_policy_rationale_naming_a_hidden_object_is_not_returned(runtimes, package, surface):
    _, config = package
    deny = replace(
        access_policy("deny", ORDERS, REVENUE),
        id="policy.test.mixed_deny",
        rationale=f"Block orders and {REVENUE} for support",
    )
    runtime = runtimes(visibility_policy("hidden", REVENUE), deny)
    query = _select({"measure": ORDERS})
    if surface.startswith("mcp"):
        arguments = {"query": query, "mode": surface.split()[1], "policy_context": CALLER}
        response = mcp_call(runtime, "execute", arguments)
    else:
        response = http_call(runtime, surface.split()[1], {**query, "policy_context": CALLER})
    assert "POLICY_DENIED" in json.dumps(response)
    serialized = json.dumps(response)
    assert deny.id not in serialized
    assert deny.rationale not in serialized
    hidden = engine_hidden(
        with_policies(config, visibility_policy("hidden", REVENUE), deny), CALLER
    )
    assert not leaks(response, hidden_tokens(config, hidden), request=query)
    card = mcp_call(
        runtime, "inspect", {"object_id": ORDERS, "verbosity": "full", "policy_context": CALLER}
    )
    assert deny.id not in json.dumps(card)
    assert not leaks(card, hidden_tokens(config, hidden), request=ORDERS)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("kind", ["metric", "measure"])
def test_padded_hidden_ids_match_padded_absent_ids(runtimes, package, action, kind):
    _, config = package
    object_id = OBJECTS[kind]
    governed, missing, hidden = _pair(runtimes, package, action, object_id)
    query = _select({kind: f"  {object_id} "})
    for mode in ("run", "validate", "sql"):
        arguments = {"query": query, "mode": mode, "policy_context": CALLER}
        response = mcp_call(governed, "execute", arguments)
        assert response["ok"] is False
        _check(response, mcp_call(missing, "execute", arguments), config, hidden, query)


GROUPING_SEEDS = {
    "group_by_dimension": ({"group_by": [STORE]}, STORE),
    "time_role": ({"time": {"temporal_role": ORDER_TIME, "grain": "month"}}, ORDER_TIME),
    "nested_entity": (
        {
            "metric_filters": [
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": STORE_ENTITY,
                        "scope_mode": "entity_only",
                        "input": {"measure": ORDERS},
                        "op": ">=",
                        "value": 1,
                    },
                    "op": "=",
                    "value": True,
                }
            ]
        },
        STORE_ENTITY,
    ),
}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("seed", list(GROUPING_SEEDS))
@pytest.mark.parametrize(
    "surface", ["mcp plan", "http /plan", "mcp discover", "http /discover", "http /build-options"]
)
def test_plan_seed_groupings_on_a_hidden_dimension_match_an_absent_one(
    runtimes, package, action, seed, surface
):
    _, config = package
    parts, target = GROUPING_SEEDS[seed]
    governed, missing, hidden = _pair(runtimes, package, action, target)
    query = {**_select({"metric": CUSTOMERS}), **parts}
    call = QUERY_SURFACES[surface]
    _check(call(governed, query), call(missing, query), config, hidden, query)


@pytest.mark.parametrize("action", ACTIONS)
def test_a_conversion_metric_reading_a_hidden_binding_dimension_is_hidden(
    runtimes, package, action
):
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, PRODUCT_TYPE)
    assert CONVERSION in hidden
    query = _select({"metric": CONVERSION})
    for mode in ("run", "validate", "sql"):
        arguments = {"query": query, "mode": mode, "policy_context": CALLER}
        response = mcp_call(governed, "execute", arguments)
        assert response["ok"] is False
        _check(response, mcp_call(missing, "execute", arguments), config, hidden, query)


def test_a_denied_binding_dimension_refuses_the_conversion_query(runtimes):
    runtime = runtimes(access_policy("deny", PRODUCT_TYPE))
    report = runtime.validate({**_select({"metric": CONVERSION}), "policy_context": CALLER})
    assert [issue["code"] for issue in report["errors"]] == ["POLICY_DENIED"]
    assert report["errors"][0]["details"]["blocked_objects"] == [PRODUCT_TYPE]


@pytest.mark.parametrize("action", ACTIONS)
def test_a_metric_over_a_hidden_measure_is_unknown_everywhere(runtimes, package, action):
    """``hidden`` on revenue hides the average order value computed from it, as visible_only does."""
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, REVENUE)
    assert AOV in hidden
    for surface, call in QUERY_SURFACES.items():
        if not surface.startswith("mcp execute") and not surface.startswith("http /"):
            continue
        query = _select({"metric": AOV})
        _check(call(governed, query), call(missing, query), config, hidden, query)
    for _, call in ID_SURFACES.values():
        _check(call(governed, AOV), call(missing, AOV), config, hidden, AOV)


@pytest.mark.parametrize("action", ACTIONS)
def test_a_segment_over_hidden_lifetime_spend_is_unknown(runtimes, package, action):
    _, config = package
    governed, missing, hidden = _pair(runtimes, package, action, LIFETIME_SPEND)
    assert SEGMENT in hidden
    for surface, (_, call) in ID_SURFACES.items():
        if "segment" in surface or "inspect" in surface:
            _check(call(governed, SEGMENT), call(missing, SEGMENT), config, hidden, SEGMENT)


RAW_COLUMN = _select(
    {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {"kind": "literal", "value": True},
        "value": {"kind": "column", "entity": "entity.jaffle_order", "column": "order_total_cents"},
    }
)


@pytest.mark.parametrize("action", ACTIONS)
def test_raw_column_aggregates_are_refused_while_anything_is_hidden(runtimes, action):
    visible = runtimes().validate({**RAW_COLUMN, "policy_context": CALLER})
    refused = runtimes(visibility_policy(action, LIFETIME_SPEND)).validate(
        {**RAW_COLUMN, "policy_context": CALLER}
    )
    assert visible["ok"] is True, visible
    assert [issue["code"] for issue in refused["errors"]] == ["POLICY_DENIED"]
    assert refused["errors"][0]["details"] == {
        "blocked_objects": [],
        "policy_effects": [],
        "policy_violations": [],
    }
    assert LIFETIME_SPEND not in json.dumps(refused)


@pytest.mark.parametrize("action", ACTIONS)
def test_a_visibility_policy_with_no_objects_fails_to_load(tmp_path, action):
    import yaml

    from semantic_rails.errors import SemanticLayerError

    root = copy_package_config(tmp_path, "jaffle_shop")
    path = root / "policies.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["semantic_policies"].append(
        {
            "id": "policy.test.empty",
            "kind": "object_visibility",
            "action": action,
            "object_ids": [],
            "roles": ["support"],
        }
    )
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(root))
    assert raised.value.code == "INVALID_CONFIG"
    assert "non-empty object_ids" in str(raised.value)
