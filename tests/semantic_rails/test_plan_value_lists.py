"""Named values keep one predicate per dimension and a labelled row per value."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import duckdb
import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.generators import _draft_for_choice

STORE = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
INTENT = "item revenue for Brooklyn from Philadelphia by product type"
CHOICE = {
    "id": "measure.jaffle.item_revenue_usd",
    "kind": "measure",
    "label": "Item revenue (USD)",
}


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
    if path == "fallback":
        # Force the alternate generator, then the shared caller merge.
        import semantic_rails.planner.plan as module

        result = replace(compose(runtime, INTENT), draft=None, pattern="")
        monkeypatch.setattr(module, "compose", lambda *args, **kwargs: result)
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


@pytest.mark.parametrize("op,value", [("!=", "Brooklyn"), ("NOT IN", ["Brooklyn"])])
def test_caller_exclusion_refuses_a_requested_value(runtime_factory, op, value) -> None:
    runtime = runtime_factory("jaffle_shop")
    excluded = {"field": STORE, "op": op, "value": value}
    try:
        payload = plan_payload(runtime, intent=INTENT, partial_query={"where": [excluded]})
    finally:
        runtime.close()
    query = payload["best"]["query_ir"]
    assert len(_keeping(query, STORE)) == 1
    assert excluded in query["where"]
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "filter_values_unrealized" in [gap["kind"] for gap in payload["why"]["details"]["gaps"]]
    assert "execute" not in payload["next"].get("ready_for", [])


def test_caller_equality_and_a_second_named_value_become_one_list(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(
            runtime,
            intent="item revenue for Philadelphia by product type",
            partial_query={"where": [{"field": STORE, "op": "=", "value": "Brooklyn"}]},
        )
    finally:
        runtime.close()
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query_ir"]
    assert _keeping(query, STORE) == [
        {"field": STORE, "op": "in", "value": ["Brooklyn", "Philadelphia"]}
    ]
    assert query["group_by"] == [PRODUCT_TYPE, STORE]


@pytest.mark.parametrize("value", [None, ["Brooklyn"], {"kind": "column", "name": "store"}])
def test_invalid_caller_literal_cannot_bypass_inclusion_normalization(
    runtime_factory, value
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        with pytest.raises(SemanticLayerError) as exc:
            plan_payload(
                runtime,
                intent=INTENT,
                partial_query={"where": [{"field": STORE, "op": "=", "value": value}]},
            )
    finally:
        runtime.close()
    assert exc.value.code == "INVALID_QUERY"
    assert exc.value.details == {"path": "where", "field": STORE}
