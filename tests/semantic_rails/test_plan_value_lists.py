"""Named-value folding preserves caller constraints and ranked groupings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import duckdb
import pytest

from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.generators import (
    _draft_for_choice,
    _matched_value_rows,
    _normalize_value_filters,
)
from semantic_rails.planner.plan import _merge_partial_query

STORE = "dimension.jaffle_store_name"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
CATALOG_PRODUCT_TYPE = "dimension.jaffle_product_type"
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


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("equality", ["is", "="])
def test_separate_equality_clauses_keep_contradiction_and_refuse_execution(
    runtime_factory, monkeypatch, path, equality
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = f"item revenue where store {equality} Brooklyn and store {equality} Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent)
    finally:
        runtime.close()
    assert _keeping(payload["best"]["query_ir"], STORE) == [
        {"field": STORE, "op": "=", "value": "Brooklyn"},
        {"field": STORE, "op": "=", "value": "Philadelphia"},
    ]
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    assert "contradictory_filters" in [gap["kind"] for gap in payload["why"]["details"]["gaps"]]
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_separate_equality_clauses_match_empty_duckdb_reference(
    runtime_factory, monkeypatch, path
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "item revenue where store is Brooklyn and store is Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query={"group_by": [PRODUCT_TYPE]})
        # Execute the diagnostic IR directly to compare its conjunction with SQL;
        # the public plan must refuse execute readiness for this contradiction.
        assert "execute" not in payload["next"].get("ready_for", [])
        actual = runtime.query(payload["best"]["query_ir"])["rows"]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = connection.execute(
                "SELECT i.product_type, SUM(i.item_revenue_cents / 100.0) "
                "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                "JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name = 'Brooklyn' AND s.store_name = 'Philadelphia' GROUP BY 1"
            ).fetchall()
        assert actual == expected == []
    finally:
        runtime.close()


def test_values_without_phrase_provenance_keep_separate_equalities() -> None:
    matched = [
        {"dimension_id": STORE, "value": "Brooklyn"},
        {"dimension_id": STORE, "value": "Philadelphia"},
    ]
    assert _normalize_value_filters({}, matched)["where"] == [
        {"field": STORE, "op": "=", "value": "Brooklyn"},
        {"field": STORE, "op": "=", "value": "Philadelphia"},
    ]


@pytest.mark.parametrize("path", ["primary", "fallback", "plan"])
def test_named_values_share_one_filter_without_adding_grouping(runtime_factory, path: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        if path == "primary":
            draft = compose(runtime, INTENT).draft
            assert draft is not None
            query = draft.query
            assert STORE not in draft.interpreted_intent["group_by"]
            assert STORE not in {row["id"] for row in draft.resolved}
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
        assert query["group_by"] == [product_dimension]
    finally:
        runtime.close()


def test_multi_value_draft_executes_combined_values_per_group(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent=INTENT)
        assert payload["status"] == "ok", payload.get("why")
        rows = runtime.query(payload["best"]["query_ir"])["rows"]
        actual = {row[PRODUCT_TYPE]: row["item_revenue_usd"] for row in rows}
        assert len(actual) == len(rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = {
                product: revenue
                for product, revenue in connection.execute(
                    "SELECT i.product_type, SUM(i.item_revenue_cents / 100.0) "
                    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                    "JOIN jaffle_store s ON o.store_id = s.store_id "
                    "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1"
                ).fetchall()
            }
        assert actual == pytest.approx(expected)
        assert actual == pytest.approx({"beverage": 381387, "jaffle": 327015})
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
def test_caller_filter_stays_as_written_beside_named_values(
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
    expected_status = "ok" if isinstance(value, list) else "low_confidence"
    assert payload["status"] == expected_status, payload.get("why")
    query = payload["best"]["query_ir"]
    assert len(query["where"]) == 2
    assert query["where"][0] == {"field": STORE, "op": op, "value": value}
    assert query["where"][1]["field"] == STORE
    assert query["where"][1]["op"] == "in"
    assert set(query["where"][1]["value"]) == {"Brooklyn", "Philadelphia"}
    assert STORE not in query["group_by"]


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
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
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


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("grouping", ["product type", "product types"])
@pytest.mark.parametrize("separator", ["from", "and"])
def test_ranked_named_values_keep_exact_groupings_and_combined_totals(
    runtime_factory, monkeypatch, path, grouping, separator
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = f"top 3 {grouping} by item revenue for Brooklyn {separator} Philadelphia"
    partial = (
        {
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
        if path == "fallback"
        else None
    )
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
        assert partial == before
        if path == "fallback":
            assert payload["status"] == "low_confidence", payload.get("why")
            assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
            assert payload["why"]["details"]["gaps"][0]["kind"] == "ambiguous_grouping"
            assert payload["why"]["details"]["gaps"][0]["clause"] == grouping
            assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"
            assert "execute" not in payload["next"].get("ready_for", [])
        else:
            assert payload["status"] == "ok", payload.get("why")
            assert "execute" in payload["next"].get("ready_for", [])
        query = payload["best"]["query_ir"]
        assert {
            **query,
            "where": [{**row, "value": sorted(row["value"])} for row in query["where"]],
        } == {
            "version": 1 if path == "fallback" else 2,
            "select": [
                {
                    "as": "item_revenue_usd",
                    "expression": {"measure": CHOICE["id"], "aggregation": "sum"},
                }
            ],
            "group_by": (
                [PRODUCT_TYPE, "dimension.jaffle_product_type"]
                if path == "fallback"
                else [PRODUCT_TYPE]
            ),
            "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}],
            "limit": 3,
            "where": [{"field": STORE, "op": "in", "value": ["Brooklyn", "Philadelphia"]}],
        }
        # Execute the refused fallback's diagnostic IR only for the SQL comparison.
        rows = runtime.query(query)["rows"]
        actual = [(row[PRODUCT_TYPE], row["item_revenue_usd"]) for row in rows]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
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


@pytest.mark.parametrize("caller_has_list", [False, True])
def test_merge_keeps_caller_rows_first_without_adding_grouping(caller_has_list) -> None:
    generated = {"field": STORE, "op": "in", "value": ["Brooklyn", "Philadelphia"]}
    equality = {"field": f" {STORE} ", "op": "=", "value": "Brooklyn"}
    caller_rows = [generated, equality] if caller_has_list else [equality]
    query = {"where": [generated], "group_by": [PRODUCT_TYPE]}
    merged = _merge_partial_query(None, query, {"where": caller_rows})
    assert merged["group_by"] == [PRODUCT_TYPE]
    expected = (
        [generated, {**equality, "field": STORE}]
        if caller_has_list
        else [{**equality, "field": STORE}, generated]
    )
    assert merged["where"] == expected


def test_single_value_strips_field_without_changing_caller_operator() -> None:
    query = {"where": [{"field": f" {STORE} ", "op": " == ", "value": "Brooklyn"}]}
    before = deepcopy(query)
    normalized = _normalize_value_filters(query, [{"dimension_id": STORE, "value": "Brooklyn"}])
    assert query == before
    assert normalized["where"] == [
        {"field": STORE, "op": " == ", "value": "Brooklyn"},
        {"field": STORE, "op": "=", "value": "Brooklyn"},
    ]
    assert "group_by" not in normalized


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_caller_membership_order_and_case_preserve_equality_and_readiness(
    runtime_factory, monkeypatch, path
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "top 3 product type by item revenue for Brooklyn from Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    readiness = set()
    try:
        for op in ("IN", "in"):
            for values in (["Philadelphia", "Brooklyn"], ["Brooklyn", "Philadelphia"]):
                where = [
                    {"field": STORE, "op": op, "value": values},
                    {"field": STORE, "op": "=", "value": "Brooklyn"},
                ]
                before = deepcopy(where)
                payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
                assert where == before
                assert payload["best"]["query_ir"]["where"][:2] == where
                readiness.add((payload["status"], tuple(payload["next"].get("ready_for", []))))
        assert len(readiness) == 1
    finally:
        runtime.close()


def test_supplied_ranking_does_not_add_named_value_grouping(runtime_factory, monkeypatch) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {
        "group_by": [PRODUCT_TYPE],
        "order_by": [{"field": "item_revenue_usd", "direction": "DESC"}],
        "limit": 3,
    }
    try:
        payload = plan_payload(runtime, intent=INTENT, partial_query=partial)
        _force_fallback(runtime, monkeypatch, INTENT, "fallback")
        fallback = plan_payload(runtime, intent=INTENT, partial_query=partial)
        assert STORE not in fallback["best"]["query_ir"]["group_by"]
        query = payload["best"]["query_ir"]
        assert STORE not in query["group_by"]
        filters = _keeping(query, STORE)
        assert len(filters) == 1 and filters[0]["op"] == "in"
        assert set(filters[0]["value"]) == {"Brooklyn", "Philadelphia"}
        assert query["group_by"] == [PRODUCT_TYPE]
        assert payload["status"] == "ok", payload.get("why")
        rows = runtime.query(query)["rows"]
        actual = [(row[PRODUCT_TYPE], row["item_revenue_usd"]) for row in rows]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = connection.execute(
                "SELECT i.product_type, SUM(i.item_revenue_cents / 100.0) "
                "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                "JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 3"
            ).fetchall()
        assert [product for product, _ in actual] == [product for product, _ in expected]
        assert [value for _, value in actual] == pytest.approx([value for _, value in expected])
        assert dict(actual) == pytest.approx({"beverage": 381387, "jaffle": 327015})
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("path", "intent"),
    [
        ("mixed-case-draft", "item revenue for Brooklyn and Philadelphia"),
        ("primary", "item revenue for brooklyn and philadelphia"),
        ("fallback", "item revenue for brooklyn and philadelphia"),
        ("primary", "item revenue for Brooklyn from Philadelphia"),
        ("fallback", "item revenue for Brooklyn from Philadelphia"),
        ("primary", "item revenue for Brooklyn, Philadelphia"),
        ("fallback", "item revenue for Brooklyn, Philadelphia"),
    ],
)
@pytest.mark.parametrize("caller_filter", [False, True])
def test_unranked_resolved_named_values_execute_a_total_with_caller_filters_preserved(
    runtime_factory, monkeypatch, path, intent, caller_filter
) -> None:
    runtime = runtime_factory("jaffle_shop")
    where = [{"field": STORE, "op": "=", "value": "Brooklyn"}] if caller_filter else []
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        if path == "mixed-case-draft":
            # Direct inference retains case; plan lowercases the shared phrase.
            query = _draft_for_choice(
                runtime, intent=intent, partial_query={"where": where}, choice=CHOICE
            ).query
        else:
            payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
            query = payload["best"]["query_ir"]
            assert payload["status"] == ("low_confidence" if caller_filter else "ok"), payload.get(
                "why"
            )
            if caller_filter:
                assert "execute" not in payload["next"].get("ready_for", [])
                assert "filter_values_unrealized" in [
                    gap["kind"] for gap in payload["why"]["details"]["gaps"]
                ]
            else:
                assert "execute" in payload["next"].get("ready_for", [])
        assert not query.get("group_by")
        assert query["where"][: len(where)] == where
        filters = _keeping(query, STORE)
        assert len(filters) == 1 + len(where)
        assert filters[-1]["op"] == "in"
        assert set(filters[-1]["value"]) == {"Brooklyn", "Philadelphia"}
        rows = runtime.query(query)["rows"]
        assert len(rows) == 1
        actual = rows[0][query["select"][0]["as"]]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = connection.execute(
                "SELECT SUM(i.item_revenue_cents / 100.0) "
                "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                "JOIN jaffle_store s ON o.store_id = s.store_id "
                "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
                + ("AND s.store_name = 'Brooklyn'" if caller_filter else "")
            ).fetchone()[0]
        assert actual == pytest.approx(expected)
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("where", [None, "", {}])
def test_empty_caller_where_does_not_raise(runtime_factory, monkeypatch, path, where) -> None:
    runtime = runtime_factory("jaffle_shop")
    _force_fallback(runtime, monkeypatch, INTENT, path)
    try:
        payload = plan_payload(runtime, intent=INTENT, partial_query={"where": where})
    finally:
        runtime.close()
    assert payload["status"] == "ok", payload.get("why")
    product_dimension = "dimension.jaffle_product_type" if path == "fallback" else PRODUCT_TYPE
    assert payload["best"]["query_ir"]["group_by"] == [product_dimension]


@pytest.mark.parametrize(
    "values", [["Brooklyn", "Philadelphia"], ["Brooklyn", "Philadelphia", "San Francisco"]]
)
def test_compound_discovery_keeps_each_named_value_and_its_span(runtime_factory, values) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = ("item revenue for " + ", ".join(values[:-1]) + " and " + values[-1]).lower()
    try:
        query = {"select": [{"expression": {"measure": CHOICE["id"]}}]}
        matched = _matched_value_rows(runtime, query, intent)
        assert {row["value"] for row in matched} == set(values)
        assert all(row["dimension_id"] == STORE for row in matched)
        for row in matched:
            start, end = row["matched_span"]
            assert intent[start:end] == row["value"].lower()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("intent", "existing", "requested", "reference_sql"),
    [
        (
            "orders by order id",
            "dimension.jaffle_order_customer_id",
            "dimension.jaffle_order_id",
            "SELECT customer_id, order_id, COUNT(DISTINCT order_id) "
            "FROM jaffle_order GROUP BY customer_id, order_id",
        ),
        (
            "item revenue by item id",
            "dimension.jaffle_item_order_id",
            "dimension.jaffle_item_id",
            "SELECT order_id, item_id, SUM(item_revenue_cents / 100.0) "
            "FROM jaffle_item GROUP BY order_id, item_id",
        ),
    ],
)
def test_fallback_resolves_distinct_groupings_before_deduplicating(
    runtime_factory, monkeypatch, intent, existing, requested, reference_sql
) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {"group_by": [existing]}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, "fallback")
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
        query = payload["best"]["query_ir"]
        assert partial == before
        assert query["group_by"] == [existing, requested]
        assert payload["status"] == "low_confidence", payload.get("why")
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        gap = payload["why"]["details"]["gaps"][0]
        assert gap["kind"] == "ambiguous_grouping"
        assert gap["actual"]["dimension_ids"] == sorted([existing, requested])
        assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"
        assert "execute" not in payload["next"].get("ready_for", [])
        # Execute only the diagnostic IR to retain the independent SQL comparison.
        rows = runtime.query(query)["rows"]
        measure = query["select"][0]["as"]
        actual = sorted((row[existing], row[requested], row[measure]) for row in rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = sorted(connection.execute(reference_sql).fetchall())
        assert [row[:2] for row in actual] == [row[:2] for row in expected]
        assert [row[2] for row in actual] == pytest.approx([row[2] for row in expected])
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_plan_keeps_both_product_groupings_or_refuses_readiness(
    runtime_factory, monkeypatch, path
) -> None:
    runtime = runtime_factory("jaffle_shop")
    intent = "item revenue by product type and product"
    _force_fallback(runtime, monkeypatch, intent, path)
    try:
        payload = plan_payload(runtime, intent=intent)
        if payload["status"] == "ok":
            groups = payload["best"]["query_ir"]["group_by"]
            assert len(groups) == 2
            assert groups[0] in {PRODUCT_TYPE, "dimension.jaffle_product_type"}
            assert groups[1] in {
                "dimension.jaffle_product_id",
                "dimension.jaffle_product_name",
                "dimension.jaffle_item_product_name",
            }
        else:
            assert payload["status"] == "low_confidence", payload.get("why")
            assert "execute" not in payload["next"].get("ready_for", [])
    finally:
        runtime.close()


@pytest.mark.parametrize("grouping", ["product type", "product types"])
@pytest.mark.parametrize(
    "authored_groups", [[PRODUCT_TYPE], [PRODUCT_TYPE, "dimension.jaffle_product_type"]]
)
def test_fallback_preserves_authored_groups_and_deduplicates_resolved_ids(
    runtime_factory, grouping, authored_groups
) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {"group_by": authored_groups}
    before = deepcopy(partial)
    try:
        query = _draft_for_choice(
            runtime,
            intent=f"item revenue by {grouping} and store name",
            partial_query=partial,
            choice=CHOICE,
        ).query
        assert query["group_by"] == list(
            dict.fromkeys([*authored_groups, "dimension.jaffle_product_type", STORE])
        )
        assert partial == before
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("Item product type", "jaffle.Item.product_type"),
        ("ItemProductType", "jaffle.Item.productType"),
        ("Item product types", "jaffle.Item.product_types"),
    ],
)
def test_ambiguous_added_grouping_cannot_bypass_readiness_guard(
    runtime_factory, monkeypatch, path, label, name
) -> None:
    import semantic_rails.planner.plan as module

    runtime = runtime_factory("jaffle_shop")
    intent = "item revenue by product type"
    partial = {"group_by": [PRODUCT_TYPE]}
    before = deepcopy(partial)
    monkeypatch.setattr(
        runtime,
        "_config",
        replace(
            runtime._config,
            dimensions=[
                replace(dim, label=label, name=name, aliases=[]) if dim.id == PRODUCT_TYPE else dim
                for dim in runtime._config.dimensions
            ],
        ),
    )
    result = compose(runtime, intent)
    if path == "primary":
        draft = replace(
            result.draft,
            query={
                **result.draft.query,
                "group_by": [PRODUCT_TYPE, "dimension.jaffle_product_type"],
            },
        )
        result = replace(result, draft=draft)
    else:
        result = replace(result, draft=None, pattern="")
    monkeypatch.setattr(module, "compose", lambda *args, **kwargs: result)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
        assert partial == before
        assert payload["best"]["query_ir"]["group_by"] == [
            PRODUCT_TYPE,
            "dimension.jaffle_product_type",
        ]
        assert payload["status"] == "low_confidence", payload.get("why")
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        gap = payload["why"]["details"]["gaps"][0]
        assert gap["kind"] == "ambiguous_grouping"
        assert gap["clause"] == "product type"
        assert gap["actual"]["dimension_ids"] == sorted(
            [PRODUCT_TYPE, "dimension.jaffle_product_type"]
        )
        assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"
        assert "group_by" in payload["why"]["recovery_hints"][0]["message"]
        assert "execute" not in payload["next"].get("ready_for", [])
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("path", "intent", "caller", "group_by", "crowded"),
    [
        # Four more catalog "Product type" dimensions push the competing
        # dimension out of discovery's shortlist; readiness must not rank names.
        (
            "primary",
            "item revenue by product type",
            [CATALOG_PRODUCT_TYPE],
            [CATALOG_PRODUCT_TYPE, PRODUCT_TYPE],
            True,
        ),
        (
            "fallback",
            "item revenue by product type",
            [PRODUCT_TYPE],
            [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE],
            True,
        ),
        ("primary", "item revenue by store", [PRODUCT_TYPE], [PRODUCT_TYPE, STORE], False),
    ],
)
def test_grouping_added_beside_caller_group_by_is_not_execute_ready(
    runtime_factory, monkeypatch, path, intent, caller, group_by, crowded
) -> None:
    runtime = runtime_factory("jaffle_shop")
    if crowded:
        source = next(dim for dim in runtime._config.dimensions if dim.id == CATALOG_PRODUCT_TYPE)
        copies = [
            replace(source, id=f"dimension.product_class_{index}", name=f"ProductClass{index}")
            for index in range(4)
        ]
        dimensions = [*runtime._config.dimensions, *copies]
        monkeypatch.setattr(runtime, "_config", replace(runtime._config, dimensions=dimensions))
    _force_fallback(runtime, monkeypatch, intent, path)
    partial = {"group_by": caller}
    before = deepcopy(partial)
    try:
        payload = plan_payload(runtime, intent=intent, partial_query=partial)
    finally:
        runtime.close()
    assert partial == before
    assert payload["best"]["query_ir"]["group_by"] == group_by
    assert payload["status"] == "low_confidence", payload.get("why")
    assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
    gap = next(
        gap for gap in payload["why"]["details"]["gaps"] if gap["kind"] == "ambiguous_grouping"
    )
    assert gap["clause"] == intent.split(" by ")[1]
    assert gap["actual"]["dimension_ids"] == sorted(group_by)
    assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"
    assert "execute" not in payload["next"].get("ready_for", [])


def test_caller_group_by_naming_every_grouping_is_execute_ready(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    partial = {"group_by": [PRODUCT_TYPE, STORE]}
    try:
        payload = plan_payload(runtime, intent="item revenue by store", partial_query=partial)
        query = payload["best"]["query_ir"]
        assert query["group_by"] == [PRODUCT_TYPE, STORE]
        assert payload["status"] == "ok", payload.get("why")
        assert "execute" in payload["next"].get("ready_for", [])
        measure = query["select"][0]["as"]
        rows = runtime.query(query)["rows"]
        actual = sorted((row[PRODUCT_TYPE], row[STORE], row[measure]) for row in rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = sorted(
                connection.execute(
                    "SELECT i.product_type, s.store_name, SUM(i.item_revenue_cents / 100.0) "
                    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
                    "JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1, 2"
                ).fetchall()
            )
        assert [row[:2] for row in actual] == [row[:2] for row in expected]
        assert [row[2] for row in actual] == pytest.approx([row[2] for row in expected])
    finally:
        runtime.close()
