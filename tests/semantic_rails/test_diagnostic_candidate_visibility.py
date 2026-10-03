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

ORDER_TIME = "temporal_role.jaffle_order_time"
FIRST_ORDER = "temporal_role.jaffle_customer_first_order_at"
LATEST_ORDER = "temporal_role.jaffle_customer_latest_ordered_at"
FISCAL_DAY = "temporal_role.jaffle_fiscal_calendar_day"
CUSTOMERS = "measure.jaffle.customer_count"
ORDERS = "measure.jaffle.order_count"


def _producer_case(runtime, monkeypatch, producer):
    query = {
        "select": [{"expression": {"measure": ORDERS}, "as": "orders"}],
        "time": {"temporal_role": ORDER_TIME, "grain": "month"},
        "policy_context": {"audience": "external"},
        "verbosity": "full",
    }
    if producer == "alias":
        _with_districts(runtime, monkeypatch)
        monkeypatch.setattr(
            runtime,
            "_config",
            replace(
                runtime._config,
                dimensions=[
                    replace(row, aliases=["district"])
                    if row.id in {CUSTOMER_DISTRICT, STORE_DISTRICT}
                    else row
                    for row in runtime._config.dimensions
                ],
            ),
        )
        query["select"][0]["expression"] = {
            "kind": "aggregate",
            "measure": ORDERS,
            "filter": {"all": [{"dimension": "district", "op": "=", "value": "Central"}]},
        }
        return (
            query,
            CUSTOMER_DISTRICT,
            "Customer district",
            "AMBIGUOUS_ALIAS",
            "candidates",
            [
                STORE_DISTRICT,
                CUSTOMER_DISTRICT,
            ],
        )
    if producer == "calendar":
        query["time"]["calendar_id"] = "fiscal"
        return (
            query,
            FISCAL_DAY,
            "Fiscal calendar day",
            "INCOMPATIBLE_CALENDAR",
            ("alternative_temporal_roles"),
            [FISCAL_DAY],
        )
    if producer == "conversion_events":
        from semantic_rails.compiler import compile_query

        query["time"]["temporal_role"] = FIRST_ORDER
        query["select"] = [
            {
                "expression": {
                    "kind": "conversion",
                    "base": {"kind": "aggregate", "measure": CUSTOMERS},
                    "converted": {"kind": "aggregate", "measure": CUSTOMERS},
                    "entity": "entity.jaffle_customer",
                    "window": {"unit": "day", "value": 7},
                    "matching_mode": "first_converted_after_base",
                },
                "as": "conversion",
            }
        ]
        with pytest.raises(SemanticLayerError) as raised:
            compile_query(runtime._config, None, query)
        assert raised.value.code == "CONVERSION_NOT_SUPPORTED"
        alternatives = raised.value.details["candidate_measures"]
        assert ORDERS in alternatives
        return (
            query,
            ORDERS,
            "Order count",
            "CONVERSION_NOT_SUPPORTED",
            "candidate_measures",
            alternatives,
        )
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            runtime._config,
            measures=[
                replace(row, compatible_temporal_roles=[FIRST_ORDER, LATEST_ORDER])
                if row.id == CUSTOMERS
                else row
                for row in runtime._config.measures
            ],
        ),
    )
    if producer == "measure_clocks":
        query["select"].append({"expression": {"measure": CUSTOMERS}, "as": "customers"})
        code = "INCOMPATIBLE_TEMPORAL_ROLE"
    else:
        query["metric_filters"] = [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_customer",
                    "input": {"kind": "aggregate", "measure": CUSTOMERS},
                    "op": ">",
                    "value": 1,
                },
                "op": "=",
                "value": True,
            }
        ]
        code = "INVALID_TEMPORAL_BINDING"
    return (
        query,
        LATEST_ORDER,
        "Customer latest ordered at",
        code,
        "compatible",
        [
            FIRST_ORDER,
            LATEST_ORDER,
        ],
    )


def _producer_response(runtime, query, transport):
    if transport == "validate":
        return runtime.validate(query)
    if transport == "http":
        service = SemanticHTTPService(runtime)
        # Compile reaches the producer and HTTP's real exception boundary.
        with pytest.raises(SemanticLayerError) as raised:
            service.handle("POST", "/compile", query)
        payload, status = service.exception_payload(
            raised.value, stage="compile", context=RequestContext(audience="external")
        )
        assert status == 400
        return payload
    return SemanticLayerMCPAdapter(runtime).call_tool(
        "execute", {"query": query, "mode": "validate"}
    )


