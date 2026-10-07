"""An undeclared request environment never bypasses package governance."""

from dataclasses import replace

import pytest

from semantic_rails.caveats import caveat_warnings
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata import (
    build_options_payload,
    catalog_payload,
    discover_payload,
    inspect_payload,
)
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.planner.plan import plan_payload
from semantic_rails.policies import (
    enforce_query_policies,
    hidden_object_ids,
    policy_effects_for_object,
    query_policy_effects,
    row_filters_for_context,
    withheld_object_ids,
)
from semantic_rails.policy_rules import hidden_policy_ids, visible_only_listed
from semantic_rails.request_context import TrustedAttributes
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticCaveatConfig, SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config

MEASURE = "measure.jaffle.revenue_usd"
QUERY = {"select": [{"expression": {"measure": MEASURE}, "as": "revenue"}]}
POLICIES = {
    "hidden": ("object_visibility", "hidden", {}),
    "visible_only": ("object_visibility", "visible_only", {"roles": ["finance"]}),
    "deny": ("object_access", "deny", {}),
    "withhold_values": ("object_access", "withhold_values", {}),
    "release_label": ("package_release", "label", {"config": {"label": "stable"}}),
    "protected": ("protected_object", "protected", {}),
    "metric_constraint": (
        "metric_constraint",
        "constrain",
        {"config": {"required_group_by": ["dimension.jaffle_store_name"]}},
    ),
    "row_filter": (
        "row_filter",
        "",
        {
            "object_ids": [],
            "config": {
                "dimension": "dimension.jaffle_order_has_food_item",
                "attribute": "has_food",
            },
        },
    ),
    "caveat": None,
    "no_policies": None,
}
# name -> (request context, ordinary scoped policies apply, visible_only is in force)
SCOPES = {
    "declared_match": ({"environment": "production"}, True, True),
    "declared_non_match": ({"environment": "staging"}, False, False),
    "undeclared": ({"environment": "prod"}, None, None),
    "absent": ({}, False, True),
}


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return copy_package_config(
        tmp_path_factory.mktemp("policy_environments"), "jaffle_shop", preseed_db=True
    )


@pytest.fixture(scope="module")
def config(package):
    return replace(load_package_config(str(package)), semantic_policies=[], semantic_caveats=[])


def governed(config, kind):
    if kind == "caveat":
        caveat = SemanticCaveatConfig(
            "caveat.test.scoped",
            "business_event",
            "Revenue needs interpretation.",
            object_ids=[MEASURE],
            environments=["production"],
        )
        return replace(config, semantic_caveats=[caveat])
    if kind == "no_policies":
        return config
    policy_kind, action, fields = POLICIES[kind]
    policy = SemanticPolicyConfig(
        "policy.test.scoped",
        policy_kind,
        action=action,
        environments=["production"],
        **{"object_ids": [MEASURE], **fields},
    )
    return replace(config, semantic_policies=[policy])


def assert_environment_error(code, message, details, allowed):
    assert code == "INVALID_QUERY"
    assert "prod" in message
    assert details == {"environment": "prod", "allowed_environments": allowed}
    assert all(environment in message for environment in allowed)


@pytest.mark.parametrize("kind", POLICIES)
@pytest.mark.parametrize("scope", SCOPES)
def test_every_policy_kind_checks_the_request_environment(
    config, package, monkeypatch, kind, scope
):
    context, applies, in_force = SCOPES[scope]
    context = {**context, "attributes": TrustedAttributes({"has_food": True})}
    engine = Runtime.from_config(governed(config, kind), source_path=str(package))
    query = {**QUERY, "policy_context": context}
    try:
        # A prior successful compile cannot let an invalid environment through a fast path.
        assert engine.compile({**QUERY, "policy_context": {"environment": "staging"}})[
            "rendered_sql"
        ]
        if scope == "undeclared":

            def no_evaluation(*args, **kwargs):
                pytest.fail("undeclared environment reached policy evaluation or SQL output")

            monkeypatch.setattr("semantic_rails.policies._policy_action", no_evaluation)
            monkeypatch.setattr("semantic_rails.policies._policy_matches", no_evaluation)
            monkeypatch.setattr("semantic_rails.policy_rules.policy_action", no_evaluation)
            monkeypatch.setattr("semantic_rails.policy_rules.policy_matches", no_evaluation)
            monkeypatch.setattr("semantic_rails.caveats._match_caveat", no_evaluation)
            monkeypatch.setattr("semantic_rails.compiler.render_select_for_profile", no_evaluation)
            monkeypatch.setattr(engine, "_get_adapter", no_evaluation)
            report = engine.validate(query)
            assert report["ok"] is False
            issue = report["errors"][0]
            assert_environment_error(
                issue["code"], issue["message"], issue["details"], config.package.environments
            )
            for operation in (engine.compile, engine.query):
                with pytest.raises(SemanticLayerError) as exc:
                    operation(query)
                assert_environment_error(
                    exc.value.code, str(exc.value), exc.value.details, config.package.environments
                )
            result = SemanticLayerMCPAdapter(engine).call_tool(
                "execute", {"query": query, "mode": "run"}
            )
            assert result["ok"] is False
            assert result["errors"][0]["code"] == "INVALID_QUERY"
            assert not any(result.get(field) for field in ("rows", "sql", "rendered_sql"))
            return

        denied = (kind == "visible_only" and in_force) or (
            applies and kind in {"hidden", "deny", "withhold_values", "metric_constraint"}
        )
        report = engine.validate(query)
        assert report["ok"] is (not denied), report
        if denied:
            assert report["errors"][0]["code"] == "POLICY_DENIED"
            return
        compiled = engine.compile(query)
        if kind == "caveat":
            assert (
                any(
                    warning["code"] == "SEMANTIC_CAVEAT_APPLIED"
                    for warning in compiled.get("warnings", [])
                )
                is applies
            )
        elif kind == "row_filter":
            assert bool(row_filters_for_context(engine._config, context)) is applies
        else:
            assert bool(
                policy_effects_for_object(
                    engine._config, MEASURE, environment=context.get("environment", "")
                )
            ) is (applies and kind != "no_policies")
    finally:
        engine.close()


