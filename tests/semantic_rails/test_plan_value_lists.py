"""Named-value folding preserves caller constraints and ranked groupings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

import semantic_rails.planner.plan as plan_module
from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.call import Call
from semantic_rails.planner.generators import (
    _draft_for_choice,
    _matched_value_rows,
    _normalize_value_filters,
)
from semantic_rails.planner.plan_query import _merge_partial_query
from semantic_rails.runtime import Runtime, runtime_request_scope
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config, opened

STORE = "dimension.jaffle_store_name"
STORE_ID = "dimension.jaffle_store_id"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
CATALOG_PRODUCT_TYPE = "dimension.jaffle_product_type"
STORE_DISTRICT = "dimension.store_district"
CUSTOMER_DISTRICT = "dimension.customer_district"
INTENT = "item revenue for Brooklyn from Philadelphia by product type"
BROOKLYN_INTENT = "item revenue for Brooklyn by product type"
CHOICE = {
    "id": "measure.jaffle.item_revenue_usd",
    "kind": "measure",
    "label": "Item revenue (USD)",
}
BOTH_STORES = {"field": STORE, "op": "is", "value": ["Brooklyn", "Philadelphia"]}
ITEM_REVENUE_BY_STORE = (
    "SELECT {} SUM(i.item_revenue_cents / 100.0) "
    "FROM jaffle_item i JOIN jaffle_order o ON i.order_id = o.order_id "
    "JOIN jaffle_store s ON o.store_id = s.store_id {}"
)


@pytest.fixture(scope="module")
def jaffle_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)


@pytest.fixture(scope="module")
def runtime(jaffle_path: Path) -> Iterator[Runtime]:
    with closing(opened(Runtime.from_path(str(jaffle_path)))) as runtime:
        yield runtime


@pytest.fixture()
def fresh(jaffle_path: Path) -> Iterator[Runtime]:
    """A runtime of its own, for a test that changes the package it reads."""
    with closing(Runtime.from_path(str(jaffle_path))) as runtime:
        yield runtime


def _force_fallback(runtime, monkeypatch, intent: str, path: str) -> None:
    if path == "fallback":
        # Composed as plan composes it: inside the caller's request scope.
        result = replace(runtime_request_scope(compose)(runtime, intent), draft=None, pattern="")
        monkeypatch.setattr(plan_module, "compose", lambda *args, **kwargs: result)


def _alias(item: Any) -> str:
    return item.split(".")[-1] if isinstance(item, str) else item["as"]


def _keeping(query: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [
        row for row in query.get("where", []) if row.get("field") == field and row.get("op") == "is"
    ]


def _reference(runtime: Runtime, sql: str) -> list[tuple]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql).fetchall()


def _sorted(where: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filters with their listed values in order."""
    return [
        {**row, "value": sorted(row["value"])} if isinstance(row["value"], list) else row
        for row in where
    ]


def _not_ready(payload: dict[str, Any], gap: str | None = None) -> None:
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "execute" not in payload["next"].get("ready_for", [])
    if gap:
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert gap in [row["kind"] for row in payload["why"]["details"]["gaps"]]


def _ambiguous_grouping(payload: dict[str, Any], clause: str, dimension_ids: list[str]) -> None:
    _not_ready(payload, "ambiguous_grouping")
    gap = next(
        row for row in payload["why"]["details"]["gaps"] if row["kind"] == "ambiguous_grouping"
    )
    assert gap["clause"] == clause
    assert gap["actual"]["dimension_ids"] == sorted(dimension_ids)
    assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("equality", ["is", "="])
def test_separate_equality_clauses_keep_contradiction_and_refuse_execution(
    runtime, monkeypatch, path, equality
) -> None:
    intent = f"item revenue where store {equality} Brooklyn and store {equality} Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent)
    assert _keeping(payload["best"]["query"], STORE) == [
        {"field": STORE, "op": "is", "value": "Brooklyn"},
        {"field": STORE, "op": "is", "value": "Philadelphia"},
    ]
    _not_ready(payload, "contradictory_filters")


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_separate_equality_clauses_match_empty_duckdb_reference(runtime, monkeypatch, path) -> None:
    intent = "item revenue where store is Brooklyn and store is Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query={"group_by": [PRODUCT_TYPE]})
    # Execute the diagnostic IR directly to compare its conjunction with SQL;
    # the public plan must refuse execute readiness for this contradiction.
    assert "execute" not in payload["next"].get("ready_for", [])
    actual = runtime.query(payload["best"]["query"])["rows"]
    where = "WHERE s.store_name = 'Brooklyn' AND s.store_name = 'Philadelphia' GROUP BY 1"
    expected = _reference(runtime, ITEM_REVENUE_BY_STORE.format("i.product_type,", where))
    assert actual == expected == []


