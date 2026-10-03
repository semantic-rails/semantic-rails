"""Dimension visibility precedes planning, ranking and diagnostic disclosure."""

from __future__ import annotations

import json
from dataclasses import replace

import duckdb
import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult
from semantic_rails.schema import SemanticPolicyConfig, ValueDomainConfig, ValueDomainValue
from tests.semantic_rails.test_plan_value_lists import (
    CUSTOMER_DISTRICT,
    STORE_DISTRICT,
)
from tests.semantic_rails.test_plan_value_lists import (
    _with_districts as _with_metadata_districts,
)


def _with_districts(runtime, monkeypatch) -> None:
    _with_metadata_districts(runtime, monkeypatch)
    # Keep the fixture executable on the shared read-only seed.
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            runtime._config,
            dimensions=[
                replace(dim, column="store_name") if dim.id == STORE_DISTRICT else dim
                for dim in runtime._config.dimensions
            ],
        ),
    )


def _hide_customer_district(runtime, monkeypatch, **context) -> None:
    policy = SemanticPolicyConfig(
        id="policy.hide_customer_district",
        kind="object_visibility",
        object_ids=[CUSTOMER_DISTRICT],
        action="hidden",
        **context,
    )
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(runtime._config, semantic_policies=[*runtime._config.semantic_policies, policy]),
    )


