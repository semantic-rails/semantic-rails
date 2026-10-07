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
        # A domain naming the hidden dimension is hidden with it, shared or not: the value
        # grounds nothing, and the draft that leaves it out is held.
        assert payload["best"]["query_ir"].get("where", []) == []
        assert payload["status"] != "ok"
        assert "execute" not in payload.get("next", {}).get("ready_for", [])
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


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_hidden_underscore_dimension_behaves_like_an_absent_dimension(
    runtime_factory, monkeypatch, path, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    hidden_id = "dimension.private_revenue_usd"
    dimension = replace(
        runtime._config.dimensions[0],
        id=hidden_id,
        name="private.revenue_usd",
        label="Revenue usd",
        aliases=[],
    )
    policy = SemanticPolicyConfig(
        id="policy.hide_revenue_dimension",
        kind="object_visibility",
        object_ids=[hidden_id],
        action="hidden",
    )
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            runtime._config,
            dimensions=[*runtime._config.dimensions, dimension],
            semantic_policies=[*runtime._config.semantic_policies, policy],
        ),
    )
    intent = "revenue_usd at store name level"
    partial = {
        "where": [
            {
                "field": "dimension.jaffle_store_name",
                "op": "IN",
                "value": ["Brooklyn", "Philadelphia"],
            }
        ]
    }
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        hidden = plan_payload(runtime, intent=intent, detail=detail, partial_query=partial)
        monkeypatch.setattr(
            runtime,
            "_config",
            replace(
                runtime._config,
                dimensions=[dim for dim in runtime._config.dimensions if dim.id != hidden_id],
            ),
        )
        runtime._catalog_search_index = None
        _force_fallback(runtime, monkeypatch, intent, path)
        absent = plan_payload(runtime, intent=intent, detail=detail, partial_query=partial)
        assert hidden == absent
        assert hidden_id not in json.dumps(hidden)
        assert "Revenue usd" not in json.dumps(hidden)
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_hidden_name_holding_a_filter_value_behaves_like_an_absent_dimension(
    runtime_factory, monkeypatch, path, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    hidden_id = "dimension.private_new_customers"
    dimension = replace(
        runtime._config.dimensions[0],
        id=hidden_id,
        name="private.new_customers",
        label="New customers",
        aliases=[],
    )
    policy = SemanticPolicyConfig(
        id="policy.hide_new_customers",
        kind="object_visibility",
        object_ids=[hidden_id],
        action="hidden",
    )
    base = runtime._config
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            base,
            dimensions=[*base.dimensions, dimension],
            semantic_policies=[*base.semantic_policies, policy],
        ),
    )
    # No level word: only the name obligation reads the question's names.
    intent = "revenue by store name for new customers"
    partial = {
        "group_by": ["dimension.jaffle_store_name"],
        "where": [
            {
                "field": "dimension.jaffle_store_name",
                "op": "IN",
                "value": ["Brooklyn", "Philadelphia"],
            }
        ],
    }
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        hidden = plan_payload(runtime, intent=intent, detail=detail, partial_query=partial)
        monkeypatch.setattr(runtime, "_config", base)
        runtime._catalog_search_index = None
        _force_fallback(runtime, monkeypatch, intent, path)
        absent = plan_payload(runtime, intent=intent, detail=detail, partial_query=partial)
        assert hidden == absent
        assert absent["status"] == "ok"
        assert hidden_id not in json.dumps(hidden)
        assert "New customers" not in json.dumps(hidden)
        assert "filter_inside_grouping" not in json.dumps(hidden)
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["dimensions", "entities"])
def test_hidden_underscore_objects_are_excluded_from_name_spans(runtime_factory, kind) -> None:
    runtime = runtime_factory("jaffle_shop")
    row = getattr(runtime._config, kind)[0]
    hidden = replace(row, name="private.revenue_usd", label="Revenue usd", aliases=[])
    policy = SemanticPolicyConfig(
        id="policy.hide_named_object",
        kind="object_visibility",
        object_ids=[row.id],
        action="hidden",
    )
    config = replace(
        runtime._config,
        **{kind: [hidden]},
        semantic_policies=[*runtime._config.semantic_policies, policy],
    )
    question = "revenue_usd at store name level"
    query = {"group_by": []}
    try:
        # The check reads names through its caller-scoped view, never the raw config.
        assert plan_module._named_groupings_unmet(config, question, query) == (
            plan_module._named_groupings_unmet(replace(config, **{kind: []}), question, query)
        )
        visible = replace(config, semantic_policies=runtime._config.semantic_policies)
        assert "revenue_usd" in plan_module._named_groupings_unmet(visible, question, query)[0]
    finally:
        runtime.close()