def test_values_without_phrase_provenance_keep_separate_equalities(runtime) -> None:
    matched = [
        {"dimension_id": STORE, "value": "Brooklyn"},
        {"dimension_id": STORE, "value": "Philadelphia"},
    ]
    normalized = _normalize_value_filters(Call(), matched, config=runtime._config)
    assert normalized.to_call()["where"] == [
        {"field": STORE, "op": "is", "value": "Brooklyn"},
        {"field": STORE, "op": "is", "value": "Philadelphia"},
    ]


@pytest.mark.parametrize("path", ["primary", "fallback", "plan"])
def test_named_values_share_one_filter_without_adding_grouping(runtime, path: str) -> None:
    if path == "primary":
        draft = compose(runtime, INTENT).draft
        assert draft is not None
        query = draft.query.to_call()
        assert STORE not in draft.interpreted_intent["group_by"]
        assert STORE not in {row["id"] for row in draft.resolved}
    elif path == "fallback":
        query = _draft_for_choice(
            runtime, intent=INTENT, partial_query=None, choice=CHOICE
        ).query.to_call()
    else:
        payload = plan_payload(runtime, intent=INTENT)
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query"]
    filters = _keeping(query, STORE)
    assert len(filters) == 1
    assert filters[0]["op"] == "is"
    assert set(filters[0]["value"]) == {"Brooklyn", "Philadelphia"}
    product_dimension = CATALOG_PRODUCT_TYPE if path == "fallback" else PRODUCT_TYPE
    assert query["group_by"] == [product_dimension]


MONTHLY_REVENUE = (
    "SELECT DATE_TRUNC('month', o.ordered_at), SUM(o.order_total_cents / 100.0) "
    "FROM jaffle_order o LEFT JOIN jaffle_store s ON o.store_id = s.store_id WHERE {} GROUP BY 1"
)


@pytest.mark.parametrize(
    ("intent", "expected_where", "expected_groups", "reference_sql"),
    [
        (
            "revenue for Brooklyn by month",
            [{"field": STORE, "op": "is", "value": "Brooklyn"}],
            [],
            MONTHLY_REVENUE.format("s.store_name = 'Brooklyn'"),
        ),
        # The planner drafts the null-keeping exclusion, which answers the question.
        (
            "revenue not Brooklyn by month",
            [{"field": STORE, "op": "not", "value": "Brooklyn"}],
            [],
            MONTHLY_REVENUE.format("s.store_name IS DISTINCT FROM 'Brooklyn'"),
        ),
        (
            "item revenue for food from Brooklyn by store",
            [
                {"field": STORE, "op": "is", "value": "Brooklyn"},
                {"field": PRODUCT_TYPE, "op": "is", "value": "jaffle"},
            ],
            [STORE_ID, STORE],
            ITEM_REVENUE_BY_STORE.format(
                "s.store_id, s.store_name,",
                "WHERE s.store_name = 'Brooklyn' AND i.product_type = 'jaffle' GROUP BY 1, 2",
            ),
        ),
        # Both named stores in one filter, combined per product type.
        (
            INTENT,
            [BOTH_STORES],
            [PRODUCT_TYPE],
            ITEM_REVENUE_BY_STORE.format(
                "i.product_type,", "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1"
            ),
        ),
    ],
)
def test_single_values_negation_and_distinct_dimensions_keep_their_behavior(
    runtime, intent, expected_where, expected_groups, reference_sql
) -> None:
    payload = plan_payload(runtime, intent=intent)
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert _sorted(query["where"]) == _sorted(expected_where)
    assert query.get("group_by", []) == expected_groups
    # A month's rows key on the period's date; a store's on its key and name.
    keys = expected_groups or ["period"]

    def day(value: Any) -> Any:
        return str(value)[:10] if keys == ["period"] else value

    alias = _alias(query["select"][0])
    actual = sorted(
        (*(day(row[key]) for key in keys), row[alias]) for row in runtime.query(query)["rows"]
    )
    reference = _reference(runtime, reference_sql)
    expected = sorted((*(day(value) for value in row[:-1]), row[-1]) for row in reference)
    assert expected
    assert [row[:-1] for row in actual] == [row[:-1] for row in expected]
    assert [row[-1] for row in actual] == pytest.approx([row[-1] for row in expected])
    if intent == INTENT:
        assert dict(actual) == pytest.approx({"beverage": 381387, "jaffle": 327015})


