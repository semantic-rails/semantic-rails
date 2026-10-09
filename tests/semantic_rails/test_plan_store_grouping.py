"""Store groupings preserve the requested attribute even when names repeat."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.intent_holds import _qualifying_entity_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config
from tests.semantic_rails.result_helpers import assert_plan_held, typed_rows
from tests.semantic_rails.test_plan_value_lists import _force_fallback

STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
STORE_LABEL = "dimension.retail_store_label"
STORE_COLUMNS = {STORE_ID: "store_id", STORE_NAME: "store_name", STORE_LABEL: "store_label"}
ID_FILTER = {"field": STORE_ID, "op": "IN", "value": ["a", "b", "c"]}
NAME_FILTER = {"field": STORE_NAME, "op": "IN", "value": ["Central", "Harbor"]}
# A question that names store attributes plainly is answered, grouped by those attributes.
ANSWERED = {
    "revenue by store id",
    "revenue by store code",
    "revenue by store key",
    "revenue by store number",
    "revenue by store name",
    "revenue by store label",
    "revenue by store id and store name",
}
# "each" and "every" name a grouping the primary draft reads; the catalog fallback holds them.
ANSWERED_PRIMARY = {
    "revenue each store id",
    "revenue every store id",
    "revenue for each store id",
    "revenue each store name",
    "revenue every store name",
    "revenue for each store name",
}


@pytest.fixture()
def retail(tmp_path: Path) -> Iterator[Runtime]:
    (tmp_path / "models").mkdir()
    (tmp_path / "data" / "csv").mkdir(parents=True)
    (tmp_path / "data" / "csv" / "stores.csv").write_text(
        "store_id,store_name,store_label,reported_at,revenue\n"
        "a,Central,East,2026-01-01T09:00:00,10\n"
        "b,Central,West,2026-01-02T09:00:00,25\n"
        "c,Harbor,South,2026-02-01T09:00:00,7\n",
        encoding="utf-8",
    )
    files = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "retail",
                "namespace": "retail",
                "name": "Retail",
                "warehouse": "duckdb",
                "default_db": "data/retail.duckdb",
                "seed": {"kind": "csv_dir_duckdb", "source": "data/csv"},
            },
        },
        "graph.yml": {"graph": {"entities": {"store": {"key": ["store_id"], "model": "stores"}}}},
        "models/stores.yml": {
            "model": {
                "id": "stores",
                "relation": "stores",
                "entities": {"store": {}},
                "times": {
                    "reported_at": {"column": "reported_at", "kind": "timestamp", "default": True}
                },
                "dimensions": {
                    "store_id": {
                        "as": STORE_ID,
                        "column": "store_id",
                        "label": "Store id",
                        "synonyms": ["Store code", "Store key", "Store number"],
                    },
                    "store_name": {"as": STORE_NAME, "column": "store_name", "label": "Store name"},
                    "store_label": {
                        "as": STORE_LABEL,
                        "column": "store_label",
                        "label": "Store label",
                    },
                },
                "measures": {
                    "revenue": {
                        "label": "Revenue",
                        "kind": "aggregate",
                        "expr": "revenue",
                        "default_agg": "sum",
                    }
                },
            }
        },
    }
    for name, body in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    runtime = Runtime.from_path(str(tmp_path))
    try:
        yield runtime
    finally:
        runtime.close()


# Filters deliberately mention the requested attributes: their words cannot
# discharge the question's obligation to group by those attributes.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    ("intent", "dimensions", "filters"),
    [
        ("revenue by store id", [STORE_ID], []),
        ("revenue by store id", [STORE_ID], [ID_FILTER]),
        ("revenue by store code", [STORE_ID], []),
        ("revenue by store key", [STORE_ID], []),
        ("revenue by store number", [STORE_ID], []),
        ("revenue by store name", [STORE_NAME], [NAME_FILTER]),
        (
            "revenue by store label",
            [STORE_LABEL],
            [{"field": STORE_LABEL, "op": "IN", "value": ["East", "West", "South"]}],
        ),
        ("revenue by store", None, []),
        ("revenue by stores", None, []),
        ("revenue by stores", None, [ID_FILTER]),
        ("revenue each store id", [STORE_ID], [ID_FILTER]),
        ("revenue every store id", [STORE_ID], [ID_FILTER]),
        ("revenue each store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue every store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by, store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by, store name and store id", [STORE_NAME, STORE_ID], [NAME_FILTER, ID_FILTER]),
        ("revenue for each store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\tstore name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\nstore name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by\tstore id", [STORE_ID], [ID_FILTER]),
        ("revenue by\nstore id", [STORE_ID], [ID_FILTER]),
        ("revenue per store id", [STORE_ID], [ID_FILTER]),
        ("revenue for each store id", [STORE_ID], [ID_FILTER]),
        ("revenue at the store id level", [STORE_ID], [ID_FILTER]),
        ("revenue by the store name", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store name per month", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store name sorted by revenue", [STORE_NAME], [NAME_FILTER]),
        ("revenue by store mystery", [STORE_ID], [ID_FILTER]),
        (
            "revenue by store id and store name",
            [STORE_ID, STORE_NAME],
            [
                {"field": STORE_ID, "op": "IN", "value": ["a", "b"]},
                {"field": STORE_NAME, "op": "IN", "value": ["Central"]},
            ],
        ),
    ],
)
def test_store_attribute_matches_reference_sql_or_withholds_execution(
    retail: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    intent: str,
    dimensions: list[str] | None,
    filters: list[dict[str, Any]],
) -> None:
    _force_fallback(retail, monkeypatch, intent, path)
    payload = plan_payload(retail, intent=intent, partial_query={"where": filters})
    if intent in ANSWERED or (path == "primary" and intent in ANSWERED_PRIMARY):
        assert payload["status"] == "ok", payload
        # The question names the dimension it groups by, so no row name is assumed.
        assert "assumptions" not in payload, payload
    if payload["status"] != "ok":
        assert "execute" not in payload["next"].get("ready_for", []), payload
        return

    assert "execute" in payload["next"].get("ready_for", []), payload
    query = payload["best"]["query_ir"]
    if dimensions is None:
        dimensions = query.get("group_by", [])
        assert len(dimensions) == 1 and dimensions[0] in STORE_COLUMNS, payload
    assert query.get("group_by") == dimensions, payload
    alias = query["select"][0]["as"]
    columns = [STORE_COLUMNS[dimension] for dimension in dimensions]
    result_columns = list(dimensions)
    if "per month" in intent:
        assert query["time"]["grain"] == "month"
        columns.append("DATE_TRUNC('month', reported_at)")
        result_columns.append(query["time"]["temporal_role"] + "__month")
    predicates = []
    parameters = []
    for row in filters:
        predicates.append(
            STORE_COLUMNS[row["field"]] + " IN (" + ",".join("?" for _ in row["value"]) + ")"
        )
        parameters.extend(row["value"])
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    reference_sql = (
        "SELECT "
        + ", ".join(columns)
        + ", SUM(revenue) FROM stores"
        + where
        + " GROUP BY "
        + ", ".join(str(index + 1) for index in range(len(columns)))
    )
    actual = typed_rows(retail.query(query))
    retail.close()
    with duckdb.connect(retail.db_path, read_only=True) as connection:
        expected = [
            dict(zip([*result_columns, alias], row, strict=True))
            for row in connection.execute(reference_sql, parameters).fetchall()
        ]
    assert sorted(actual, key=str) == sorted(expected, key=str)


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    "intent",
    [
        "revenue by the store name",
        "revenue by store name per month",
        "revenue by store name sorted by revenue",
        "revenue by, store name",
        "revenue for each store name",
        "revenue by\tstore name",
        "revenue by\nstore name",
    ],
)
def test_filtered_store_name_matches_reference_sql_or_withholds_execution(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    dimension = "dimension.jaffle_store_name"
    filters = [{"field": dimension, "op": "IN", "value": ["Brooklyn", "Philadelphia"]}]
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent, partial_query={"where": filters})
        if payload["status"] != "ok":
            assert "execute" not in payload["next"].get("ready_for", []), payload
            return
        query = payload["best"]["query_ir"]
        assert "execute" in payload["next"].get("ready_for", []), payload
        assert query.get("group_by") == [dimension], payload
        alias = query["select"][0]["as"]
        rows = typed_rows(runtime.query(query))
        actual = {row[dimension]: row[alias] for row in rows}
        assert len(actual) == len(rows)
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = dict(
                connection.execute(
                    "SELECT s.store_name, SUM(o.order_total_cents / 100.0) "
                    "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
                    "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1"
                ).fetchall()
            )
        assert actual == pytest.approx(expected)
        assert actual == pytest.approx({"Brooklyn": 259424.85, "Philadelphia": 486468.18})
    finally:
        runtime.close()


# A question that lists stores is answered one row per store, never as one total.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    "intent",
    ["stores with more than 2000 orders in 2017", "customers with more than 3 orders in 2017"],
)
def test_store_list_is_grouped_by_store_or_withholds_execution(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent)
        assert payload["status"] == "low_confidence", payload
        assert "execute" not in payload["next"].get("ready_for", []), payload
        assert payload["why"]["code"] == (
            "PLAN_INTENT_COVERAGE_GAP" if path == "primary" else "PLAN_UNMATCHED_TERMS"
        ), payload
    finally:
        runtime.close()


# Two stores share a name, so a ranking by name merges them into one row.
@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("intent", ["top 1 store by revenue", "top 3 stores by revenue"])
def test_store_ranking_with_shared_name_matches_reference_sql_or_withholds_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    package = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True, writable=True)
    with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "UPDATE jaffle_store SET store_name = 'Brooklyn' WHERE store_name = 'Philadelphia'"
        )
    runtime = Runtime.from_path(str(package))
    try:
        _force_fallback(runtime, monkeypatch, intent, path)
        payload = plan_payload(runtime, intent=intent)
        if payload["status"] != "ok":
            assert "execute" not in payload["next"].get("ready_for", []), payload
            return
        assert "execute" in payload["next"].get("ready_for", []), payload
        query = payload["best"]["query_ir"]
        alias = query["select"][0]["as"]
        actual = [row[alias] for row in typed_rows(runtime.query(query))]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = [
                revenue
                for _, _, revenue in connection.execute(
                    "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) AS revenue "
                    "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                    "GROUP BY s.store_id, s.store_name ORDER BY revenue DESC LIMIT ?",
                    [query["limit"]],
                ).fetchall()
            ]
        assert actual == pytest.approx(expected), payload
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_store_answers_by_its_key_and_name_on_both_paths(
    runtime_factory, monkeypatch, path
) -> None:
    # "store" names the Store entity: never a customer's preferred store, found by its words.
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, "revenue by store", path)
        payload = plan_payload(runtime, intent="revenue by store")
        query = payload["best"]["query_ir"]
        assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
        assert payload["status"] == "ok", payload.get("why")
        assert "execute" in payload["next"]["ready_for"]
        alias = query["select"][0]["as"]
        actual = sorted(
            (row["dimension.jaffle_store_id"], row["dimension.jaffle_store_name"], row[alias])
            for row in typed_rows(runtime.query(query))
        )
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = connection.execute(
                "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) "
                "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
    finally:
        runtime.close()
    assert [row[:2] for row in actual] == [row[:2] for row in expected]
    assert [float(row[2]) for row in actual] == pytest.approx([float(row[2]) for row in expected])


@pytest.mark.parametrize(
    ("question", "groups", "measure", "aggregation", "held"),
    [
        ("stores with more than 2000 orders", [], "order_count", "count_distinct", True),
        (
            "stores with more than 2000 orders",
            ["store_name"],
            "order_count",
            "count_distinct",
            True,
        ),
        ("stores with more than 2000 orders", ["store_id"], "order_count", "count_distinct", True),
        ("customers with more than 3 orders", [], "order_count", "count_distinct", True),
        (
            "customers with more than 3 orders",
            ["customer_name"],
            "order_count",
            "count_distinct",
            True,
        ),
        (
            "customers with more than 3 orders",
            ["customer_id"],
            "order_count",
            "count_distinct",
            True,
        ),
        (
            "how many customers with more than 3 orders",
            [],
            "customer_count",
            "count_distinct",
            True,
        ),
        ("how many customers with more than 3 orders", [], "customer_count", "sum", True),
        ("number of stores open", [], "open_store_count_eop", "last_value", False),
    ],
)
def test_qualification_requires_entity_keys_or_a_selected_key_count(
    runtime_factory, question, groups, measure, aggregation, held
) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        intent = parse_intent(runtime, question)
        query = {
            "group_by": [f"dimension.jaffle_{group}" for group in groups],
            "select": [
                {"expression": {"measure": f"measure.jaffle.{measure}", "aggregation": aggregation}}
            ],
        }
        why = _qualifying_entity_why(runtime, intent, query)
        assert bool(why) is held
        if held:
            assert why["code"] == "PLAN_INTENT_COVERAGE_GAP"
        # A count in a qualification predicate never stands in for a selected count.
        if measure == "order_count" and not groups:
            query["select"][0]["expression"]["predicates"] = [
                {"measure": "measure.jaffle.customer_count", "aggregation": "count_distinct"}
            ]
            assert _qualifying_entity_why(runtime, intent, query) is not None
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("grain", ["month", "year"])
def test_number_of_open_stores_without_one_day_is_held(
    runtime_factory, monkeypatch, path, grain
) -> None:
    question = "number of stores open" + (f" by {grain}" if grain else "")
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, question, path)
        payload = plan_payload(runtime, intent=question)
        if path == "fallback":
            assert payload["status"] == "low_confidence", payload
            assert "execute" not in payload["next"].get("ready_for", []), payload
            return
        assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
        [gap] = [
            gap
            for gap in payload["why"]["details"]["gaps"]
            if gap["kind"] == "stock_as_of_unrealized"
        ]
        assert gap["expected"] == {
            "grain": "day",
            "stocks": ["measure.jaffle.open_store_count_eop"],
        }
        assert gap["actual"] == {"grain": grain}
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_number_of_open_stores_reads_the_last_complete_day(
    runtime_factory, monkeypatch, path
) -> None:
    # Each store reports three snapshots, then stops: only one still reports on 2018-05-01,
    # while each store's last snapshot would count all five.
    now = {"now": "2018-05-02T06:00:00Z"}
    question = "number of stores open"
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, question, path)
        payload = plan_payload(runtime, intent=question, partial_query={"policy_context": now})
        assert payload["status"] == "ok", payload.get("why")
        query = payload["best"]["query_ir"]
        assert (query["time"]["start"], query["time"]["end"]) == ("2018-05-01", "2018-05-02")
        rows = runtime.query({**query, "policy_context": now})["rows"]
        answer = [row[query["select"][0]["as"]] for row in rows]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            [(gold,)] = connection.execute(
                "SELECT SUM(open_store_count) FROM jaffle_store_inventory_snapshot "
                "WHERE date_day = DATE '2018-05-01'"
            ).fetchall()
            [(last,)] = connection.execute(
                "SELECT SUM(open_store_count) FROM jaffle_store_inventory_snapshot "
                "WHERE (store_id, date_day) IN (SELECT (store_id, MAX(date_day)) "
                "FROM jaffle_store_inventory_snapshot GROUP BY store_id)"
            ).fetchall()
        assert answer == [gold] == [1]
        assert last == 5
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("names", [["Brooklyn", "Philadelphia"], ["Brooklyn"]])
def test_named_store_revenue_is_one_combined_monthly_series(
    runtime_factory, monkeypatch, path, names
) -> None:
    question = (
        "revenue for Brooklyn and Philadelphia stores by month"
        if len(names) == 2
        else "revenue for Brooklyn store by month"
    )
    runtime = runtime_factory("jaffle_shop")
    try:
        _force_fallback(runtime, monkeypatch, question, path)
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "ok", payload
        assert "execute" in payload["next"].get("ready_for", [])
        query = payload["best"]["query_ir"]
        assert not query.get("group_by")
        actual = typed_rows(runtime.query(query))
        alias = query["select"][0]["as"]
        runtime.close()
        with duckdb.connect(runtime.db_path, read_only=True) as connection:
            expected = dict(
                connection.execute(
                    "SELECT date_trunc('month', o.ordered_at), SUM(o.order_total_cents / 100.0) "
                    "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
                    "WHERE s.store_name IN (" + ",".join("?" for _ in names) + ") GROUP BY 1",
                    names,
                ).fetchall()
            )
        assert len(actual) == len(expected)
        assert {
            row["temporal_role.jaffle_order_time__month"]: row[alias] for row in actual
        } == pytest.approx(expected)
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize(
    "question",
    ["stores with more than 2000 orders in 2017", "customers with more than 3 orders in 2017"],
)
def test_forcing_a_qualification_draft_through_fallback_still_holds(
    runtime_factory, monkeypatch, path, question
) -> None:
    import semantic_rails.planner.plan as module

    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, question)
        assert result.draft is not None
        assert runtime.validate(result.draft.query)["ok"]
        assert not result.draft.query.get("group_by")
        if path == "fallback":
            monkeypatch.setattr(
                module, "compose", lambda *args, **kwargs: replace(result, draft=None)
            )
            monkeypatch.setattr(
                module,
                "_distinct_fallback_drafts",
                lambda *args, **kwargs: [(result.draft, result.pattern)],
            )
        payload = plan_payload(runtime, intent=question)
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert "execute" not in payload["next"].get("ready_for", [])
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "question",
    [
        "stores with more than 2000 orders in 2017",
        "customers with more than 3 orders in 2017",
        "daily order volume from customers who made more than 10 purchases in that month",
        "daily order volume from customers with at least 10 orders in that month",
        "monthly order volume for customers that made more than 10 purchases in that month",
        "monthly orders from customers who made more than 10 purchases in that month",
        "What is the daily order volume for customers who made more than 10 purchases in that month?",
        "What is the monthly order volume for customers who made more than 10 purchases in that month?",
    ],
)
def test_qualification_check_only_downgrades_scalar_cohort_answers(
    runtime_factory, monkeypatch, question
) -> None:
    import semantic_rails.planner.plan as module

    runtime = runtime_factory("jaffle_shop")
    try:
        after = plan_payload(runtime, intent=question)
        with monkeypatch.context() as without_check:
            without_check.setattr(module, "_qualifying_entity_why", lambda *args: None)
            before = plan_payload(runtime, intent=question)
        assert before["status"] == "ok"
        assert "execute" in before["next"]["ready_for"]
        assert after["status"] == "low_confidence"
        assert after["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert "execute" not in after["next"].get("ready_for", [])
        assert after["best"]["query_ir"] == before["best"]["query_ir"]
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_selected_customer_count_does_not_prove_qualification_scope(
    runtime_factory, monkeypatch, path
) -> None:
    import semantic_rails.planner.plan as module

    question = "how many customers with more than 3 orders in 2017"
    partial_query = {
        "select": [
            {
                "as": "qualified_customers",
                "expression": {
                    "measure": "measure.jaffle.customer_count",
                    "aggregation": "count_distinct",
                },
            }
        ]
    }
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, question)
        assert result.draft is not None
        if path == "fallback":
            monkeypatch.setattr(
                module, "compose", lambda *args, **kwargs: replace(result, draft=None)
            )
            monkeypatch.setattr(
                module,
                "_distinct_fallback_drafts",
                lambda *args, **kwargs: [(result.draft, result.pattern)],
            )
        payload = plan_payload(runtime, intent=question, partial_query=partial_query)
        assert payload["status"] == "low_confidence", payload
        assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
        assert "execute" not in payload["next"].get("ready_for", [])
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("omit_threshold", [False, True])
def test_grouped_customer_qualification_matches_reference_or_holds(
    runtime_factory, monkeypatch, path, omit_threshold
) -> None:
    import semantic_rails.planner.plan as module

    question = "customers with more than 3 orders in 2017"
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, question)
        assert result.draft is not None
        query = {**result.draft.query, "group_by": ["dimension.jaffle_customer_id"]}
        if omit_threshold:
            query.pop("metric_filters", None)
            query.pop("having", None)
        assert runtime.validate(query)["ok"]
        draft = replace(result.draft, query=query)
        monkeypatch.setattr(
            module,
            "compose",
            lambda *args, **kwargs: replace(result, draft=draft if path == "primary" else None),
        )
        if path == "fallback":
            monkeypatch.setattr(
                module,
                "_distinct_fallback_drafts",
                lambda *args, **kwargs: [(draft, result.pattern)],
            )
        payload = plan_payload(
            runtime, intent=question, partial_query={"group_by": ["dimension.jaffle_customer_id"]}
        )
        if omit_threshold or payload["status"] != "ok":
            assert payload["status"] == "low_confidence", payload
            assert payload["why"]["code"] == "PLAN_INTENT_COVERAGE_GAP"
            assert "execute" not in payload["next"].get("ready_for", [])
        else:
            actual = typed_rows(runtime.query(payload["best"]["query_ir"]))
            with duckdb.connect(runtime.db_path, read_only=True) as connection:
                expected = {
                    row[0]
                    for row in connection.execute(
                        "WITH qualified AS ("
                        "SELECT customer_id FROM jaffle_order "
                        "WHERE ordered_at >= '2017-01-01' AND ordered_at < '2018-01-01' "
                        "GROUP BY customer_id HAVING COUNT(DISTINCT order_id) > 3) "
                        "SELECT DISTINCT c.customer_id FROM jaffle_customer c "
                        "JOIN qualified USING (customer_id)"
                    ).fetchall()
                }
            assert len(expected) == 912
            assert {row["dimension.jaffle_customer_id"] for row in actual} == expected
    finally:
        runtime.close()