@pytest.mark.parametrize("environment", ["production", " production "])
def test_caveat_matches_declared_environment_with_padding(config, package, environment):
    config = governed(config, "caveat")
    engine = Runtime.from_config(config, source_path=str(package))
    try:
        compiled = engine.compile({**QUERY, "policy_context": {"environment": environment}})
        warnings = compiled.get("warnings", [])
        assert any(warning["code"] == "SEMANTIC_CAVEAT_APPLIED" for warning in warnings)
    finally:
        engine.close()


DIRECT_GATES = {
    "hidden": lambda config: hidden_object_ids(config, environment="prod"),
    "hidden_policy": lambda config: hidden_policy_ids(config, environment="prod"),
    "visible_only": lambda config: visible_only_listed(config, environment="prod"),
    "object_effects": lambda config: policy_effects_for_object(config, MEASURE, environment="prod"),
    "query_effects": lambda config: query_policy_effects(config, [MEASURE], environment="prod"),
    "enforce": lambda config: enforce_query_policies(config, [MEASURE], environment="prod"),
    "withheld": lambda config: withheld_object_ids(config, [MEASURE], environment="prod"),
    "row_filter": lambda config: row_filters_for_context(config, {"environment": "prod"}),
    "caveat": lambda config: caveat_warnings(
        config, {}, {"policy_context": {"environment": "prod"}}
    ),
}


@pytest.mark.parametrize("gate", DIRECT_GATES)
def test_direct_policy_gates_refuse_even_without_a_policy_match(config, gate, monkeypatch):
    def no_match(*args, **kwargs):
        return False

    monkeypatch.setattr("semantic_rails.policies._policy_matches", no_match)
    monkeypatch.setattr("semantic_rails.policy_rules.policy_matches", no_match)
    with pytest.raises(SemanticLayerError) as exc:
        DIRECT_GATES[gate](governed(config, "deny"))
    assert_environment_error(
        exc.value.code, str(exc.value), exc.value.details, config.package.environments
    )


@pytest.mark.parametrize("environment", ["", "prod"])
def test_package_declaring_no_environments_preserves_only_absent_context(
    config, package, environment
):
    config = replace(config, package=replace(config.package, environments=[]))
    engine = Runtime.from_config(config, source_path=str(package))
    try:
        report = engine.validate({**QUERY, "policy_context": {"environment": environment}})
        assert report["ok"] is (not environment)
        if environment:
            issue = report["errors"][0]
            assert_environment_error(issue["code"], issue["message"], issue["details"], [])
    finally:
        engine.close()


SURFACES = {
    "catalog": lambda engine, context: catalog_payload(engine, policy_context=context),
    "discover": lambda engine, context: discover_payload(
        engine, terms="revenue", partial_query={"policy_context": context}
    ),
    "inspect": lambda engine, context: inspect_payload(
        engine, object_id=MEASURE, partial_query={"policy_context": context}
    ),
    "build_options": lambda engine, context: build_options_payload(
        engine, partial_query={"policy_context": context}
    ),
    "valid_values": lambda engine, context: valid_values_payload(
        engine,
        dimension_id="dimension.jaffle_order_has_food_item",
        query={"policy_context": context},
    ),
    "plan": lambda engine, context: plan_payload(
        engine, intent="revenue", partial_query={"policy_context": context}
    ),
    "validate": lambda engine, context: engine.validate({**QUERY, "policy_context": context}),
    "compile": lambda engine, context: engine.compile({**QUERY, "policy_context": context}),
    "execute": lambda engine, context: engine.query({**QUERY, "policy_context": context}),
    "segment_validate": lambda engine, context: engine.segment_validate(
        "segment.test.missing", policy_context=context
    ),
    "segment_explain": lambda engine, context: engine.segment_explain(
        "segment.test.missing", policy_context=context
    ),
    "segment_preview": lambda engine, context: engine.segment_preview(
        "segment.test.missing", policy_context=context
    ),
}


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("surface", SURFACES)
def test_shared_request_boundary_refuses_before_grants_or_metadata(
    config, package, surface, granted, monkeypatch
):
    context = {"environment": "prod"}
    if granted:
        context["metric_allowlist"] = ["metric.sales.aov_usd"]
    engine = Runtime.from_config(governed(config, "deny"), source_path=str(package))

    def no_evaluation(*args, **kwargs):
        pytest.fail("undeclared environment reached policy evaluation or metadata")

    monkeypatch.setattr("semantic_rails.policies._policy_matches", no_evaluation)
    monkeypatch.setattr(engine, "catalog", no_evaluation)
    try:
        if surface in {"validate", "segment_validate"}:
            result = SURFACES[surface](engine, context)
            assert result["ok"] is False
            issue = result["errors"][0]
            assert_environment_error(
                issue["code"], issue["message"], issue["details"], config.package.environments
            )
        else:
            with pytest.raises(SemanticLayerError) as exc:
                SURFACES[surface](engine, context)
            assert_environment_error(
                exc.value.code, str(exc.value), exc.value.details, config.package.environments
            )
    finally:
        engine.close()