@pytest.mark.parametrize(
    ("path", "field", "op", "value"),
    [
        ("primary", STORE, "=", "Brooklyn"),
        ("fallback", STORE, "==", "Brooklyn"),
        ("primary", STORE, "IN", ["Brooklyn", "Philadelphia"]),
        ("fallback", STORE, "IN", ["Brooklyn", "Philadelphia"]),
        ("fallback", f" {STORE} ", " in ", "Brooklyn"),
    ],
)
def test_caller_filter_stays_as_written_beside_named_values(
    runtime, monkeypatch, path, field, op, value
) -> None:
    partial = {"where": [{"field": field, "op": op, "value": value}]}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, INTENT, path)
    payload = plan_payload(runtime, intent=INTENT, partial_query=partial)
    assert partial == before
    expected_status = "ok" if isinstance(value, list) and path == "primary" else "low_confidence"
    assert payload["status"] == expected_status, payload.get("why")
    query = payload["best"]["query"]
    assert len(query["where"]) == 2
    # The caller's alias reads as its word.
    assert query["where"][0] == {"field": STORE, "op": "is", "value": value}
    assert query["where"][1]["field"] == STORE
    assert query["where"][1]["op"] == "is"
    assert set(query["where"][1]["value"]) == {"Brooklyn", "Philadelphia"}
    assert STORE not in query["group_by"]


@pytest.mark.parametrize(
    ("path", "intent", "field", "op", "value"),
    [
        ("primary", INTENT, STORE, "!=", "Brooklyn"),
        ("primary", BROOKLYN_INTENT, f" {STORE} ", "NOT IN", ["Brooklyn"]),
        ("fallback", INTENT, f" {STORE} ", "NOT IN", ["Brooklyn"]),
        ("fallback", BROOKLYN_INTENT, STORE, "!=", "Brooklyn"),
    ],
)
def test_caller_exclusion_refuses_a_requested_value(
    runtime, monkeypatch, path, intent, field, op, value
) -> None:
    excluded = {"field": field, "op": op, "value": value}
    written = {"field": STORE, "op": "not", "value": "Brooklyn"}
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query={"where": [excluded]})
    query = payload["best"]["query"]
    assert len(_keeping(query, STORE)) == 1
    assert written in query["where"]
    expected_gap = "filter_values_unrealized" if intent == INTENT else "contradictory_filters"
    _not_ready(payload, expected_gap)


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_caller_equality_outside_named_values_lowers_confidence(runtime, monkeypatch, path) -> None:
    intent = "item revenue for Philadelphia by product type"
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(
        runtime,
        intent=intent,
        partial_query={"where": [{"field": STORE, "op": "is", "value": "Brooklyn"}]},
    )
    _not_ready(payload)
    query = payload["best"]["query"]
    assert _keeping(query, STORE) == [
        {"field": STORE, "op": "is", "value": "Brooklyn"},
        {"field": STORE, "op": "is", "value": "Philadelphia"},
    ]
    assert STORE not in query["group_by"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("primary", ["Brooklyn"]),
        ("fallback", ["Brooklyn"]),
        ("primary", None),
        ("fallback", {"kind": "column", "name": "store"}),
    ],
)
def test_invalid_caller_literal_cannot_bypass_inclusion_normalization(
    runtime, monkeypatch, path, value
) -> None:
    _force_fallback(runtime, monkeypatch, INTENT, path)
    partial = {"where": [{"field": STORE, "op": "is", "value": value}]}
    if value is None or isinstance(value, dict):
        # A null or an expression is no filter value: the call is refused.
        with pytest.raises(SemanticLayerError) as caught:
            plan_payload(runtime, intent=INTENT, partial_query=partial)
        assert caught.value.details["path"] == "where[0].value"
        return
    payload = plan_payload(runtime, intent=INTENT, partial_query=partial)
    assert payload["status"] != "ok"
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize(("path", "field"), [("primary", STORE), ("fallback", f" {STORE} ")])
def test_caller_only_intersection_keeps_filters_and_executed_rows(
    runtime, monkeypatch, path, field
) -> None:
    intent = "item revenue by product type"
    where = [{**BOTH_STORES, "field": field}, {"field": STORE, "op": "is", "value": "Brooklyn"}]
    partial = {"where": where}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query=partial)
    # Fallback's Product type belongs to Product, rather than the measure's Item.
    assert payload["status"] == ("ok" if path == "primary" else "low_confidence"), payload.get(
        "why"
    )
    query = payload["best"]["query"]
    assert query["where"] == [{**row, "field": STORE} for row in where]
    assert partial == before
    assert len(query["group_by"]) == 1 and STORE not in query["group_by"]
    product = query["group_by"][0]
    alias = _alias(query["select"][0])
    actual = {row[product]: row[alias] for row in runtime.query(query)["rows"]}
    where_sql = "WHERE s.store_name = 'Brooklyn' GROUP BY 1"
    expected = dict(_reference(runtime, ITEM_REVENUE_BY_STORE.format("i.product_type,", where_sql)))
    assert actual == pytest.approx(expected)


