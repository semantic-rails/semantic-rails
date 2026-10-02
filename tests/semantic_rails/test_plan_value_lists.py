"""Named-value folding preserves caller constraints and ranked groupings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.generators import _draft_for_choice, _normalize_value_filters
from semantic_rails.planner.plan import _merge_partial_query

STORE = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
INTENT = "item revenue for Brooklyn from Philadelphia by product type"
CHOICE = {
    "id": "measure.jaffle.item_revenue_usd",
    "kind": "measure",
    "label": "Item revenue (USD)",
}


def _force_fallback(runtime, monkeypatch, intent: str, path: str) -> None:
    if path == "fallback":
        import semantic_rails.planner.plan as module

        result = replace(compose(runtime, intent), draft=None, pattern="")
        monkeypatch.setattr(module, "compose", lambda *args, **kwargs: result)


def _keeping(query: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [
        row
        for row in query.get("where", [])
        if row.get("field") == field and str(row.get("op", "=")).upper() in {"=", "==", "IN"}
    ]


@pytest.mark.parametrize("path", ["primary", "fallback", "plan"])
def test_named_values_share_one_filter_and_grouping(runtime_factory, path: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        if path == "primary":
            draft = compose(runtime, INTENT).draft
            assert draft is not None
            query = draft.query
            assert STORE in draft.interpreted_intent["group_by"]
            assert STORE in {row["id"] for row in draft.resolved}
        elif path == "fallback":
            query = _draft_for_choice(runtime, intent=INTENT, partial_query={}, choice=CHOICE).query
        else:
            payload = plan_payload(runtime, intent=INTENT)
            assert payload["status"] == "ok", payload.get("why")
            query = payload["best"]["query_ir"]
        filters = _keeping(query, STORE)
        assert len(filters) == 1
        assert filters[0]["op"] == "in"
        assert set(filters[0]["value"]) == {"Brooklyn", "Philadelphia"}
        product_dimension = "dimension.jaffle_product_type" if path == "fallback" else PRODUCT_TYPE
        assert query["group_by"] == [product_dimension, STORE]
    finally:
        runtime.close()


def test_multi_value_draft_executes_each_value_per_group(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=INTENT)
        assert payload["status"] == "ok", payload.get("why")
        rows = runtime.query(payload["best"]["query_ir"])["rows"]
        actual = {(row[STORE], row[PRODUCT_TYPE]): row["item_revenue_usd"] for row in rows}
        assert len(actual) == len(rows)
        runtime.close()
        with duckdb.connect(runtime.db_path) as connection:
            expected = {
                (store, product): revenue
                for store, product, revenue in connection.execute(
                    "SELECT s.store_name, i.product_type, SUM(i.item_revenue_cents / 100.0) "
                    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                    "JOIN jaffle_store s ON o.store_id = s.store_id "
                    "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1, 2"
                ).fetchall()
            }
        assert actual == pytest.approx(expected)
        assert {store for store, _ in actual} == {"Brooklyn", "Philadelphia"}
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "status", "expected_where", "expected_groups"),
    [
        (
            "revenue for Brooklyn by month",
            "ok",
            [{"field": STORE, "op": "=", "value": "Brooklyn"}],
            [],
        ),
        (
            "revenue not Brooklyn by month",
            "low_confidence",
            [{"field": STORE, "op": "=", "value": "Brooklyn"}],
            [],
        ),
        (
            "item revenue for food from Brooklyn by store",
            "ok",
            [
                {"field": STORE, "op": "=", "value": "Brooklyn"},
                {"field": PRODUCT_TYPE, "op": "=", "value": "jaffle"},
            ],
            [STORE],
        ),
    ],
)
def test_single_values_negation_and_distinct_dimensions_keep_their_behavior(
    runtime_factory, intent, status, expected_where, expected_groups
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()
    assert payload["status"] == status
    query = payload["best"]["query_ir"]
    assert query["where"] == expected_where
    assert query.get("group_by", []) == expected_groups
    if status == "low_confidence":
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert "negation_reversed" in [gap["kind"] for gap in payload["why"]["details"]["gaps"]]


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        (STORE, "=", "Brooklyn"),
        (STORE, "==", "Brooklyn"),
        (STORE, "IN", ["Brooklyn", "Philadelphia"]),
        (f" {STORE} ", " in ", "Brooklyn"),
    ],
)
def test_caller_filter_cannot_be_appended_to_a_normalized_list(
    runtime_factory, monkeypatch, path, field, op, value
) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {"where": [{"field": field, "op": op, "value": value}]}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, INTENT, path)
    try:
        payload = plan_payload(runtime, intent=INTENT, partial_query=partial)
    finally:
        runtime.close()
    assert partial == before
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert len(_keeping(query, STORE)) == 1
    assert set(_keeping(query, STORE)[0]["value"]) == {"Brooklyn", "Philadelphia"}
    assert STORE in query["group_by"]


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("intent", [INTENT, "item revenue for Brooklyn by product type"])
@pytest.mark.parametrize("field", [STORE, f" {STORE} "])
@pytest.mark.parametrize("op,value", [("!=", "Brooklyn"), ("NOT IN", ["Brooklyn"])])
def test_caller_exclusion_refuses_a_requested_value(
    runtime_factory, monkeypatch, path, intent, field, op, value
) -> None:
    runtime = runtime_factory("jaffle_shop")
    excluded = {"field": field, "op": op, "value": value}
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query={"where": [excluded]})
    finally:
        runtime.close()
    query = payload["best"]["query_ir"]
    assert len(_keeping(query, STORE)) == 1
    assert {**excluded, "field": STORE} in query["where"]
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    expected_gap = "filter_values_unrealized" if intent == INTENT else "contradictory_filters"
    assert expected_gap in [gap["kind"] for gap in payload["why"]["details"]["gaps"]]
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_caller_equality_outside_named_values_lowers_confidence(
    runtime_factory, monkeypatch, path
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "item revenue for Philadelphia by product type"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(
            runtime,
            intent=intent,
            partial_query={"where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]},
        )
    finally:
        runtime.close()
    assert payload["status"] == "low_confidence", payload.get("why")
    query = payload["best"]["query_ir"]
    assert _keeping(query, STORE) == [
        {"field": STORE, "op": "=", "value": "Brooklyn"},
        {"field": STORE, "op": "=", "value": "Philadelphia"},
    ]
    assert STORE not in query["group_by"]
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("value", [None, ["Brooklyn"], {"kind": "column", "name": "store"}])
def test_invalid_caller_literal_cannot_bypass_inclusion_normalization(
    runtime_factory, monkeypatch, path, value
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _force_fallback(runtime, monkeypatch, INTENT, path)
    try:
        payload = plan_payload(
            runtime,
            intent=INTENT,
            partial_query={"where": [{"field": STORE, "op": "=", "value": value}]},
        )
    finally:
        runtime.close()
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("field", [STORE, f" {STORE} "])
def test_caller_only_intersection_keeps_filters_and_executed_rows(
    runtime_factory, monkeypatch, path, field
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "item revenue by product type"
    where = [
        {"field": field, "op": "IN", "value": ["Brooklyn", "Philadelphia"]},
        {"field": STORE, "op": "=", "value": "Brooklyn"},
    ]
    partial = {"where": where}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert query["where"] == [{**row, "field": STORE} for row in where]
        assert partial == before
        assert len(query["group_by"]) == 1 and STORE not in query["group_by"]
        rows = runtime.query(query)["rows"]
        product = query["group_by"][0]
        alias = query["select"][0]["as"]
        actual = {row[product]: row[alias] for row in rows}
        runtime.close()
        with duckdb.connect(runtime.db_path) as connection:
            expected = dict(
                connection.execute(
                    "SELECT i.product_type, SUM(i.item_revenue_cents / 100.0) "
                    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                    "JOIN jaffle_store s ON o.store_id = s.store_id "
                    "WHERE s.store_name = 'Brooklyn' GROUP BY 1"
                ).fetchall()
            )
        assert actual == pytest.approx(expected)
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("intent", "where", "gap"),
    [
        (
            "item revenue by product type",
            [
                {"field": STORE, "op": "=", "value": "Brooklyn"},
                {"field": STORE, "op": "=", "value": "Philadelphia"},
            ],
            "contradictory_filters",
        ),
        (
            "item revenue for Chicago by product type",
            [{"field": STORE, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}],
            "contradictory_filters",
        ),
    ],
)
def test_unrelated_caller_inclusions_are_preserved_and_refused(
    runtime_factory, monkeypatch, path, intent, where, gap
) -> None:
    runtime = runtime_factory("jaffle_shop")
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
    finally:
        runtime.close()
    query = payload["best"]["query_ir"]
    assert query["where"][: len(where)] == where
    assert STORE not in query["group_by"]
    assert payload["status"] == "low_confidence", payload.get("why")
    assert gap in [row["kind"] for row in payload["why"]["details"]["gaps"]]
    assert "execute" not in payload["next"].get("ready_for", [])


def test_ranked_named_values_execute_one_combined_top_three(
    runtime_factory,
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "top 3 product type by item revenue for Brooklyn from Philadelphia"
    try:
        payload = plan_payload(runtime, intent=intent)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert query["group_by"] == [PRODUCT_TYPE]
        assert query["limit"] == 3
        filters = _keeping(query, STORE)
        assert len(filters) == 1 and filters[0]["op"] == "in"
        assert set(filters[0]["value"]) == {"Brooklyn", "Philadelphia"}
        rows = runtime.query(query)["rows"]
        actual = [(row[PRODUCT_TYPE], row["item_revenue_usd"]) for row in rows]
        runtime.close()
        with duckdb.connect(runtime.db_path) as connection:
            expected = connection.execute(
                "SELECT i.product_type, SUM(i.item_revenue_cents / 100.0) "
                "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                "JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 3"
            ).fetchall()
        assert [product for product, _ in actual] == [product for product, _ in expected]
        assert [value for _, value in actual] == pytest.approx([value for _, value in expected])
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("path", "intent", "expected_values"),
    [
        pytest.param(
            "primary",
            "top 3 product types by item revenue for Brooklyn and Philadelphia",
            "Brooklyn",
            id="unresolved-plural-grouping",
        ),
        pytest.param(
            "fallback",
            "top 3 product types by item revenue for Brooklyn and Philadelphia",
            "Brooklyn",
            id="unresolved-compound-values",
        ),
        pytest.param(
            "fallback",
            "top 3 product type by item revenue for Brooklyn from Philadelphia",
            ["Brooklyn", "Philadelphia"],
            id="duplicate-ranked-grouping",
        ),
    ],
)
def test_ranked_value_lists_refuse_unresolved_intent_without_widening_filters(
    runtime_factory, monkeypatch, path, intent, expected_values
) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = None
    if path == "fallback":
        partial = {
            "select": [
                {
                    "as": "item_revenue_usd",
                    "expression": {"measure": CHOICE["id"], "aggregation": "sum"},
                }
            ],
            "group_by": [PRODUCT_TYPE],
            "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}],
            "limit": 3,
        }
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
    finally:
        runtime.close()
    query = payload["best"]["query_ir"]
    assert query["limit"] == 3
    assert STORE not in query["group_by"]
    assert len(query["where"]) == 1
    row = query["where"][0]
    assert row["field"] == STORE
    if isinstance(expected_values, list):
        assert row["op"] == "in"
        assert len(row["value"]) == len(expected_values)
        assert set(row["value"]) == set(expected_values)
    else:
        assert row == {"field": STORE, "op": "=", "value": expected_values}
    assert payload["status"] == "low_confidence", {
        "query": query,
        "ready_for": payload["next"].get("ready_for", []),
        "why": payload.get("why"),
    }
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("caller_has_list", [False, True])
def test_merge_folds_only_into_generated_lists_without_adding_grouping(caller_has_list) -> None:
    generated = {"field": STORE, "op": "in", "value": ["Brooklyn", "Philadelphia"]}
    equality = {"field": f" {STORE} ", "op": "=", "value": "Brooklyn"}
    caller_rows = [generated, equality] if caller_has_list else [equality]
    query = {"where": [generated], "group_by": [PRODUCT_TYPE]}
    merged = _merge_partial_query(None, query, {"where": caller_rows})
    assert merged["group_by"] == [PRODUCT_TYPE]
    expected = [generated, {**equality, "field": STORE}] if caller_has_list else [generated]
    assert merged["where"] == expected


def test_single_value_normalizes_field_and_operator_without_mutating_caller() -> None:
    query = {"where": [{"field": f" {STORE} ", "op": " == ", "value": "Brooklyn"}]}
    before = deepcopy(query)
    normalized = _normalize_value_filters(query, [{"dimension_id": STORE, "value": "Brooklyn"}])
    assert query == before
    assert normalized["where"] == [{"field": STORE, "op": "=", "value": "Brooklyn"}]
    assert "group_by" not in normalized
