"""A ``visible_only`` policy shows objects only to the contexts it names.

A sensitive revenue metric and its measure are visible only to the finance role. Every other
context (no roles, an unknown role, a support role, the wrong audience) sees neither them nor
anything computed from them on any surface, and every query reading them is refused; the
finance role gets the reference-SQL number.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.diagnostics import enrich_object_not_found
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
)
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.plan import plan_payload
from semantic_rails.policies import enforce_query_policies, hidden_object_ids
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime, runtime_request_scope
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails import test_route_clarification as route
from tests.semantic_rails.conftest import copy_package_config, opened

REVENUE_METRIC = "metric.sales.revenue"
REVENUE = "measure.jaffle.revenue_usd"
AOV = "metric.sales.aov_usd"
SEGMENT = "segment.jaffle.revenue_customers"
CUSTOMERS = "metric.sales.customer_count"
STORE = "dimension.jaffle_store_name"
RESTRICTED_DIMENSION = "dimension.jaffle_order_has_food_item"
# Listed by the policy, then computed from them.
RESTRICTED = (REVENUE_METRIC, REVENUE, AOV, SEGMENT)
FINANCE_ONLY = {
    "id": "policy.test.revenue_finance_only",
    "kind": "object_visibility",
    "action": "visible_only",
    "object_ids": [REVENUE_METRIC, REVENUE],
    "roles": ["finance"],
    "rationale": "Revenue is visible only to the finance role.",
}
EXTRA_METRIC = {
    "metrics": {
        "sales.revenue": {
            "as": REVENUE_METRIC,
            "label": "Revenue",
            "description": "Total order revenue.",
            "kind": "aggregate",
            "measure": "revenue_usd",
            "value_type": "currency",
            "currency": "USD",
            "temporal_role": "temporal_role.jaffle_order_time",
        }
    }
}
EXTRA_SEGMENT = {
    "segments": {
        "customer.revenue": {
            "id": SEGMENT,
            "label": "Revenue customers",
            "description": "Customers with at least 100 USD of order revenue.",
            "entity": "entity.jaffle_customer",
            "basis_metric": CUSTOMERS,
            "preview_dimensions": ["dimension.jaffle_customer_name"],
            "membership": {
                "metric_filters": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": "entity.jaffle_customer",
                            "scope_mode": "entity_only",
                            "input": {"measure": REVENUE},
                            "op": ">=",
                            "value": 100,
                        },
                        "op": "=",
                        "value": True,
                    }
                ]
            },
        }
    }
}
# name -> (policy_context, eligible)
CONTEXTS = {
    "no_roles": ({}, False),
    "unknown_role": ({"roles": ["intern"]}, False),
    "support": ({"roles": ["support"]}, False),
    "finance": ({"roles": ["finance"]}, True),
    "finance_any_case": ({"roles": [" Finance "]}, True),
    "finance_and_support": ({"roles": ["finance", "support"]}, True),
}
BY_STORE = {
    "select": [{"expression": {"metric": REVENUE_METRIC}, "as": "revenue"}],
    "group_by": [STORE],
}


def _threshold(input_: dict[str, Any]) -> dict[str, Any]:
    predicate = {"kind": "metric_predicate", "entity": "entity.jaffle_customer"}
    predicate |= {"scope_mode": "entity_only", "input": input_, "op": ">=", "value": 100}
    return {"expression": predicate, "op": "=", "value": True}


# Every way to read revenue without naming the policy's objects as a catalog would.
BYPASSES = {
    "guessed_id": BY_STORE,
    "derived_metric": {"select": [{"expression": {"metric": AOV}, "as": "aov"}]},
    "raw_measure": {"select": [{"expression": {"measure": REVENUE}, "as": "revenue"}]},
    "inline_expression": {
        "select": [
            {
                "expression": {
                    "kind": "arithmetic",
                    "op": "multiply",
                    "left": {"measure": REVENUE},
                    "right": {"kind": "literal", "value": 2},
                },
                "as": "doubled",
            }
        ]
    },
    "metric_filter": {
        "select": [{"expression": {"metric": CUSTOMERS}, "as": "customers"}],
        "metric_filters": [_threshold({"measure": REVENUE})],
    },
    "rank_by_it": {
        **BY_STORE,
        "order_by": [{"field": "revenue", "direction": "DESC"}],
        "limit": 3,
    },
    "conditional_raw_value": {
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "sum",
                    "condition": {"kind": "literal", "value": True},
                    "value": {
                        "kind": "column",
                        "entity": "entity.jaffle_order",
                        "column": "order_total_cents",
                    },
                },
                "as": "r",
            }
        ],
        "group_by": [STORE],
    },
    "conditional_raw_condition": {
        "select": [
            {
                "expression": {
                    "kind": "aggregate_if",
                    "aggregation": "count",
                    "condition": {
                        "kind": "comparison",
                        "op": ">",
                        "left": {
                            "kind": "column",
                            "entity": "entity.jaffle_order",
                            "column": "order_total_cents",
                        },
                        "right": {"kind": "literal", "value": 0},
                    },
                },
                "as": "r",
            }
        ],
        "group_by": [STORE],
    },
    "restricted_dimension_filter": {
        "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
        "where": [{"field": RESTRICTED_DIMENSION, "op": "=", "value": True}],
    },
}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = copy_package_config(
        tmp_path_factory.mktemp("visible_only"), "jaffle_shop", preseed_db=True
    )
    _write(root / "metrics" / "core" / "revenue.yml", EXTRA_METRIC)
    _write(root / "segments" / "revenue.yml", EXTRA_SEGMENT)
    _write(root / "policies.yml", {"semantic_policies": [FINANCE_ONLY]})
    return root


@pytest.fixture(scope="module")
def engine(package: Path) -> Iterator[Runtime]:
    runtime = Runtime.from_path(str(package))
    try:
        yield opened(runtime)
    finally:
        runtime.close()


def _engine(package: Path, *policies: dict[str, Any]) -> Runtime:
    config = load_package_config(str(package))
    rows = [SemanticPolicyConfig(**row) for row in policies]
    return Runtime.from_config(replace(config, semantic_policies=rows), source_path=str(package))


def _with(query: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {**query, "policy_context": context}


def _refusals(engine: Runtime, query: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    """The code, details and message of validate, compile, execute and MCP execute."""
    report = engine.validate(query)
    assert report["ok"] is False, report
    issue = report["errors"][0]
    outcomes = [(issue["code"], issue.get("details", {}), issue["message"])]
    for call in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as exc:
            call(query)
        outcomes.append((exc.value.code, exc.value.details, str(exc.value)))
    tool = SemanticLayerMCPAdapter(engine).call_tool("execute", {"query": query, "mode": "run"})
    assert tool["ok"] is False, tool
    for payload in (report, tool):
        for field in ("sql", "rendered_sql", "sql_plan", "rows"):
            assert not payload.get(field), payload
    issue = tool["errors"][0]
    outcomes.append((issue["code"], issue.get("details", {}), issue["message"]))
    return outcomes


def _mentions(payload: Any, ids: Iterable[str] = RESTRICTED) -> set[str]:
    """Restricted ids anywhere in a payload, except in other objects' authored descriptions."""

    def authored(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: authored(value) for key, value in node.items() if key != "description"}
        return [authored(value) for value in node] if isinstance(node, list | tuple) else node

    text = json.dumps(authored(payload), default=str)
    return {
        object_id
        for object_id in ids
        if re.search(rf"(?<![\w.]){re.escape(object_id)}(?![\w.])", text)
    }