@pytest.mark.parametrize(
    ("path", "intent", "where"),
    [
        (
            "primary",
            "item revenue by product type",
            [
                {"field": STORE, "op": "is", "value": "Brooklyn"},
                {"field": STORE, "op": "is", "value": "Philadelphia"},
            ],
        ),
        ("fallback", "item revenue for Chicago by product type", [BOTH_STORES]),
    ],
)
def test_unrelated_caller_inclusions_are_preserved_and_refused(
    runtime, monkeypatch, path, intent, where
) -> None:
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
    query = payload["best"]["query"]
    assert query["where"][: len(where)] == where
    assert STORE not in query["group_by"]
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "contradictory_filters" in [row["kind"] for row in payload["why"]["details"]["gaps"]]
    assert "execute" not in payload["next"].get("ready_for", [])


TOP_3_BY_ITEM_REVENUE = ITEM_REVENUE_BY_STORE.format(
    "i.product_type,",
    "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
)


@pytest.mark.parametrize(
    ("path", "grouping", "separator"),
    [
        ("primary", "product type", "from"),
        ("fallback", "product type", "and"),
        ("fallback", "product types", "from"),
    ],
)
def test_ranked_named_values_keep_exact_groupings_and_combined_totals(
    runtime, monkeypatch, path, grouping, separator
) -> None:
    intent = f"top 3 {grouping} by item revenue for Brooklyn {separator} Philadelphia"
    partial = (
        {
            "select": [CHOICE["id"]],
            "group_by": [PRODUCT_TYPE],
            "order_by": [{"field": "item_revenue_usd", "direction": "desc"}],
            "limit": 3,
        }
        if path == "fallback"
        else None
    )
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query=partial)
    assert partial == before
    if path == "fallback":
        _ambiguous_grouping(payload, grouping, [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE])
    else:
        assert payload["status"] == "ok", payload.get("why")
        assert "execute" in payload["next"].get("ready_for", [])
    query = payload["best"]["query"]
    assert {**query, "where": _sorted(query["where"])} == {
        "select": [CHOICE["id"]],
        "group_by": (
            [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE] if path == "fallback" else [PRODUCT_TYPE]
        ),
        "order_by": [{"field": "item_revenue_usd", "direction": "desc"}],
        "limit": 3,
        "where": [BOTH_STORES],
    }
    # Execute the refused fallback's diagnostic IR only for the SQL comparison.
    rows = runtime.query(query)["rows"]
    actual = [(row[PRODUCT_TYPE], row["item_revenue_usd"]) for row in rows]
    expected = _reference(runtime, TOP_3_BY_ITEM_REVENUE)
    assert [product for product, _ in actual] == [product for product, _ in expected]
    assert [value for _, value in actual] == pytest.approx([value for _, value in expected])


def test_ranked_plural_grouping_stays_unresolved_with_both_named_values(runtime) -> None:
    # The primary planner doesn't read a plural grouping name as its singular.
    intent = "top 3 product types by item revenue for Brooklyn and Philadelphia"
    payload = plan_payload(runtime, intent=intent)
    query = payload["best"]["query"]
    _not_ready(payload)
    assert query["limit"] == 3
    assert PRODUCT_TYPE not in query["group_by"]
    assert STORE not in query["group_by"]
    assert _sorted(query["where"]) == [BOTH_STORES]


