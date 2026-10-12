"""Store groupings preserve the requested attribute even when names repeat."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

import semantic_rails.planner.plan as plan_module
from semantic_rails.planner import compose, plan_payload
from semantic_rails.planner.call import Call
from semantic_rails.planner.intent_holds import _qualifying_entity_why
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.result_helpers import assert_plan_held, typed_rows
from tests.semantic_rails.test_plan_named_groupings import _write_retail_package
from tests.semantic_rails.test_plan_value_lists import _force_fallback

STORE_ID = "dimension.retail_store_id"
STORE_NAME = "dimension.retail_store_name"
STORE_LABEL = "dimension.retail_store_label"
STORE_COLUMNS = {STORE_ID: "store_id", STORE_NAME: "store_name", STORE_LABEL: "store_label"}
ID_FILTER = {"field": STORE_ID, "op": "is", "value": ["a", "b", "c"]}
NAME_FILTER = {"field": STORE_NAME, "op": "is", "value": ["Central", "Harbor"]}
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
# A qualification beside a window, which doesn't say whether each entity's count is taken in
# that window or over all time: plan asks which.
UNSTATED = ("needs_clarification", "ENTITY_FILTER_PERIOD_UNSTATED")


def _alias(item: Any) -> str:
    """A select item's output column: an id's last segment, or its ``as``."""
    return item.split(".")[-1] if isinstance(item, str) else item["as"]


@pytest.fixture(scope="module")
def retail_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_retail_package(tmp_path_factory.mktemp("retail"), explicit_columns=True)


@pytest.fixture(scope="module")
def retail(retail_package: Path) -> Iterator[Runtime]:
    with closing(opened(Runtime.from_path(str(retail_package)))) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    path = copy_package_config(tmp_path_factory.mktemp("jaffle"), "jaffle_shop", preseed_db=True)
    with closing(opened(Runtime.from_path(str(path)))) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def shared_name(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """Two stores share a name, so a ranking by name merges them into one row."""
    package = copy_package_config(
        tmp_path_factory.mktemp("shared"), "jaffle_shop", preseed_db=True, writable=True
    )
    with duckdb.connect(str(package / "jaffle_shop.duckdb")) as connection:
        connection.execute(
            "UPDATE jaffle_store SET store_name = 'Brooklyn' WHERE store_name = 'Philadelphia'"
        )
    with closing(opened(Runtime.from_path(str(package)))) as runtime:
        yield runtime


def _reference(runtime: Runtime, sql: str, parameters: list[Any] | None = None) -> list[tuple]:
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        return connection.execute(sql, parameters or []).fetchall()


# Filters deliberately mention the requested attributes: their words cannot
# discharge the question's obligation to group by those attributes. The fallback path is read
# beside the primary where the two answer differently.
_BOTH = ("primary", "fallback")
_STORE_ATTRIBUTES = [
    ("revenue by store id", [STORE_ID], [], ("primary",)),
    ("revenue by store id", [STORE_ID], [ID_FILTER], _BOTH),
    ("revenue by store code", [STORE_ID], [], _BOTH),
    ("revenue by store key", [STORE_ID], [], _BOTH),
    ("revenue by store number", [STORE_ID], [], _BOTH),
    ("revenue by store name", [STORE_NAME], [NAME_FILTER], _BOTH),
    (
        "revenue by store label",
        [STORE_LABEL],
        [{"field": STORE_LABEL, "op": "is", "value": ["East", "West", "South"]}],
        _BOTH,
    ),
    ("revenue by store", None, [], ("primary",)),
    ("revenue by stores", None, [], ("fallback",)),
    ("revenue by stores", None, [ID_FILTER], ("primary",)),
    ("revenue each store id", [STORE_ID], [ID_FILTER], _BOTH),
    ("revenue every store id", [STORE_ID], [ID_FILTER], _BOTH),
    ("revenue each store name", [STORE_NAME], [NAME_FILTER], _BOTH),
    ("revenue every store name", [STORE_NAME], [NAME_FILTER], _BOTH),
    ("revenue by, store name", [STORE_NAME], [NAME_FILTER], ("fallback",)),
    ("revenue by, store name and store id", [STORE_NAME, STORE_ID], [NAME_FILTER, ID_FILTER], ("primary",)),
    ("revenue for each store name", [STORE_NAME], [NAME_FILTER], _BOTH),
    ("revenue by\tstore name", [STORE_NAME], [NAME_FILTER], ("fallback",)),
    ("revenue by\nstore id", [STORE_ID], [ID_FILTER], ("primary",)),
    ("revenue per store id", [STORE_ID], [ID_FILTER], ("fallback",)),
    ("revenue for each store id", [STORE_ID], [ID_FILTER], _BOTH),
    ("revenue at the store id level", [STORE_ID], [ID_FILTER], ("primary",)),
    ("revenue by the store name", [STORE_NAME], [NAME_FILTER], ("fallback",)),
    ("revenue by store name per month", [STORE_NAME], [NAME_FILTER], ("primary",)),
    ("revenue by store name sorted by revenue", [STORE_NAME], [NAME_FILTER], ("fallback",)),
    ("revenue by store mystery", [STORE_ID], [ID_FILTER], ("primary",)),
    (
        "revenue by store id and store name",
        [STORE_ID, STORE_NAME],
        [
            {"field": STORE_ID, "op": "is", "value": ["a", "b"]},
            {"field": STORE_NAME, "op": "is", "value": ["Central"]},
        ],
        _BOTH,
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    ("path", "intent", "dimensions", "filters"),
    [
        (path, intent, dimensions, filters)
        for intent, dimensions, filters, paths in _STORE_ATTRIBUTES
        for path in paths
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
    query = payload["best"]["query"]
    if dimensions is None:
        dimensions = query.get("group_by", [])
        assert len(dimensions) == 1 and dimensions[0] in STORE_COLUMNS, payload
    assert query.get("group_by") == dimensions, payload
    alias = _alias(query["select"][0])
    columns = [STORE_COLUMNS[dimension] for dimension in dimensions]
    result_columns = list(dimensions)
    if "per month" in intent:
        assert query["time"]["grain"] == "month"
        columns.append("CAST(DATE_TRUNC('month', reported_at) AS DATE)")
        result_columns.append("period")
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
    expected = [
        dict(zip([*result_columns, alias], row, strict=True))
        for row in _reference(retail, reference_sql, parameters)
    ]
    assert sorted(actual, key=str) == sorted(expected, key=str)


@pytest.mark.parametrize(
    ("path", "intent"),
    [
        ("primary", "revenue by the store name"),
        ("fallback", "revenue by store name per month"),
        ("primary", "revenue by store name sorted by revenue"),
        ("fallback", "revenue by, store name"),
        ("primary", "revenue for each store name"),
        ("fallback", "revenue by\tstore name"),
        ("primary", "revenue by\nstore name"),
    ],
)
def test_filtered_store_name_matches_reference_sql_or_withholds_execution(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    dimension = "dimension.jaffle_store_name"
    filters = [{"field": dimension, "op": "is", "value": ["Brooklyn", "Philadelphia"]}]
    _force_fallback(runtime, monkeypatch, intent, path)
    payload = plan_payload(runtime, intent=intent, partial_query={"where": filters})
    if payload["status"] != "ok":
        assert "execute" not in payload["next"].get("ready_for", []), payload
        return
    query = payload["best"]["query"]
    assert "execute" in payload["next"].get("ready_for", []), payload
    assert query.get("group_by") == [dimension], payload
    alias = _alias(query["select"][0])
    rows = typed_rows(runtime.query(query))
    actual = {row[dimension]: row[alias] for row in rows}
    assert len(actual) == len(rows)
    expected = dict(
        _reference(
            runtime,
            "SELECT s.store_name, SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN ('Brooklyn', 'Philadelphia') GROUP BY 1",
        )
    )
    assert actual == pytest.approx(expected)
    assert actual == pytest.approx({"Brooklyn": 259424.85, "Philadelphia": 486468.18})


@pytest.mark.parametrize(("path", "intent"), [("primary", "top 1 store by revenue"),
                                              ("fallback", "top 3 stores by revenue")])  # fmt: skip
def test_store_ranking_with_shared_name_matches_reference_sql_or_withholds_execution(
    shared_name: Runtime, monkeypatch: pytest.MonkeyPatch, path: str, intent: str
) -> None:
    _force_fallback(shared_name, monkeypatch, intent, path)
    payload = plan_payload(shared_name, intent=intent)
    if payload["status"] != "ok":
        assert "execute" not in payload["next"].get("ready_for", []), payload
        return
    assert "execute" in payload["next"].get("ready_for", []), payload
    query = payload["best"]["query"]
    alias = _alias(query["select"][0])
    actual = [row[alias] for row in typed_rows(shared_name.query(query))]
    expected = [
        revenue
        for _, _, revenue in _reference(
            shared_name,
            "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) AS revenue "
            "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
            "GROUP BY s.store_id, s.store_name ORDER BY revenue DESC LIMIT ?",
            [query["limit"]],
        )
    ]
    assert actual == pytest.approx(expected), payload


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_store_answers_by_its_key_and_name_on_both_paths(runtime, monkeypatch, path) -> None:
    # "store" names the Store entity: never a customer's preferred store, found by its words.
    _force_fallback(runtime, monkeypatch, "revenue by store", path)
    payload = plan_payload(runtime, intent="revenue by store")
    query = payload["best"]["query"]
    assert query["group_by"] == ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
    assert payload["status"] == "ok", payload.get("why")
    assert "execute" in payload["next"]["ready_for"]
    alias = _alias(query["select"][0])
    actual = sorted(
        (row["dimension.jaffle_store_id"], row["dimension.jaffle_store_name"], row[alias])
        for row in typed_rows(runtime.query(query))
    )
    expected = _reference(
        runtime,
        "SELECT s.store_id, s.store_name, SUM(o.order_total_cents / 100.0) "
        "FROM jaffle_order o JOIN jaffle_store s ON o.store_id = s.store_id "
        "GROUP BY 1, 2 ORDER BY 1, 2",
    )
    assert [row[:2] for row in actual] == [row[:2] for row in expected]
    assert [float(row[2]) for row in actual] == pytest.approx([float(row[2]) for row in expected])


@pytest.mark.parametrize(
    ("question", "groups", "measure", "aggregation", "held"),
    [
        ("stores with more than 2000 orders", [], "order_count", "count_distinct", True),
        ("stores with more than 2000 orders", ["store_name"], "order_count", "count_distinct", True),
        ("stores with more than 2000 orders", ["store_id"], "order_count", "count_distinct", True),
        ("customers with more than 3 orders", [], "order_count", "count_distinct", True),
        ("customers with more than 3 orders", ["customer_name"], "order_count", "count_distinct", True),
        ("customers with more than 3 orders", ["customer_id"], "order_count", "count_distinct", True),
        ("how many customers with more than 3 orders", [], "customer_count", "count_distinct", True),
        ("how many customers with more than 3 orders", [], "customer_count", "sum", True),
        ("number of stores open", [], "open_store_count_eop", "last_value", False),
    ],
)  # fmt: skip
def test_qualification_requires_entity_keys_or_a_selected_key_count(
    runtime, question, groups, measure, aggregation, held
) -> None:
    intent = parse_intent(runtime, question)
    expression = {
        "kind": "measure",
        "measure": f"measure.jaffle.{measure}",
        "aggregation": aggregation,
    }
    query = {"group_by": [f"dimension.jaffle_{group}" for group in groups],
             "select": [{"expression": expression}]}  # fmt: skip
    why = _qualifying_entity_why(runtime, intent, Call.from_call(query))
    assert bool(why) is held
    if held:
        assert why["code"] == "PLAN_INTENT_COVERAGE_GAP"
    # A count in a qualification predicate never stands in for a selected count.
    if measure == "order_count" and not groups:
        expression["predicates"] = [
            {"measure": "measure.jaffle.customer_count", "aggregation": "count_distinct"}
        ]
        assert _qualifying_entity_why(runtime, intent, Call.from_call(query)) is not None


@pytest.mark.parametrize(("path", "grain"), [("primary", "month"), ("fallback", "year")])
def test_number_of_open_stores_without_one_day_is_held(runtime, monkeypatch, path, grain) -> None:
    question = f"number of stores open by {grain}"
    _force_fallback(runtime, monkeypatch, question, path)
    payload = plan_payload(runtime, intent=question)
    if path == "fallback":
        assert payload["status"] == "low_confidence", payload
        assert "execute" not in payload["next"].get("ready_for", []), payload
        return
    assert_plan_held(payload, "PLAN_INTENT_COVERAGE_GAP")
    [gap] = [
        gap for gap in payload["why"]["details"]["gaps"] if gap["kind"] == "stock_as_of_unrealized"
    ]
    assert gap["expected"] == {"grain": "day", "stocks": ["measure.jaffle.open_store_count_eop"]}
    assert gap["actual"] == {"grain": grain}


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_number_of_open_stores_reads_the_last_complete_day(runtime, monkeypatch, path) -> None:
    # Each store reports three snapshots, then stops: only one still reports on 2018-05-01,
    # while each store's last snapshot would count all five.
    now = "2018-05-02T06:00:00Z"
    question = "number of stores open"
    _force_fallback(runtime, monkeypatch, question, path)
    payload = plan_payload(runtime, intent=question, now=now)
    assert payload["status"] == "ok", payload.get("why")
    query = payload["best"]["query"]
    # The last complete day before now: 2018-05-01.
    assert query["time"]["last"] == {"unit": "day", "value": 1}
    rows = runtime.query(query, now=now)["rows"]
    answer = [row[_alias(query["select"][0])] for row in rows]
    [(gold,)] = _reference(
        runtime,
        "SELECT SUM(open_store_count) FROM jaffle_store_inventory_snapshot "
        "WHERE date_day = DATE '2018-05-01'",
    )
    [(last,)] = _reference(
        runtime,
        "SELECT SUM(open_store_count) FROM jaffle_store_inventory_snapshot "
        "WHERE (store_id, date_day) IN (SELECT (store_id, MAX(date_day)) "
        "FROM jaffle_store_inventory_snapshot GROUP BY store_id)",
    )
    assert answer == [gold] == [1]
    assert last == 5


@pytest.mark.parametrize(
    ("path", "names"), [("primary", ["Brooklyn", "Philadelphia"]), ("fallback", ["Brooklyn"])]
)
def test_named_store_revenue_is_one_combined_monthly_series(
    runtime, monkeypatch, path, names
) -> None:
    question = (
        "revenue for Brooklyn and Philadelphia stores by month"
        if len(names) == 2
        else "revenue for Brooklyn store by month"
    )
    _force_fallback(runtime, monkeypatch, question, path)
    payload = plan_payload(runtime, intent=question)
    assert payload["status"] == "ok", payload
    assert "execute" in payload["next"].get("ready_for", [])
    query = payload["best"]["query"]
    assert not query.get("group_by")
    actual = typed_rows(runtime.query(query))
    alias = _alias(query["select"][0])
    expected = dict(
        _reference(
            runtime,
            "SELECT CAST(date_trunc('month', o.ordered_at) AS DATE), "
            "SUM(o.order_total_cents / 100.0) "
            "FROM jaffle_order o JOIN jaffle_store s USING (store_id) "
            "WHERE s.store_name IN (" + ",".join("?" for _ in names) + ") GROUP BY 1",
            names,
        )
    )
    assert len(actual) == len(expected)
    assert {row["period"]: row[alias] for row in actual} == pytest.approx(expected)


@pytest.mark.parametrize("path", ["forced", "catalog"])
@pytest.mark.parametrize(
    "question",
    ["stores with more than 2000 orders in 2017", "customers with more than 3 orders in 2017"],
)
def test_a_qualification_draft_on_the_fallback_still_holds(
    runtime, monkeypatch, path, question
) -> None:
    result = compose(runtime, question)
    assert result.draft is not None
    assert runtime.validate(result.draft.query.to_call())["ok"]
    assert not result.draft.query.group_by
    if path == "forced":
        # The primary's own draft, offered as the fallback: held as the primary is.
        monkeypatch.setattr(
            plan_module, "compose", lambda *args, **kwargs: replace(result, draft=None)
        )
        monkeypatch.setattr(
            plan_module,
            "_distinct_fallback_drafts",
            lambda *args, **kwargs: [(result.draft, result.pattern)],
        )
        outcome = UNSTATED
    else:
        # A list of stores is answered one row per store, never as one total.
        _force_fallback(runtime, monkeypatch, question, "fallback")
        outcome = ("low_confidence", "PLAN_UNMATCHED_TERMS")
    payload = plan_payload(runtime, intent=question)
    assert (payload["status"], payload["why"]["code"]) == outcome
    assert "execute" not in payload["next"].get("ready_for", [])


# Counted in each month, "in that month" drafts the cohort per period, which a monthly answer
# carries (rows: test_a_cohort_counted_in_each_month_matches_plain_sql).
_EACH_MONTH = [
    "monthly order volume for customers that made more than 10 purchases in that month",
    "monthly orders from customers who made more than 10 purchases in that month",
    "What is the monthly order volume for customers who made more than 10 purchases in that month?",
]


@pytest.mark.parametrize(
    ("question", "outcome"),
    [
        # A qualification beside a window is held on the primary path.
        ("stores with more than 2000 orders in 2017", UNSTATED),
        ("customers with more than 3 orders in 2017", UNSTATED),
        # A daily answer has no month to count "that month" in, so the month goes unused.
        *(
            (question, ("low_confidence", "PLAN_UNMATCHED_TERMS"))
            for question in [
                "daily order volume from customers who made more than 10 purchases in that month",
                "daily order volume from customers with at least 10 orders in that month",
                "What is the daily order volume for customers who made more than 10 purchases in "
                "that month?",
            ]
        ),
        *((question, ("ok", None)) for question in _EACH_MONTH),
    ],
)
def test_qualification_check_only_downgrades_scalar_cohort_answers(
    runtime, monkeypatch, question, outcome
) -> None:
    after = plan_payload(runtime, intent=question)
    with monkeypatch.context() as without_check:
        without_check.setattr(plan_module, "_qualifying_entity_why", lambda *args: None)
        before = plan_payload(runtime, intent=question)
    # Each draft states its cohort and the rows it is counted over, or is held before the
    # check for another reason: with or without the check, plan answers alike.
    for key in ("status", "why", "next"):
        assert after.get(key) == before.get(key)
    assert after["best"]["query"] == before["best"]["query"]
    status, code = outcome
    assert (after["status"], (after.get("why") or {}).get("code")) == (status, code)
    assert ("execute" in after["next"].get("ready_for", [])) is (status == "ok")
    if code == "PLAN_UNMATCHED_TERMS":
        assert after["why"]["details"]["terms"] == ["month"]
    if status != "ok":
        return
    # A cohort counted in each month matches plain SQL.
    query = after["best"]["query"]
    assert query["time"]["grain"] == "month"
    alias = _alias(query["select"][0])
    actual = {row["period"]: row[alias] for row in typed_rows(runtime.query(query))}
    expected = dict(
        _reference(
            runtime,
            "WITH cohort AS (SELECT customer_id, DATE_TRUNC('month', ordered_at) AS month "
            "FROM jaffle_order GROUP BY 1, 2 HAVING COUNT(DISTINCT order_id) > 10) "
            "SELECT CAST(DATE_TRUNC('month', o.ordered_at) AS DATE), "
            "COUNT(DISTINCT o.order_id) FROM jaffle_order o JOIN cohort c "
            "ON o.customer_id = c.customer_id "
            "AND DATE_TRUNC('month', o.ordered_at) = c.month GROUP BY 1",
        )
    )
    assert expected
    assert actual == expected


@pytest.mark.parametrize("path", ["primary", "fallback"])
def test_selected_customer_count_does_not_prove_qualification_scope(
    runtime, monkeypatch, path
) -> None:
    question = "how many customers with more than 3 orders in 2017"
    expression = {
        "kind": "measure",
        "measure": "measure.jaffle.customer_count",
        "aggregation": "count_distinct",
    }
    partial_query = {"select": [{"as": "qualified_customers", "expression": expression}]}
    result = compose(runtime, question)
    assert result.draft is not None
    if path == "fallback":
        monkeypatch.setattr(
            plan_module, "compose", lambda *args, **kwargs: replace(result, draft=None)
        )
        monkeypatch.setattr(
            plan_module,
            "_distinct_fallback_drafts",
            lambda *args, **kwargs: [(result.draft, result.pattern)],
        )
    payload = plan_payload(runtime, intent=question, partial_query=partial_query)
    # The caller's customer count can't be read on the orders' clock the qualification
    # takes, so the draft never validates; either way it is held.
    assert payload["status"] == "low_confidence", payload
    assert payload["why"]["code"] in {"PLAN_FALLBACK_SEMANTIC_DRIFT", "VALIDATION_FAILED"}
    assert "execute" not in payload["next"].get("ready_for", [])


@pytest.mark.parametrize("path", ["primary", "fallback"])
@pytest.mark.parametrize("omit_threshold", [False, True])
def test_grouped_customer_qualification_matches_reference_or_holds(
    runtime, monkeypatch, path, omit_threshold
) -> None:
    question = "customers with more than 3 orders in 2017"
    result = compose(runtime, question)
    assert result.draft is not None
    query = {**result.draft.query.to_call(), "group_by": ["dimension.jaffle_customer_id"]}
    if omit_threshold:
        # The threshold is the customer filter's aggregate condition.
        query["where"] = [row for row in query.get("where", []) if "entity" not in row]
        query = {key: value for key, value in query.items() if value}
        query.pop("having", None)
    assert runtime.validate(query)["ok"]
    draft = replace(result.draft, query=Call.from_call(query))
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda *args, **kwargs: replace(result, draft=draft if path == "primary" else None),
    )
    if path == "fallback":
        monkeypatch.setattr(
            plan_module,
            "_distinct_fallback_drafts",
            lambda *args, **kwargs: [(draft, result.pattern)],
        )
    payload = plan_payload(
        runtime, intent=question, partial_query={"group_by": ["dimension.jaffle_customer_id"]}
    )
    if omit_threshold:
        # The draft drops the threshold the question states.
        assert payload["status"] == "low_confidence", payload
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert payload["why"]["details"]["terms"] == ["3"]
        assert "execute" not in payload["next"].get("ready_for", [])
    elif payload["status"] != "ok":
        # More than 3 orders in 2017, or over every order: plan asks, each option grouped
        # by the caller's customer.
        assert payload["status"] == "needs_clarification", payload
        assert payload["why"]["code"] == "ENTITY_FILTER_PERIOD_UNSTATED"
        assert "execute" not in payload["next"].get("ready_for", [])
        for option in payload["why"]["details"]["options"]:
            call = payload["best"]["query"] if "$ref" in option["call"] else option["call"]
            assert call["group_by"] == ["dimension.jaffle_customer_id"]
    else:
        actual = typed_rows(runtime.query(payload["best"]["query"]))
        expected = {
            row[0]
            for row in _reference(
                runtime,
                "WITH qualified AS ("
                "SELECT customer_id FROM jaffle_order "
                "WHERE ordered_at >= '2017-01-01' AND ordered_at < '2018-01-01' "
                "GROUP BY customer_id HAVING COUNT(DISTINCT order_id) > 3) "
                "SELECT DISTINCT c.customer_id FROM jaffle_customer c "
                "JOIN qualified USING (customer_id)",
            )
        }
        assert len(expected) == 912
        assert {row["dimension.jaffle_customer_id"] for row in actual} == expected