def _reference(package: Path) -> list[tuple[str, float]]:
    con = duckdb.connect(str(package / "jaffle_shop.duckdb"), read_only=True)
    try:
        rows = con.execute(
            "SELECT s.store_name, SUM(o.order_total_cents / 100.0) FROM jaffle_order o "
            "JOIN jaffle_store s ON s.store_id = o.store_id GROUP BY 1"
        ).fetchall()
    finally:
        con.close()
    return sorted((name, round(float(total), 2)) for name, total in rows)


# Each metadata surface, called as a context: its payload, or the refusal it raised.
SURFACES: dict[str, Callable[[Runtime, dict[str, Any]], Any]] = {
    "catalog": lambda engine, context: catalog_payload(
        engine, view="full", verbosity="full", policy_context=context
    ),
    "discover": lambda engine, context: discover_payload(
        engine, terms="revenue", partial_query={"policy_context": context}, limit=50
    ),
    "build_options": lambda engine, context: build_options_payload(
        engine, partial_query={"policy_context": context}, focus_terms="revenue", limit=50
    ),
    "inspect": lambda engine, context: inspect_payload(
        engine, object_id=REVENUE_METRIC, partial_query={"policy_context": context}
    ),
    "valid_values": lambda engine, context: valid_values_payload(
        engine,
        dimension_id=STORE,
        query=_with(BY_STORE, context),
        allow_live_query=True,
        include_counts=True,
    ),
    "plan": lambda engine, context: plan_payload(
        engine, intent="total revenue by store name", partial_query={"policy_context": context}
    ),
}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", CONTEXTS)
def test_metadata_surfaces_show_revenue_only_to_finance(engine, surface, name):
    context, eligible = CONTEXTS[name]
    try:
        payload = SURFACES[surface](engine, context)
    except SemanticLayerError as exc:
        assert not eligible, exc
        assert exc.code in {"OBJECT_NOT_FOUND", "POLICY_DENIED"}, exc
        return
    if eligible:
        assert _mentions(payload) & {REVENUE_METRIC, REVENUE}, surface
        return
    assert surface != "inspect", payload
    named = _mentions(payload, hidden_object_ids(engine._config, roles=["support"]))
    assert not named, (surface, named)
    if surface == "plan":
        assert payload["status"] != "ok" or "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize(("roles", "eligible"), [(["support"], False), (["finance"], True)])