@pytest.mark.parametrize("caller_has_list", [False, True])
def test_merge_keeps_caller_rows_first_without_adding_grouping(runtime, caller_has_list) -> None:
    equality = {"field": f" {STORE} ", "op": "is", "value": "Brooklyn"}
    caller_rows = [BOTH_STORES, equality] if caller_has_list else [equality]
    query = {"where": [BOTH_STORES], "group_by": [PRODUCT_TYPE]}
    merged = _merge_partial_query(
        runtime._config, Call.from_call(query), Call.from_call({"where": caller_rows})
    ).to_call()
    assert merged["group_by"] == [PRODUCT_TYPE]
    stripped = {**equality, "field": STORE}
    assert merged["where"] == (
        [BOTH_STORES, stripped] if caller_has_list else [stripped, BOTH_STORES]
    )


def test_single_value_strips_field_without_changing_caller_operator(runtime) -> None:
    query = {"where": [{"field": f" {STORE} ", "op": "is", "value": "Brooklyn"}]}
    before = deepcopy(query)
    normalized = _normalize_value_filters(
        Call.from_call(query),
        [{"dimension_id": STORE, "value": "Brooklyn"}],
        config=runtime._config,
    ).to_call()
    assert query == before
    # The caller's condition already keeps the named value: the call holds it once.
    assert normalized["where"] == [{"field": STORE, "op": "is", "value": "Brooklyn"}]
    assert "group_by" not in normalized


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_caller_membership_order_and_case_preserve_equality_and_readiness(
    runtime, monkeypatch, path
) -> None:
    intent = "top 3 product type by item revenue for Brooklyn from Philadelphia"
    _force_fallback(runtime, monkeypatch, intent, path)
    readiness = set()
    for op in ("IN", "in"):
        for values in (["Philadelphia", "Brooklyn"], ["Brooklyn", "Philadelphia"]):
            where = [
                {"field": STORE, "op": op, "value": values},
                {"field": STORE, "op": "is", "value": "Brooklyn"},
            ]
            before = deepcopy(where)
            payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
            assert where == before
            # The caller's alias reads as its word.
            assert payload["best"]["query"]["where"][:2] == [{**where[0], "op": "is"}, where[1]]
            readiness.add((payload["status"], tuple(payload["next"].get("ready_for", []))))
    assert len(readiness) == 1


def test_supplied_ranking_does_not_add_named_value_grouping(runtime, monkeypatch) -> None:
    partial = {
        "group_by": [PRODUCT_TYPE],
        "order_by": [{"field": "item_revenue_usd", "direction": "desc"}],
        "limit": 3,
    }
    payload = plan_payload(runtime, intent=INTENT, partial_query=partial)
    _force_fallback(runtime, monkeypatch, INTENT, "fallback")
    fallback = plan_payload(runtime, intent=INTENT, partial_query=partial)
    assert STORE not in fallback["best"]["query"]["group_by"]
    query = payload["best"]["query"]
    filters = _keeping(query, STORE)
    assert len(filters) == 1 and filters[0]["op"] == "is"
    assert set(filters[0]["value"]) == {"Brooklyn", "Philadelphia"}
    assert query["group_by"] == [PRODUCT_TYPE]
    assert payload["status"] == "ok", payload.get("why")
    rows = runtime.query(query)["rows"]
    actual = [(row[PRODUCT_TYPE], row["item_revenue_usd"]) for row in rows]
    expected = _reference(runtime, TOP_3_BY_ITEM_REVENUE)
    assert [product for product, _ in actual] == [product for product, _ in expected]
    assert [value for _, value in actual] == pytest.approx([value for _, value in expected])