def _assert_no_hidden_dimension(payload) -> None:
    # Check nested structured fields as well as both transport/text representations.
    if isinstance(payload, dict):
        for key, value in payload.items():
            _assert_no_hidden_dimension(key)
            _assert_no_hidden_dimension(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _assert_no_hidden_dimension(value)
    elif isinstance(payload, str):
        assert "customer_district" not in payload.lower()
        assert "customer district" not in payload.lower()
    for text in (json.dumps(payload, default=str), str(payload)):
        assert "customer_district" not in text.lower()
        assert "customer district" not in text.lower()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
@pytest.mark.parametrize("scoped", [False, True])
def test_hidden_dimension_is_not_selected_or_disclosed(
    runtime_factory, monkeypatch, path, detail, scoped
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    _hide_customer_district(runtime, monkeypatch, **({"audiences": ["external"]} if scoped else {}))
    intent = "item revenue by district"
    _force_fallback(runtime, monkeypatch, intent, path)
    partial = {"policy_context": {"audience": "external"}} if scoped else None
    try:
        payload = plan_payload(runtime, intent=intent, detail=detail, partial_query=partial)
        _assert_no_hidden_dimension(payload)
        query = payload["best"]["query_ir"]
        assert query["group_by"] == [STORE_DISTRICT]
        if detail in {"full", "debug"}:
            assert payload["intent_ir"]["grouping"] == [
                {"id": STORE_DISTRICT, "object_type": "dimension", "label": "Store district"}
            ]
        actual_rows = runtime.query({**query, **(partial or {})})["rows"]
        alias = query["select"][0]["as"]
        actual = {row[STORE_DISTRICT]: row[alias] for row in actual_rows}
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = dict(
                connection.execute(
                    "SELECT s.store_name, SUM(i.item_revenue_cents / 100.0) "
                    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                    "JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1"
                ).fetchall()
            )
        assert len(actual) == len(actual_rows) == len(expected)
        assert actual == pytest.approx(expected)
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_visible_dimension_keeps_existing_selection(runtime_factory, monkeypatch, path) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    intent = "item revenue by district"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, detail="full")
        assert payload["best"]["query_ir"]["group_by"] == [CUSTOMER_DISTRICT]
        assert payload["intent_ir"]["grouping"][0]["id"] == CUSTOMER_DISTRICT
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_visibility_is_scoped_to_each_request(runtime_factory, monkeypatch, path) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    _hide_customer_district(runtime, monkeypatch, audiences=["external"])
    intent = "item revenue by district"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payloads = [
            plan_payload(
                runtime,
                intent=intent,
                detail="full",
                partial_query={"policy_context": {"audience": audience}},
            )
            for audience in ("internal", "external", "internal")
        ]
        assert payloads[0] == payloads[2]
        assert payloads[0]["best"]["query_ir"]["group_by"] == [CUSTOMER_DISTRICT]
        assert payloads[1]["best"]["query_ir"]["group_by"] == [STORE_DISTRICT]
        _assert_no_hidden_dimension(payloads[1])
    finally:
        runtime.close()


@pytest.mark.parametrize("shared", [False, True])
def test_value_domains_do_not_reintroduce_hidden_dimensions(
    runtime_factory, monkeypatch, shared
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    _hide_customer_district(runtime, monkeypatch)
    domain = ValueDomainConfig(
        id="value_domain.district",
        dimensions=[CUSTOMER_DISTRICT, *([STORE_DISTRICT] if shared else [])],
        values=[ValueDomainValue(value="Brooklyn", label="Brooklyn")],
    )
    monkeypatch.setattr(runtime, "_config", replace(runtime._config, value_domains=[domain]))
    try:
        payload = plan_payload(runtime, intent="item revenue for Brooklyn", detail="debug")
        _assert_no_hidden_dimension(payload)
        where = payload["best"]["query_ir"].get("where", [])
        assert where == (
            [{"field": STORE_DISTRICT, "op": "=", "value": "Brooklyn"}] if shared else []
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_explicit_hidden_name_behaves_like_an_absent_dimension(
    runtime_factory, monkeypatch, path, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    _hide_customer_district(runtime, monkeypatch)
    intent = "item revenue by customer district"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        hidden = plan_payload(runtime, intent=intent, detail=detail)
        monkeypatch.setattr(
            runtime,
            "_config",
            replace(
                runtime._config,
                dimensions=[
                    dim for dim in runtime._config.dimensions if dim.id != CUSTOMER_DISTRICT
                ],
            ),
        )
        runtime._catalog_search_index = None
        _force_fallback(runtime, monkeypatch, intent, path)
        absent = plan_payload(runtime, intent=intent, detail=detail)
        assert hidden == absent
        assert hidden["status"] != "ok"
        assert "execute" not in hidden.get("next", {}).get("ready_for", [])
        # The intent echo is caller text; it cannot confirm a catalog object exists.
        assert CUSTOMER_DISTRICT not in json.dumps(hidden)
        assert "Customer district" not in json.dumps(hidden)
    finally:
        runtime.close()


@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_hidden_name_cannot_ground_an_intent_or_enter_catalog_hints(
    runtime_factory, monkeypatch, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    _hide_customer_district(runtime, monkeypatch)
    dimensions = [
        replace(dim, name="aardvarksecret", label="Aardvarksecret", aliases=[])
        if dim.id == CUSTOMER_DISTRICT
        else dim
        for dim in runtime._config.dimensions
    ]
    monkeypatch.setattr(runtime, "_config", replace(runtime._config, dimensions=dimensions))
    try:
        payloads = []
        for intent in ("item revenueasdf by aardvarksecret", "total moon dust by unknown"):
            hidden = plan_payload(runtime, intent=intent, detail=detail)
            monkeypatch.setattr(
                runtime,
                "_config",
                replace(
                    runtime._config,
                    dimensions=[dim for dim in dimensions if dim.id != CUSTOMER_DISTRICT],
                ),
            )
            runtime._catalog_search_index = None
            absent = plan_payload(runtime, intent=intent, detail=detail)
            assert hidden == absent
            payloads.append(hidden)
            monkeypatch.setattr(runtime, "_config", replace(runtime._config, dimensions=dimensions))
            runtime._catalog_search_index = None
        # The unrelated question's catalog hints cannot introduce the hidden name.
        assert "aardvarksecret" not in json.dumps(payloads[1]).lower()
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_uncertain_visibility_does_not_disclose_dimensions(
    runtime_factory, monkeypatch, path
) -> None:
    from semantic_rails import policies

    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    monkeypatch.setattr(policies, "diagnostic_hidden_object_ids", lambda *args: None)
    intent = "item revenue by district"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        with pytest.raises(SemanticLayerError) as error:
            plan_payload(runtime, intent=intent, detail="debug")
        assert error.value.code == "OBJECT_NOT_FOUND"
        payload = {
            "code": error.value.code,
            "message": str(error.value),
            "details": error.value.details,
        }
        _assert_no_hidden_dimension(payload)
        assert STORE_DISTRICT not in json.dumps(payload)
    finally:
        runtime.close()


@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_bypassing_candidate_filter_refuses_without_disclosure(
    runtime_factory, monkeypatch, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    composed = plan_module.compose(runtime, "item revenue by district")
    assert composed.draft.query["group_by"] == [CUSTOMER_DISTRICT]
    _hide_customer_district(runtime, monkeypatch)
    monkeypatch.setattr(plan_module, "compose", lambda *args: composed)
    try:
        with pytest.raises(SemanticLayerError) as error:
            plan_payload(runtime, intent="item revenue by district", detail=detail)
        assert error.value.code == "OBJECT_NOT_FOUND"
        _assert_no_hidden_dimension(
            {"code": error.value.code, "message": str(error.value), "details": error.value.details}
        )
    finally:
        runtime.close()


def _force_fallback(runtime, monkeypatch, intent, path) -> None:
    if path == "fallback":
        monkeypatch.setattr(
            plan_module,
            "compose",
            lambda runtime, intent: CompositionResult(
                intent_ir=parse_intent(runtime, intent), draft=None
            ),
        )