def test_cards_of_visible_objects_name_revenue_only_for_finance(engine, roles, eligible):
    """Related measures and metrics, companions and starter queries on every other card."""
    partial = {"policy_context": {"roles": roles}}
    config = engine._config
    restricted = hidden_object_ids(config, roles=["support"])
    named: set[str] = set()
    for rows in (config.entities, config.dimensions, config.measures, config.metric_recipes):
        for row in rows:
            if row.id not in restricted:
                card = inspect_payload(engine, object_id=row.id, partial_query=partial)
                named |= _mentions(card, restricted)
    assert bool(named) is eligible, named


@pytest.mark.parametrize(("roles", "eligible"), [(["support"], False), (["finance"], True)])
def test_full_catalog_payload_filters_restricted_companions(tmp_path, roles, eligible):
    root = copy_package_config(tmp_path, "jaffle_shop")
    path = root / "package.yml"
    data = yaml.safe_load(path.read_text())
    data["package"]["schema_strict"] = False
    _write(path, data)
    # Keep only regular models in this non-strict catalog fixture.
    (root / "models" / "core" / "daily_metrics.yml").unlink()
    (root / "models" / "core" / "monthly_metrics.yml").unlink()
    (root / "metrics" / "core" / "time_series_metrics.yml").unlink()
    path = root / "models" / "core" / "orders.yml"
    data = yaml.safe_load(path.read_text())
    keys = ("comparison_peers", "clock_variants", "preferred_companion_metrics")
    for key in keys:
        data["model"]["measures"]["order_cost_usd"][key] = [REVENUE_METRIC, CUSTOMERS]
    _write(path, data)
    _write(root / "metrics" / "core" / "revenue.yml", EXTRA_METRIC)
    _write(root / "policies.yml", {"semantic_policies": [FINANCE_ONLY]})
    runtime = Runtime.from_path(str(root))
    try:
        catalog = catalog_payload(
            runtime, view="full", verbosity="full", policy_context={"roles": roles}
        )
        assert (REVENUE_METRIC in _mentions(catalog)) is eligible
        measure = next(
            (row for row in catalog["measures"] if row["id"] == "measure.jaffle.order_cost_usd"),
            None,
        )
        # A measure naming the restricted metric (as a peer) is restricted with it.
        assert (measure is not None) is eligible
        if measure is None:
            return
        for key in keys:
            assert measure["payload"][key] == (
                [REVENUE_METRIC, CUSTOMERS] if eligible else [CUSTOMERS]
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("name", CONTEXTS)
def test_intent_subjects_name_revenue_only_for_finance(engine, name):
    context, eligible = CONTEXTS[name]

    @runtime_request_scope
    def subjects(runtime: Runtime, *, partial_query: dict[str, Any]) -> set[str]:
        intent = parse_intent(runtime, "total revenue by store name").to_dict()
        return {row["id"] for row in intent["subjects"]}

    found = subjects(engine, partial_query={"policy_context": context})
    assert bool(found & {REVENUE_METRIC, REVENUE}) is eligible


@pytest.mark.parametrize("bypass", BYPASSES)
@pytest.mark.parametrize("name", CONTEXTS)
def test_every_query_reading_revenue_is_refused_outside_finance(engine, package, bypass, name):
    context, eligible = CONTEXTS[name]
    query = _with(BYPASSES[bypass], context)
    runtime = (
        _engine(
            package,
            {**FINANCE_ONLY, "object_ids": [*FINANCE_ONLY["object_ids"], RESTRICTED_DIMENSION]},
        )
        if bypass == "restricted_dimension_filter"
        else engine
    )
    try:
        if eligible:
            assert runtime.validate(query)["ok"] is True
            return
        hidden = hidden_object_ids(runtime._config, roles=context.get("roles", []))
        hidden_names = hidden | {
            row.label
            for rows in (
                runtime._config.entities,
                runtime._config.dimensions,
                runtime._config.measures,
                runtime._config.metric_recipes,
                runtime._config.segments,
            )
            for row in rows
            if row.id in hidden and row.label
        }
        # A restricted object the query names is unknown; raw columns name none and are denied.
        named = hidden & set(re.findall(r"[\w.]+", json.dumps(query)))
        for code, details, message in _refusals(runtime, query):
            assert code == ("OBJECT_NOT_FOUND" if named else "POLICY_DENIED")
            # Only the caller's own ids and visible suggestions may be named.
            suggested = set(details.get("closest_matches", []))
            assert not suggested & hidden, details
            serialized = json.dumps(details)
            for object_id in sorted(named | suggested, key=len, reverse=True):
                serialized = serialized.replace(object_id, "")
                message = message.replace(object_id, "")
            for name in hidden_names:
                assert name.casefold() not in serialized.casefold(), details
                assert name.casefold() not in message.casefold(), message
            if not named:
                assert details["blocked_objects"] == []
    finally:
        if runtime is not engine:
            runtime.close()


@pytest.mark.parametrize("name", [name for name, (_, eligible) in CONTEXTS.items() if eligible])
def test_finance_gets_the_reference_number(engine, package, name):
    rows = engine.query(_with(BY_STORE, CONTEXTS[name][0]))["rows"]
    assert sorted((row[STORE], round(float(row["revenue"]), 2)) for row in rows) == _reference(
        package
    )


@pytest.mark.parametrize("bypass", ["conditional_raw_value", "conditional_raw_condition"])
@pytest.mark.parametrize("restricted", [True, False])
def test_conditional_raw_columns_match_reference_when_unrestricted(package, bypass, restricted):
    runtime = _engine(package, *([FINANCE_ONLY] if restricted else []))
    context = {"roles": ["finance" if restricted else "support"]}
    value = (
        "SUM(o.order_total_cents)"
        if bypass == "conditional_raw_value"
        else "COUNT(CASE WHEN o.order_total_cents > 0 THEN 1 END)"
    )
    try:
        rows = opened(runtime).query(_with(BYPASSES[bypass], context))["rows"]
        with duckdb.connect(str(package / "jaffle_shop.duckdb"), read_only=True) as con:
            expected = con.execute(
                f"SELECT s.store_name, {value} FROM jaffle_order o "
                "JOIN jaffle_store s ON s.store_id = o.store_id GROUP BY 1"
            ).fetchall()
        assert sorted((row[STORE], row["r"]) for row in rows) == sorted(expected)
    finally:
        runtime.close()


@pytest.mark.parametrize("bypass", ["conditional_raw_value", "conditional_raw_condition"])
def test_raw_conditional_aggregate_cannot_bypass_gate_without_binding(engine, bypass):
    with pytest.raises(SemanticLayerError) as exc:
        enforce_query_policies(engine._config, [], roles=["support"], query=BYPASSES[bypass])
    assert exc.value.code == "POLICY_DENIED"
    assert exc.value.details == {
        "blocked_objects": [],
        "policy_effects": [],
        "policy_violations": [],
    }


@pytest.mark.parametrize("name", CONTEXTS)
def test_a_near_miss_never_suggests_revenue_outside_finance(engine, name):
    context, eligible = CONTEXTS[name]
    typo = {"select": [{"expression": {"metric": "metric.sales.revenu"}, "as": "revenue"}]}
    report = engine.validate(_with(typo, context))
    assert report["errors"][0]["code"] == "OBJECT_NOT_FOUND"
    assert (REVENUE_METRIC in _mentions(report)) is eligible


@pytest.mark.parametrize("operation", ["segment_validate", "segment_explain", "segment_preview"])
@pytest.mark.parametrize("name", CONTEXTS)
def test_a_segment_over_revenue_is_refused_outside_finance(engine, operation, name):
    context, eligible = CONTEXTS[name]
    call = getattr(engine, operation)
    if eligible:
        result = call(SEGMENT, policy_context=context)
        assert result.get("ok", True) is not False, result
        return
    if operation == "segment_validate":
        report = call(SEGMENT, policy_context=context)
        assert report["errors"][0]["code"] == "OBJECT_NOT_FOUND"
        return
    with pytest.raises(SemanticLayerError) as exc:
        call(SEGMENT, policy_context=context)
    assert exc.value.code == "OBJECT_NOT_FOUND"


def test_unrelated_objects_stay_queryable(engine):
    query = {"select": [{"expression": {"metric": CUSTOMERS}, "as": "customers"}]}
    for context, _ in CONTEXTS.values():
        assert engine.query(_with(query, context))["rows"]


# name -> (extra policy fields, policy_context, eligible)
SCOPES = {
    "audience_listed": (
        {"audiences": ["internal"]},
        {"roles": ["finance"], "audience": "internal"},
        True,
    ),
    "audience_other": (
        {"audiences": ["internal"]},
        {"roles": ["finance"], "audience": "partner"},
        False,
    ),
    "audience_missing": ({"audiences": ["internal"]}, {"roles": ["finance"]}, False),
    "audience_only": ({"roles": [], "audiences": ["internal"]}, {"audience": "internal"}, True),
    "audience_only_other": (
        {"roles": [], "audiences": ["internal"]},
        {"audience": "partner"},
        False,
    ),
    "dev_policy_in_production": (
        {"environments": ["development"]},
        {"environment": "production"},
        True,
    ),
    "dev_policy_in_development": (
        {"environments": ["development"]},
        {"environment": "development"},
        False,
    ),
    "dev_policy_without_environment": ({"environments": ["development"]}, {}, False),
    "prod_policy_finance": (
        {"environments": ["production"]},
        {"environment": "production", "roles": ["finance"]},
        True,
    ),
    "prod_request_against_production_policy": (
        {"environments": ["production"]},
        {"environment": "prod"},
        None,
    ),
}


@pytest.mark.parametrize("name", SCOPES)
def test_scoping_separates_applicability_from_eligibility(package, name):
    extra, context, visible = SCOPES[name]
    runtime = _engine(package, {**FINANCE_ONLY, **extra})
    try:
        if visible is None:
            with pytest.raises(SemanticLayerError) as exc:
                hidden_object_ids(runtime._config, environment=context["environment"])
            assert exc.value.code == "INVALID_QUERY"
            assert {code for code, _, _ in _refusals(runtime, _with(BY_STORE, context))} == {
                "INVALID_QUERY"
            }
            return
        hidden = hidden_object_ids(
            runtime._config,
            environment=context.get("environment", ""),
            audience=context.get("audience", ""),
            roles=context.get("roles", []),
        )
        assert bool(set(RESTRICTED) & hidden) is not visible
        assert set(RESTRICTED) <= hidden or not set(RESTRICTED) & hidden
        query = _with(BY_STORE, context)
        if visible:
            assert runtime.validate(query)["ok"] is True
        else:
            assert {code for code, _, _ in _refusals(runtime, query)} == {"OBJECT_NOT_FOUND"}
    finally:
        runtime.close()


def test_every_visible_only_policy_must_be_met(package):
    """Adding a policy never widens: finance alone fails a second policy naming treasury."""
    treasury = {**FINANCE_ONLY, "id": "policy.test.treasury", "roles": ["treasury"]}
    runtime = _engine(package, FINANCE_ONLY, treasury)
    try:
        for roles, visible in (
            (["finance"], False),
            (["treasury"], False),
            (["finance", "treasury"], True),
        ):
            assert (
                REVENUE_METRIC in hidden_object_ids(runtime._config, roles=roles)
            ) is not visible
    finally:
        runtime.close()


@pytest.mark.parametrize("action", ["deny", "hidden"])
def test_explicit_restrictions_still_apply_to_eligible_contexts(package, action):
    kind = "object_access" if action == "deny" else "object_visibility"
    restriction = {"id": "policy.test.restrict", "kind": kind, "action": action}
    restriction |= {"object_ids": [REVENUE], "roles": ["finance"]}
    runtime = _engine(package, FINANCE_ONLY, restriction)
    try:
        query = _with(BY_STORE, {"roles": ["finance"]})
        for code, details, _ in _refusals(runtime, query):
            # Hidden from finance, the metric reading revenue is unknown; denied, it is refused.
            assert code == ("OBJECT_NOT_FOUND" if action == "hidden" else "POLICY_DENIED")
            if action == "deny":
                assert REVENUE in details["blocked_objects"]
                assert set(details["blocked_objects"]) <= set(RESTRICTED)
    finally:
        runtime.close()


def test_the_query_gate_reads_every_bound_object_not_policy_matches(engine, monkeypatch):
    """Forcing the per-policy effects to report nothing still refuses a restricted read: the
    caller's view has no such object to bind."""
    monkeypatch.setattr("semantic_rails.policies.query_policy_effects", lambda *a, **k: [])
    query = {"select": [{"expression": {"metric": AOV}, "as": "aov"}]}
    codes = {code for code, _, _ in _refusals(engine, _with(query, {"roles": ["support"]}))}
    assert codes == {"OBJECT_NOT_FOUND"}
    assert enforce_query_policies(engine.package_config, [AOV], roles=["finance"]) == []


@pytest.mark.parametrize(
    ("roles", "eligible"), [([], False), (["writer"], False), (["reader"], True)]
)
@pytest.mark.parametrize("restricted", [route.OWNER, route.OWNER_ROUTE[0]])
def test_route_notes_offer_a_restricted_waypoint_only_to_its_roles(
    tmp_path, monkeypatch, restricted, roles, eligible
):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")
    package = route._write_package(tmp_path)
    policy = SemanticPolicyConfig(
        id="policy.test.owner_readers",
        kind="object_visibility",
        action="visible_only",
        object_ids=[restricted],
        roles=["reader"],
    )
    config = replace(load_package_config(str(package)), semantic_policies=[policy])
    runtime = Runtime.from_config(config, source_path=str(package))
    query = {
        **route.BALANCE_BY_DISTRICT,
        "route_decisions": [{**route.DIAMOND_ROW, "relationship_path": route.BRANCH_ROUTE}],
        "policy_context": {"roles": roles},
    }
    try:
        out = runtime.query(query)
    finally:
        runtime.close()
    assert route._rows(out, ["dimension.bank_district_name", "v"]) == route._gold(route.BY_BRANCH)
    (details,) = route._chosen_by_query(out)
    disclosed = [
        object_id for object_id in [route.OWNER, *route.OWNER_ROUTE] if object_id in json.dumps(out)
    ]
    assert bool(details["route_alternatives"]) is eligible
    assert bool(disclosed) is eligible


@pytest.mark.parametrize(("roles", "eligible"), [(["support"], False), (["finance"], True)])
def test_a_restricted_dimension_takes_its_values_and_domain_with_it(package, roles, eligible):
    domain = "value_domain.jaffle_store_store_name"
    runtime = _engine(package, {**FINANCE_ONLY, "object_ids": [STORE]})
    context = {"roles": roles}
    try:
        catalog = json.dumps(catalog_payload(runtime, view="full", policy_context=context))
        assert (domain in catalog) is eligible
        found = discover_payload(
            runtime, terms="Brooklyn store name", partial_query={"policy_context": context}
        )
        assert (STORE in json.dumps(found)) is eligible
        if eligible:
            assert valid_values_payload(runtime, dimension_id=STORE, query=_with({}, context))
            return
        with pytest.raises(SemanticLayerError) as exc:
            valid_values_payload(runtime, dimension_id=STORE, query=_with({}, context))
        assert exc.value.code == "OBJECT_NOT_FOUND"
    finally:
        runtime.close()


@pytest.mark.parametrize(("roles", "eligible"), [(("support",), False), (("finance",), True)])
def test_a_metric_grant_does_not_widen_visible_only(engine, roles, eligible):
    """A resource grant listing the metric still needs one of the policy's roles."""
    context = RequestContext(
        roles=roles,
        metric_allowlist=(REVENUE_METRIC, CUSTOMERS),
        dimension_allowlist=(STORE,),
    ).to_policy_context()
    catalog = json.dumps(catalog_payload(engine, policy_context=context))
    assert CUSTOMERS in catalog
    assert (REVENUE_METRIC in _mentions(json.loads(catalog))) is eligible
    query = _with(BY_STORE, context)
    if eligible:
        assert engine.query(query)["rows"]
        return
    with pytest.raises(SemanticLayerError) as exc:
        engine.query(query)
    assert exc.value.code == "RESOURCE_ACCESS_DENIED"


def test_diagnostics_without_a_context_suggest_no_restricted_object(engine):
    """A caller with no context is not eligible: their view offers no restricted candidate."""
    missing = SemanticLayerError(
        "OBJECT_NOT_FOUND", "Unknown object", details={"object_id": "metric.sales.revenu"}
    )
    enriched = enrich_object_not_found(missing, engine.view_for({}))
    assert not set(enriched.details.get("closest_matches", [])) & set(RESTRICTED)


@pytest.mark.parametrize(("roles", "eligible"), [(["support"], False), (["finance"], True)])
def test_finance_keeps_a_visible_only_conversion_expression(package, roles, eligible):
    conversion = "metric.sales.session_to_order_conversion_rate_7d"
    runtime = _engine(package, {**FINANCE_ONLY, "object_ids": [conversion]})
    partial = {"policy_context": {"roles": roles}}
    try:
        if not eligible:
            with pytest.raises(SemanticLayerError) as exc:
                inspect_payload(runtime, object_id=conversion, partial_query=partial)
            assert exc.value.code == "OBJECT_NOT_FOUND"
            return
        card = inspect_payload(runtime, object_id=conversion, partial_query=partial)["card"]
        assert card["conversion"]["expression"]["kind"] == "conversion"
    finally:
        runtime.close()


def test_unbindable_objects_are_restricted_when_anything_is(engine, monkeypatch):
    """An object whose reads cannot be bound is restricted, never assumed independent."""
    config = replace(engine._config, semantic_policies=list(engine._config.semantic_policies))

    def unbindable(config, object_ids):
        raise SemanticLayerError("INVALID_QUERY", "cannot bind")

    monkeypatch.setattr("semantic_rails.visible_view.bind_metadata_objects", unbindable)
    assert CUSTOMERS in hidden_object_ids(config, roles=["support"])
    assert CUSTOMERS not in hidden_object_ids(config, roles=["finance"])