@pytest.mark.parametrize(
    "producer", ["alias", "calendar", "measure_clocks", "predicate_clocks", "conversion_events"]
)
@pytest.mark.parametrize("visibility", ["hidden", "visible", "unknown"])
@pytest.mark.parametrize("transport", ["validate", "http", "mcp"])
def test_real_error_producers_withhold_hidden_alternatives(
    runtime_factory, monkeypatch, producer, visibility, transport
):
    runtime = runtime_factory("jaffle_shop")
    try:
        query, hidden_id, label, code, field, alternatives = _producer_case(
            runtime, monkeypatch, producer
        )
        if visibility == "hidden":
            monkeypatch.setattr(
                runtime,
                "_config",
                replace(
                    runtime._config,
                    semantic_policies=[
                        *runtime._config.semantic_policies,
                        SemanticPolicyConfig(
                            id="policy.hide_alternative",
                            kind="object_visibility",
                            object_ids=[hidden_id],
                            audiences=["external"],
                            action="hidden",
                        ),
                    ],
                ),
            )
        elif visibility == "unknown":
            monkeypatch.setattr(policies, "hidden_object_ids", _unknown_visibility)
        payload = _producer_response(runtime, query, transport)
        assert payload["ok"] is False
        issue = payload["errors"][0]
        if visibility == "visible":
            assert issue["code"] == code
            assert issue["details"][field] == alternatives
        else:
            serialized = json.dumps(payload).lower()
            assert hidden_id.lower() not in serialized
            assert label.lower() not in serialized
            if producer == "alias":
                assert issue["code"] == "OBJECT_NOT_FOUND"
                assert "candidates" not in issue["details"]
                with monkeypatch.context() as absent_alias:
                    absent_alias.setattr(
                        runtime,
                        "_config",
                        replace(
                            runtime._config,
                            dimensions=[
                                replace(row, aliases=[]) for row in runtime._config.dimensions
                            ],
                        ),
                    )
                    absent_issue = _producer_response(runtime, query, transport)["errors"][0]
                assert issue == absent_issue
            else:
                assert issue["code"] == code
                assert issue["details"][field] == (
                    [item for item in alternatives if item != hidden_id]
                    if visibility == "hidden"
                    else []
                )
        if producer in {"measure_clocks", "predicate_clocks"}:
            # Clock alternatives are structured even for an unrestricted caller.
            assert hidden_id not in issue["message"]
            assert hidden_id not in issue.get("why_invalid", "")
            if producer == "predicate_clocks":
                hints = issue["details"]["recovery_hints"]
                assert all(hidden_id not in row["message"] for row in hints)
    finally:
        runtime.close()


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


@pytest.mark.parametrize("visibility", ["hidden", "visible", "unknown"])
def test_registry_alias_candidate_rows_use_the_same_filter(
    runtime_factory, monkeypatch, visibility
):
    from semantic_rails.diagnostics import enrich_diagnostic_candidates
    from semantic_rails.registry import Registry

    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        with pytest.raises(SemanticLayerError) as raised:
            Registry(runtime._config).resolve("district", kind="dimension")
        hidden = frozenset({CUSTOMER_DISTRICT}) if visibility == "hidden" else frozenset()
        if visibility == "unknown":
            hidden = None
        enriched = enrich_diagnostic_candidates(raised.value, runtime._config, hidden_ids=hidden)
        if visibility == "visible":
            assert enriched is raised.value
        else:
            assert enriched.code == "OBJECT_NOT_FOUND"
            _assert_no_hidden_dimension(exception_issue(enriched, stage="resolve"))
            assert "candidates" not in enriched.details
    finally:
        runtime.close()


@pytest.mark.parametrize("producer", ["filter", "registry"])
def test_registry_alias_candidates_keep_two_visible_matches(runtime_factory, monkeypatch, producer):
    from semantic_rails.diagnostics import enrich_diagnostic_candidates
    from semantic_rails.expressions import resolve_filter_dimension
    from semantic_rails.registry import Registry

    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        regional = replace(
            next(row for row in runtime._config.dimensions if row.id == STORE_DISTRICT),
            id="dimension.regional_district",
            label="Regional district",
        )
        config = replace(runtime._config, dimensions=[*runtime._config.dimensions, regional])
        with pytest.raises(SemanticLayerError) as raised:
            if producer == "filter":
                resolve_filter_dimension("district", config)
            else:
                Registry(config).resolve("district", kind="dimension")
        enriched = enrich_diagnostic_candidates(
            raised.value, config, hidden_ids=frozenset({CUSTOMER_DISTRICT})
        )
        assert enriched.code == "AMBIGUOUS_ALIAS"
        _assert_no_hidden_dimension(exception_issue(enriched, stage="resolve"))
        assert enriched.details["candidates"] == [
            row
            for row in raised.value.details["candidates"]
            if (row["id"] if isinstance(row, dict) else row) != CUSTOMER_DISTRICT
        ]
    finally:
        runtime.close()


def test_cli_context_failure_still_filters_candidates(runtime_factory, monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from semantic_rails.cli import app
    from semantic_rails.expressions import resolve_filter_dimension

    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        config = runtime._config

        def fail(args):
            resolve_filter_dimension("district", config)

        args = argparse.Namespace(cmd="validate", func=fail, query_json=None)
        monkeypatch.setattr(app, "build_parser", lambda: SimpleNamespace(parse_args=lambda: args))
        monkeypatch.setattr(app, "_config_for_error_enrichment", lambda args: config)
        monkeypatch.setattr(app, "_policy_context_from_args", _unknown_visibility)
        with pytest.raises(SystemExit) as raised:
            app.main()
        assert raised.value.code == 1
        captured = capsys.readouterr()
        _assert_no_hidden_dimension(captured.out + captured.err)
        issue = json.loads(captured.out)["error"]
        assert issue["code"] == "OBJECT_NOT_FOUND"
        assert "candidates" not in issue["details"]
        assert not issue["details"].get("closest_matches")
    finally:
        runtime.close()


def test_mcp_context_failure_still_returns_filtered_error(runtime_factory, monkeypatch):
    from semantic_rails import mcp
    from semantic_rails.expressions import resolve_filter_dimension

    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        with pytest.raises(SemanticLayerError) as raised:
            resolve_filter_dimension("district", runtime._config)
        monkeypatch.setattr(mcp, "_resolved_tool_request_context", _unknown_visibility)
        payload = SemanticLayerMCPAdapter(runtime)._error_response(
            raised.value, {"query": {"verbosity": "full"}}
        )
        assert payload["ok"] is False
        _assert_no_hidden_dimension(payload)
        assert payload["error"]["code"] == "OBJECT_NOT_FOUND"
        assert "candidates" not in payload["errors"][0]["details"]
        assert not payload["errors"][0]["details"].get("closest_matches")
    finally:
        runtime.close()


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