@pytest.mark.parametrize(
    ("path", "intent", "caller_filter"),
    [
        ("mixed-case-draft", "item revenue for Brooklyn and Philadelphia", False),
        ("mixed-case-draft", "item revenue for Brooklyn and Philadelphia", True),
        ("primary", "item revenue for brooklyn and philadelphia", False),
        ("fallback", "item revenue for brooklyn and philadelphia", True),
        ("primary", "item revenue for Brooklyn from Philadelphia", True),
        ("fallback", "item revenue for Brooklyn from Philadelphia", False),
        ("primary", "item revenue for Brooklyn, Philadelphia", False),
        ("fallback", "item revenue for Brooklyn, Philadelphia", True),
    ],
)
def test_unranked_resolved_named_values_execute_a_total_with_caller_filters_preserved(
    runtime, monkeypatch, path, intent, caller_filter
) -> None:
    where = [{"field": STORE, "op": "is", "value": "Brooklyn"}] if caller_filter else []
    _force_fallback(runtime, monkeypatch, intent, path)
    if path == "mixed-case-draft":
        # Direct inference retains case; plan lowercases the shared phrase.
        query = _draft_for_choice(
            runtime, intent=intent, partial_query=Call.from_call({"where": where}), choice=CHOICE
        ).query.to_call()
    else:
        payload = plan_payload(runtime, intent=intent, partial_query={"where": where})
        query = payload["best"]["query"]
        if caller_filter:
            _not_ready(payload, "filter_values_unrealized")
        else:
            assert payload["status"] == "ok", payload.get("why")
            assert "execute" in payload["next"].get("ready_for", [])
    assert not query.get("group_by")
    assert query["where"][: len(where)] == where
    filters = _keeping(query, STORE)
    assert len(filters) == 1 + len(where)
    assert filters[-1]["op"] == "is"
    assert set(filters[-1]["value"]) == {"Brooklyn", "Philadelphia"}
    [row] = runtime.query(query)["rows"]
    [(expected,)] = _reference(
        runtime,
        ITEM_REVENUE_BY_STORE.format(
            "",
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') "
            + ("AND s.store_name = 'Brooklyn'" if caller_filter else ""),
        ),
    )
    assert row[_alias(query["select"][0])] == pytest.approx(expected)


@pytest.mark.parametrize(("path", "where"), [("primary", None), ("fallback", [])])
def test_empty_caller_where_does_not_raise(runtime, monkeypatch, path, where) -> None:
    _force_fallback(runtime, monkeypatch, INTENT, path)
    payload = plan_payload(runtime, intent=INTENT, partial_query={"where": where})
    assert payload["status"] == ("ok" if path == "primary" else "low_confidence"), payload.get(
        "why"
    )
    product_dimension = CATALOG_PRODUCT_TYPE if path == "fallback" else PRODUCT_TYPE
    assert payload["best"]["query"]["group_by"] == [product_dimension]


@pytest.mark.parametrize(
    "values", [["Brooklyn", "Philadelphia"], ["Brooklyn", "Philadelphia", "San Francisco"]]
)
def test_compound_discovery_keeps_each_named_value_and_its_span(runtime, values) -> None:
    intent = ("item revenue for " + ", ".join(values[:-1]) + " and " + values[-1]).lower()
    query = Call.from_call({"select": [CHOICE["id"]]})
    matched = _matched_value_rows(runtime, query, intent)
    assert {row["value"] for row in matched} == set(values)
    assert all(row["dimension_id"] == STORE for row in matched)
    for row in matched:
        start, end = row["matched_span"]
        assert intent[start:end] == row["value"].lower()


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
    runtime, monkeypatch, intent, existing, requested, reference_sql
) -> None:
    partial = {"group_by": [existing]}
    before = deepcopy(partial)
    _force_fallback(runtime, monkeypatch, intent, "fallback")
    payload = plan_payload(runtime, intent=intent, partial_query=partial)
    query = payload["best"]["query"]
    assert partial == before
    assert query["group_by"] == [existing, requested]
    assert payload["why"]["details"]["gaps"][0]["kind"] == "ambiguous_grouping"
    _ambiguous_grouping(payload, intent.split(" by ")[1], [existing, requested])
    # Execute only the diagnostic IR to retain the independent SQL comparison.
    measure = _alias(query["select"][0])
    rows = runtime.query(query)["rows"]
    actual = sorted((row[existing], row[requested], row[measure]) for row in rows)
    expected = sorted(_reference(runtime, reference_sql))
    assert [row[:2] for row in actual] == [row[:2] for row in expected]
    assert [row[2] for row in actual] == pytest.approx([row[2] for row in expected])


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_plan_keeps_both_product_groupings_or_refuses_readiness(runtime, monkeypatch, path) -> None:
    intent = "item revenue by product type and product"
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent)
    if payload["status"] != "ok":
        _not_ready(payload)
        return
    groups = payload["best"]["query"]["group_by"]
    assert len(groups) == 2
    assert groups[0] in {PRODUCT_TYPE, CATALOG_PRODUCT_TYPE}
    assert groups[1] in {
        "dimension.jaffle_product_id",
        "dimension.jaffle_product_name",
        "dimension.jaffle_item_product_name",
        # The Product entity's key: its two naming dimensions leave it alone.
        "dimension.jaffle_product_sku",
    }


