"""Validation alternatives must not disclose policy-hidden catalog objects."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from semantic_rails import policies
from semantic_rails.diagnostics import enrich_expression_ast_error, exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import _enrich_runtime_error
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.test_plan_dimension_visibility import (
    _assert_no_hidden_dimension,
    _hide_customer_district,
    _with_districts,
)
from tests.semantic_rails.test_plan_value_lists import CUSTOMER_DISTRICT, STORE_DISTRICT


@pytest.mark.parametrize("failure", ["typo", "path"])
@pytest.mark.parametrize("visibility", ["hidden", "visible", "unknown"])
@pytest.mark.parametrize("transport", ["validate", "http", "mcp"])
def test_validation_candidates_respect_visibility(
    runtime_factory, monkeypatch, failure, visibility, transport
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    config = replace(
        runtime._config,
        relationships=[],
        path_preferences=[],
        dimensions=[
            replace(dim, entity="entity.jaffle_item", column="product_type")
            if dim.id == CUSTOMER_DISTRICT
            else dim
            for dim in runtime._config.dimensions
        ],
    )
    monkeypatch.setattr(runtime, "_config", config)
    if visibility == "hidden":
        _hide_customer_district(runtime, monkeypatch, audiences=["external"])
    elif visibility == "unknown":
        monkeypatch.setattr(policies, "hidden_object_ids", _unknown_visibility)
    query = {
        "select": [{"expression": {"measure": "measure.jaffle.item_revenue_usd"}, "as": "total"}],
        "group_by": ["dimension.customer_distric" if failure == "typo" else STORE_DISTRICT],
        "policy_context": {"audience": "external"},
        "verbosity": "full",
    }
    try:
        if transport == "validate":
            payload = runtime.validate(query)
        elif transport == "http":
            service = SemanticHTTPService(runtime)
            with pytest.raises(SemanticLayerError) as raised:
                runtime.compile(query)
            payload, status = service.exception_payload(
                raised.value, stage="compile", context=RequestContext(audience="external")
            )
            assert status == 400
        else:
            payload = SemanticLayerMCPAdapter(runtime).call_tool(
                "execute", {"query": query, "mode": "validate"}
            )
        assert payload["ok"] is False
        issue = payload["errors"][0]
        assert issue["code"] == ("OBJECT_NOT_FOUND" if failure == "typo" else "PATH_NOT_FOUND")
        key = "closest_matches" if failure == "typo" else "compatible_group_by_dimensions"
        if visibility == "visible":
            assert CUSTOMER_DISTRICT in issue["details"][key]
            assert any(CUSTOMER_DISTRICT in json.dumps(hint) for hint in issue["recovery_hints"])
        else:
            _assert_no_hidden_dimension(payload)
            if visibility == "unknown":
                assert not issue["details"].get(key)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "field",
    [
        "compatible_dimensions",
        "compatible_group_by_dimensions",
        "compatible_measures",
        "compatible",
        "available_temporal_roles",
        "allowed_temporal_roles",
        "reachable_targets",
    ],
)
@pytest.mark.parametrize("visibility", ["hidden", "visible", "unknown"])
def test_prefilled_catalog_candidates_cannot_bypass_filter(
    package_config_factory, field, visibility
) -> None:
    config, _ = package_config_factory("jaffle_shop")
    hidden = frozenset({CUSTOMER_DISTRICT}) if visibility == "hidden" else frozenset()
    if visibility == "unknown":
        hidden = None
    from semantic_rails.diagnostics import enrich_diagnostic_candidates

    details = {
        field: [CUSTOMER_DISTRICT, STORE_DISTRICT],
        "closest_compatible_measure": CUSTOMER_DISTRICT,
    }
    original = SemanticLayerError(
        "MIXED_GRAIN_INVALID", "Cannot use this grouping", details=details
    )
    enriched = enrich_diagnostic_candidates(original, config, hidden_ids=hidden)
    assert original.details == details
    if visibility == "visible":
        assert enriched is original
    else:
        _assert_no_hidden_dimension(exception_issue(enriched, stage="validate"))
        assert enriched.details[field] == ([STORE_DISTRICT] if visibility == "hidden" else [])


@pytest.mark.parametrize("visibility", ["hidden", "visible", "unknown"])
@pytest.mark.parametrize("prefilled", [False, True])
def test_authored_metric_suggestions_respect_visibility(
    package_config_factory, monkeypatch, visibility, prefilled
) -> None:
    from semantic_rails.expressions import MeasureRefExpr, OffsetWindowExpr

    config, _ = package_config_factory("jaffle_shop")
    metric_id = "metric.customer_district"
    measure_id = config.measures[0].id
    recipe = replace(
        config.metric_recipes[0],
        id=metric_id,
        expression=OffsetWindowExpr(
            input=MeasureRefExpr(measure=measure_id),
            kind="prior_period",
            aggregate="sum",
            unit="month",
            value=1,
        ),
    )
    config = replace(config, metric_recipes=[recipe])
    hidden = frozenset({metric_id}) if visibility == "hidden" else frozenset()
    if visibility == "unknown":
        hidden = None
    if visibility == "hidden":
        config = replace(
            config,
            semantic_policies=[
                SemanticPolicyConfig(
                    id="policy.hide_metric",
                    kind="object_visibility",
                    object_ids=[metric_id],
                    action="hidden",
                )
            ],
        )
    elif visibility == "unknown":
        monkeypatch.setattr(policies, "hidden_object_ids", _unknown_visibility)
    details = {"expression_kind": "prior_period", "received": {"measure": measure_id}}
    if prefilled:
        details["closest_matches"] = [metric_id]
    error = SemanticLayerError("INVALID_EXPRESSION_AST", "Invalid expression", details=details)
    enriched = _enrich_runtime_error(error, config, {})
    assert (
        enrich_expression_ast_error(enriched, config, hidden_ids=hidden).details == enriched.details
    )
    if visibility == "visible":
        assert enriched.details["closest_matches"] == [metric_id]
    else:
        _assert_no_hidden_dimension(exception_issue(enriched, stage="validate"))


def _unknown_visibility(*args, **kwargs):
    raise ValueError("Visibility unavailable")


@pytest.mark.parametrize("source", ["flags", "query"])
def test_cli_error_enrichment_preserves_scoped_visibility(
    package_config_factory, monkeypatch, capsys, source
) -> None:
    import argparse
    from types import SimpleNamespace

    from semantic_rails.cli import app

    config, _ = package_config_factory("jaffle_shop")
    config = replace(
        config,
        dimensions=[replace(config.dimensions[0], id=CUSTOMER_DISTRICT, label="Customer district")],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.hide_district",
                kind="object_visibility",
                object_ids=[CUSTOMER_DISTRICT],
                audiences=["external"],
                action="hidden",
            )
        ],
    )

    def fail(args):
        raise SemanticLayerError(
            "OBJECT_NOT_FOUND",
            "Unknown object",
            details={"object_id": "dimension.customer_distric"},
        )

    args = argparse.Namespace(
        cmd="inspect",
        func=fail,
        audience="external" if source == "flags" else "",
        query_json=json.dumps({"policy_context": {"audience": "external"}})
        if source == "query"
        else None,
    )
    monkeypatch.setattr(app, "build_parser", lambda: SimpleNamespace(parse_args=lambda: args))
    monkeypatch.setattr(app, "_config_for_error_enrichment", lambda args: config)
    with pytest.raises(SystemExit) as raised:
        app.main()
    assert raised.value.code == 1
    _assert_no_hidden_dimension(json.loads(capsys.readouterr().out))


@pytest.mark.parametrize("hidden_ids", [frozenset(), frozenset({"temporal_role.secret"}), None])
def test_time_axis_alternative_does_not_bypass_visibility(package_config_factory, hidden_ids):
    from semantic_rails.diagnostics import enrich_diagnostic_candidates

    config, _ = package_config_factory("jaffle_shop")
    details = {
        "time_axis_recovery": {"temporal_role": "temporal_role.secret", "grain": "day"},
        "anchor_temporal_role": "temporal_role.secret",
    }
    exc = SemanticLayerError("AMBIGUOUS_PATH", "Ambiguous grouping", details=details)
    issue = exception_issue(
        enrich_diagnostic_candidates(exc, config, hidden_ids=hidden_ids), stage="validate"
    )
    assert ("temporal_role.secret" in json.dumps(issue)) == (hidden_ids == frozenset())