CALENDAR = "entity.jaffle_time"
CUSTOMER_TYPE = "dimension.jaffle_customer_type"
STORE_NAME = "dimension.jaffle_store_name"
NEW_MONTH = "new month and store name revenue"
NEW_MONTH_PARTIAL = {
    "group_by": [STORE_NAME],
    "where": [{"field": STORE_NAME, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
    "time": {
        "temporal_role": "temporal_role.jaffle_order_time",
        "grain": "month",
        "calendar_id": "default",
    },
}


def _new_month_alias(config, hidden: str, **changes):
    """Customer type with the alias "new month", revenue labelled with that alias's value word,
    and the entity or temporal role ``hidden`` hidden from every caller, with ``changes``."""

    policy = SemanticPolicyConfig(
        id="policy.hide_clock", kind="object_visibility", object_ids=[hidden], action="hidden"
    )
    return replace(
        config,
        dimensions=[
            replace(row, aliases=[*(row.aliases or []), "new month"])
            if row.id == CUSTOMER_TYPE
            else row
            for row in config.dimensions
        ],
        measures=[
            replace(row, label="Revenue (new and repeat types)")
            if row.id == "measure.jaffle.revenue_usd"
            else row
            for row in config.measures
        ],
        entities=[replace(row, **changes) if row.id == hidden else row for row in config.entities],
        temporal_roles=[
            replace(row, **changes) if row.id == hidden else row for row in config.temporal_roles
        ],
        semantic_policies=[*config.semantic_policies, policy],
    )


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("detail", ["query", "best", "full", "debug"])
def test_a_hidden_calendar_label_cannot_change_a_response(
    runtime_factory, monkeypatch, path, detail
) -> None:
    runtime = runtime_factory("jaffle_shop")
    base = runtime._config
    try:
        payloads = []
        # Labelled "New", a visible calendar would make "new month" the time block's clock.
        for label in ("Calendar", "New"):
            monkeypatch.setattr(runtime, "_config", _new_month_alias(base, CALENDAR, label=label))
            runtime._catalog_search_index = None
            _force_fallback(runtime, monkeypatch, NEW_MONTH, path)
            payloads.append(
                plan_payload(
                    runtime, intent=NEW_MONTH, detail=detail, partial_query=NEW_MONTH_PARTIAL
                )
            )
        assert payloads[0] == payloads[1]
        assert payloads[0]["status"] == "low_confidence"
        assert "execute" not in payloads[0].get("next", {}).get("ready_for", [])
    finally:
        runtime.close()


@pytest.mark.parametrize("question", [NEW_MONTH, "revenue at new month and store level"])
def test_hidden_entity_and_temporal_role_names_cannot_change_the_name_obligation(
    package_config_factory, question
) -> None:
    config, _ = package_config_factory("jaffle_shop")
    spellings = ["New", "new month", "Month", "Store", "Store name", "Customer type", "Revenue"]
    for row in [*config.entities, *config.temporal_roles]:
        base = plan_module._named_groupings_unmet(
            _new_month_alias(config, row.id), question, NEW_MONTH_PARTIAL
        )
        for changes in [*({"label": s} for s in spellings), *({"aliases": [s]} for s in spellings)]:
            changed = _new_month_alias(config, row.id, **changes)
            unmet = plan_module._named_groupings_unmet(changed, question, NEW_MONTH_PARTIAL)
            assert unmet == base, (row.id, changes)


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
    from semantic_rails import visible_view

    def unavailable(*args, **kwargs):
        raise RuntimeError("visibility unavailable")

    runtime = runtime_factory("jaffle_shop")
    _with_districts(runtime, monkeypatch)
    monkeypatch.setattr(visible_view, "hidden_object_ids", unavailable)
    intent = "item revenue by district"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        # Unknown visibility refuses the plan before any candidate is ranked or named.
        with pytest.raises(SemanticLayerError) as error:
            plan_payload(runtime, intent=intent, detail="debug")
        assert error.value.code == "POLICY_DENIED"
        assert error.value.details == {"reason": "visibility_unresolved"}
        payload = {
            "code": error.value.code,
            "message": str(error.value),
            "details": error.value.details,
        }
        _assert_no_hidden_dimension(payload)
        assert STORE_DISTRICT not in json.dumps(payload)
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