@pytest.mark.parametrize(
    ("grouping", "authored_groups"),
    [("product type", [PRODUCT_TYPE]), ("product types", [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE])],
)
def test_fallback_preserves_authored_groups_and_deduplicates_resolved_ids(
    runtime, grouping, authored_groups
) -> None:
    partial = {"group_by": authored_groups}
    before = deepcopy(partial)
    query = _draft_for_choice(
        runtime,
        intent=f"item revenue by {grouping} and store name",
        partial_query=Call.from_call(partial),
        choice=CHOICE,
    ).query.to_call()
    assert query["group_by"] == list(dict.fromkeys([*authored_groups, CATALOG_PRODUCT_TYPE, STORE]))
    assert partial == before


@pytest.mark.parametrize(
    ("path", "label", "name"),
    [
        ("primary", "Item product type", "jaffle.Item.product_type"),
        ("fallback", "ItemProductType", "jaffle.Item.productType"),
        ("primary", "Item product types", "jaffle.Item.product_types"),
    ],
)
def test_ambiguous_added_grouping_cannot_bypass_readiness_guard(
    fresh, monkeypatch, path, label, name
) -> None:
    intent = "item revenue by product type"
    partial = {"group_by": [PRODUCT_TYPE]}
    before = deepcopy(partial)
    fresh._config = replace(
        fresh._config,
        dimensions=[
            replace(dim, label=label, name=name, aliases=[]) if dim.id == PRODUCT_TYPE else dim
            for dim in fresh._config.dimensions
        ],
    )
    result = compose(fresh, intent)
    if path == "primary":
        both = (PRODUCT_TYPE, CATALOG_PRODUCT_TYPE)
        query = replace(result.draft.query, group_by=both)
        result = replace(result, draft=replace(result.draft, query=query))
    else:
        result = replace(result, draft=None, pattern="")
    monkeypatch.setattr(plan_module, "compose", lambda *args, **kwargs: result)
    payload = plan_payload(fresh, intent=intent, partial_query=partial)
    assert partial == before
    assert payload["best"]["query"]["group_by"] == [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE]
    assert payload["why"]["details"]["gaps"][0]["kind"] == "ambiguous_grouping"
    _ambiguous_grouping(payload, "product type", [PRODUCT_TYPE, CATALOG_PRODUCT_TYPE])
    assert "group_by" in payload["why"]["recovery_hints"][0]["message"]


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
        (
            "primary",
            "item revenue by store",
            [PRODUCT_TYPE],
            [PRODUCT_TYPE, STORE_ID, STORE],
            False,
        ),
    ],
)
def test_grouping_added_beside_caller_group_by_is_not_execute_ready(
    fresh, monkeypatch, path, intent, caller, group_by, crowded
) -> None:
    if crowded:
        source = next(dim for dim in fresh._config.dimensions if dim.id == CATALOG_PRODUCT_TYPE)
        copies = [
            replace(source, id=f"dimension.product_class_{index}", name=f"ProductClass{index}")
            for index in range(4)
        ]
        fresh._config = replace(fresh._config, dimensions=[*fresh._config.dimensions, *copies])
    _force_fallback(fresh, monkeypatch, intent, path)
    partial = {"group_by": caller}
    before = deepcopy(partial)
    payload = plan_payload(fresh, intent=intent, partial_query=partial)
    assert partial == before
    assert payload["best"]["query"]["group_by"] == group_by
    _ambiguous_grouping(payload, intent.split(" by ")[1], group_by)


def test_caller_group_by_naming_every_grouping_is_execute_ready(runtime) -> None:
    partial = {"group_by": [PRODUCT_TYPE, STORE]}
    payload = plan_payload(runtime, intent="item revenue by store", partial_query=partial)
    query = payload["best"]["query"]
    # The caller's Store name settles how stores show: the draft adds no Store id.
    assert query["group_by"] == [PRODUCT_TYPE, STORE]
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"].get("ready_for", [])
    measure = _alias(query["select"][0])
    rows = runtime.query(query)["rows"]
    actual = sorted((row[PRODUCT_TYPE], row[STORE], row[measure]) for row in rows)
    expected = sorted(
        _reference(
            runtime, ITEM_REVENUE_BY_STORE.format("i.product_type, s.store_name,", "GROUP BY 1, 2")
        )
    )
    assert [row[:2] for row in actual] == [row[:2] for row in expected]
    assert [row[2] for row in actual] == pytest.approx([row[2] for row in expected])


