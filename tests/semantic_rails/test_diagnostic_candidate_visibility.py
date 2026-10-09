"""Validation alternatives must not disclose policy-hidden catalog objects.

Every producer and enricher reads the caller's visible view, so an alternative the caller cannot
see is never a candidate: an error equals the one the package without the hidden objects gives.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from semantic_rails import visible_view
from semantic_rails.diagnostics import exception_issue
from semantic_rails.errors import SemanticLayerError
from semantic_rails.http_core import SemanticHTTPService
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.registry import Registry
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import _enrich_runtime_error
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.hidden_absent import absent, envelope
from tests.semantic_rails.test_plan_dimension_visibility import (
    _assert_no_hidden_dimension,
    _hide_customer_district,
    _with_districts,
)
from tests.semantic_rails.test_plan_value_lists import CUSTOMER_DISTRICT, STORE_DISTRICT

ORDER_TIME = "temporal_role.jaffle_order_time"
FIRST_ORDER = "temporal_role.jaffle_customer_first_order_at"
LATEST_ORDER = "temporal_role.jaffle_customer_latest_ordered_at"
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
        try:
            return service.handle("POST", "/compile", query)[0]
        except SemanticLayerError as exc:
            payload, status = service.exception_payload(
                exc, stage="compile", context=RequestContext(audience="external")
            )
        assert status == 400
        return payload
    return SemanticLayerMCPAdapter(runtime).call_tool(
        "execute", {"query": query, "mode": "validate"}
    )


@pytest.mark.parametrize(
    "producer", ["alias", "measure_clocks", "predicate_clocks", "conversion_events"]
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
            monkeypatch.setattr(visible_view, "hidden_object_ids", _unknown_visibility)
        payload = _producer_response(runtime, query, transport)
        serialized = json.dumps(payload).lower()
        if visibility == "visible":
            assert payload["ok"] is False
            issue = payload["errors"][0]
            assert issue["code"] == code
            assert issue["details"][field] == alternatives
        elif visibility == "unknown":
            assert payload["ok"] is False
            issue = payload["errors"][0]
            assert issue["code"] == "POLICY_DENIED"
            assert issue["details"]["reason"] == "visibility_unresolved"
            assert hidden_id.lower() not in serialized
            return
        else:
            assert hidden_id.lower() not in serialized
            assert label.lower() not in serialized
            hidden = visible_view.hidden_object_ids(runtime.package_config, audience="external")
            with monkeypatch.context() as missing:
                missing.setattr(runtime, "_config", absent(runtime.package_config, hidden))
                expected = _producer_response(runtime, query, transport)
            assert envelope(payload) == envelope(expected)
            return
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
        monkeypatch.setattr(visible_view, "hidden_object_ids", _unknown_visibility)
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
        _assert_no_hidden_dimension(payload) if visibility != "visible" else None
        if visibility == "unknown":
            assert (issue["code"], issue["details"]) == (
                "POLICY_DENIED",
                {"reason": "visibility_unresolved"},
            )
            return
        assert issue["code"] == ("OBJECT_NOT_FOUND" if failure == "typo" else "PATH_NOT_FOUND")
        key = "closest_matches" if failure == "typo" else "compatible_group_by_dimensions"
        if visibility == "visible":
            assert CUSTOMER_DISTRICT in issue["details"][key]
            assert any(CUSTOMER_DISTRICT in json.dumps(hint) for hint in issue["recovery_hints"])
    finally:
        runtime.close()


@pytest.mark.parametrize("visibility", ["hidden", "visible"])
def test_authored_metric_suggestions_respect_visibility(package_config_factory, visibility) -> None:
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
    details = {"expression_kind": "prior_period", "received": {"measure": measure_id}}
    error = SemanticLayerError("INVALID_EXPRESSION_AST", "Invalid expression", details=details)
    enriched = _enrich_runtime_error(error, visible_view.view_of(config, {}))
    if visibility == "visible":
        assert enriched.details["closest_matches"] == [metric_id]
    else:
        _assert_no_hidden_dimension(exception_issue(enriched, stage="validate"))


def _unknown_visibility(*args, **kwargs):
    raise ValueError("Visibility unavailable")


def _hide(config, object_id):
    policy = SemanticPolicyConfig(
        id="policy.hide_district",
        kind="object_visibility",
        object_ids=[object_id],
        audiences=["external"],
        action="hidden",
    )
    return replace(config, semantic_policies=[*config.semantic_policies, policy])


def test_an_alias_ambiguous_only_through_a_hidden_object_resolves(runtime_factory, monkeypatch):
    """The caller's view holds one district: "district" names it (the reference rows are in
    test_plan_ambiguous_grouping); everyone else still has two."""
    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        config = _hide(runtime._config, CUSTOMER_DISTRICT)
        with pytest.raises(SemanticLayerError) as raised:
            Registry(visible_view.view_of(config, {})).resolve("district", kind="dimension")
        assert raised.value.code == "AMBIGUOUS_ALIAS"
        view = visible_view.view_of(config, {"audience": "external"})
        assert (
            Registry(view).resolve("district", kind="dimension")["object"]["id"] == STORE_DISTRICT
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("producer", ["filter", "registry"])
def test_registry_alias_candidates_keep_two_visible_matches(runtime_factory, monkeypatch, producer):
    from semantic_rails.expressions import resolve_filter_dimension

    runtime = runtime_factory("jaffle_shop")
    try:
        _producer_case(runtime, monkeypatch, "alias")
        regional = replace(
            next(row for row in runtime._config.dimensions if row.id == STORE_DISTRICT),
            id="dimension.regional_district",
            label="Regional district",
        )
        config = replace(runtime._config, dimensions=[*runtime._config.dimensions, regional])
        view = visible_view.view_of(_hide(config, CUSTOMER_DISTRICT), {"audience": "external"})
        with pytest.raises(SemanticLayerError) as raised:
            if producer == "filter":
                resolve_filter_dimension("district", view)
            else:
                Registry(view).resolve("district", kind="dimension")
        assert raised.value.code == "AMBIGUOUS_ALIAS"
        _assert_no_hidden_dimension(exception_issue(raised.value, stage="resolve"))
        candidates = [
            row["id"] if isinstance(row, dict) else row
            for row in raised.value.details["candidates"]
        ]
        assert sorted(candidates) == sorted([STORE_DISTRICT, regional.id])
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