def _with_districts(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add a store-owned and a customer-owned district; neither is the item's own."""

    dimensions = {dim.id: dim for dim in runtime._config.dimensions}
    districts = [
        replace(
            dimensions[STORE],
            id=STORE_DISTRICT,
            name="store_district",
            label="Store district",
            column="district",
            aliases=[],
            description="",
        ),
        replace(
            dimensions["dimension.jaffle_customer_type"],
            id=CUSTOMER_DISTRICT,
            name="customer_district",
            label="Customer district",
            aliases=[],
            description="",
        ),
    ]
    monkeypatch.setattr(
        runtime, "_config", replace(runtime._config, dimensions=[*dimensions.values(), *districts])
    )


@pytest.mark.parametrize(
    ("intent", "key", "column"),
    [
        ("item revenue by orders", "dimension.jaffle_order_id", "o.order_id"),
        ("item revenue by customers", "dimension.jaffle_customer_id", "o.customer_id"),
    ],
)
def test_a_plural_entity_on_the_fallback_groups_by_its_key(
    runtime, monkeypatch, intent, key, column
) -> None:
    # A plural naming an entity is that entity's key, never a dimension discovery scores.
    _force_fallback(runtime, monkeypatch, intent, "fallback")
    payload = plan_payload(runtime, intent=intent)
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    assert query["group_by"] == [key]
    measure = _alias(query["select"][0])
    actual = sorted((row[key], row[measure]) for row in runtime.query(query)["rows"])
    expected = sorted(
        _reference(
            runtime,
            f"SELECT {column}, SUM(i.item_revenue_cents / 100.0) FROM jaffle_item i "
            "JOIN jaffle_order o ON i.order_id = o.order_id GROUP BY 1",
        )
    )
    assert [row[0] for row in actual] == [row[0] for row in expected]
    assert [row[1] for row in actual] == pytest.approx([row[1] for row in expected])


@pytest.mark.parametrize(
    ("path", "intent"),
    [
        ("primary", "item revenue by districts"),
        ("fallback", "item revenue by the districts"),
        ("primary", "item revenue by their districts"),
        ("fallback", "item revenue by each districts"),
    ],
)
def test_plural_grouping_matching_several_dimensions_is_not_execute_ready(
    fresh, monkeypatch, path, intent
) -> None:
    _with_districts(fresh, monkeypatch)
    _force_fallback(fresh, monkeypatch, intent, path)
    payload = plan_payload(fresh, intent=intent)
    _not_ready(payload)
    assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
    assert payload["why"]["details"]["terms"] == [intent.split(" by ", 1)[1]]
    assert payload["why"]["details"]["ambiguous_groupings"] == [intent.split(" by ", 1)[1]]
    assert payload["why"]["recovery_hints"][0]["kind"] == "clarify_grouping"


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("intent", "caller"),
    [("item revenue by districts", None), ("item revenue by district", [PRODUCT_TYPE])],
)
def test_plan_output_never_names_a_hidden_grouping_dimension(
    fresh, monkeypatch, path, intent, caller
) -> None:
    _with_districts(fresh, monkeypatch)
    hidden = SemanticPolicyConfig(
        id="policy.test.hide_store_district",
        kind="object_visibility",
        object_ids=[STORE_DISTRICT],
        action="hidden",
    )
    policies = [*fresh._config.semantic_policies, hidden]
    fresh._config = replace(fresh._config, semantic_policies=policies)
    _force_fallback(fresh, monkeypatch, intent, path)
    partial = {"group_by": caller} if caller else None
    payloads = {
        detail: plan_payload(fresh, intent=intent, partial_query=partial, detail=detail)
        for detail in ("query", "best", "full", "debug")
    }
    for payload in payloads.values():
        text = json.dumps(payload, default=str)
        assert STORE_DISTRICT not in text
        assert "store district" not in text.lower()
    if caller:
        # A grouping added beside the caller's group_by is still held for clarification.
        payload = payloads["best"]
        _not_ready(payload)
        gap = payload["why"]["details"]["gaps"][0]
        assert gap["kind"] == "ambiguous_grouping"
        assert gap["actual"]["dimension_ids"] == [CUSTOMER_DISTRICT, PRODUCT_TYPE]
