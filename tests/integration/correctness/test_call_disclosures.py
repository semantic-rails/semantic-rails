"""What an answer states it assumed, checked against independent reference SQL."""

from __future__ import annotations

from decimal import Decimal

import pytest

from .answer_ledger import load_entries

ENTRIES = dict(load_entries())


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_statistic_override_states_the_rows_it_aggregated(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    spec = ENTRIES["shop/largest-order-by-store-overrides-the-revenue-statistic"]
    result = runtime.query(spec["query"])
    [[rows]] = source.reference("SELECT COUNT(*) FROM orders")
    assert result.get("assumptions", []) == [
        {
            "code": "AGGREGATION_OVERRIDE",
            "measure": "measure.shop.revenue",
            "aggregation": "max",
            "default": "sum",
            "rows": rows,
        }
    ]
    assert all(set(row) == {"dimension.shop_order_store_id", "largest"} for row in result["rows"])
    # The limited answer states the rows behind the numbers it returns.
    top = runtime.query(
        {**spec["query"], "order_by": [{"field": "largest", "direction": "desc"}], "limit": 1}
    )
    [[store_rows]] = source.reference("SELECT COUNT(*) FROM orders WHERE store_id = 'a'")
    assert [entry["rows"] for entry in top["assumptions"]] == [store_rows]


def test_an_override_equal_to_the_default_states_nothing(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    call = {
        "select": [
            {
                "expression": {
                    "kind": "measure",
                    "measure": "measure.shop.revenue",
                    "aggregation": "sum",
                },
                "as": "revenue",
            }
        ]
    }
    result = runtime.query(call)
    assert result.get("assumptions", []) == []
    # Only a statistic the call changes carries its row count out of the leaf.
    assert "_rows" not in result["rendered_sql"].rsplit("SELECT", 1)[1]


def test_validate_states_the_override_without_rows(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    spec = ENTRIES["shop/largest-order-by-store-overrides-the-revenue-statistic"]
    result = runtime.validate(spec["query"], verbosity="minimal")
    assert result.get("assumptions", []) == [
        {
            "code": "AGGREGATION_OVERRIDE",
            "measure": "measure.shop.revenue",
            "aggregation": "max",
            "default": "sum",
        }
    ]


def test_an_aggregation_the_measure_does_not_allow_is_refused_at_its_path(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    call = {
        "select": [
            "measure.shop.order_count",
            {
                "expression": {
                    "kind": "measure",
                    "measure": "measure.shop.order_count",
                    "aggregation": "median",
                }
            },
        ]
    }
    [error] = runtime.validate(call)["errors"]
    assert error["code"] == "UNSUPPORTED_AGGREGATION"
    assert error["details"]["path"] == "select[1].expression.aggregation"
    [hint] = error["details"]["recovery_hints"]
    assert hint["message"] == (
        "Use one of ['count_distinct'], or omit aggregation for the default (count_distinct)."
    )


STORE = "dimension.shop_order_store_id"
ORDER_TIME = "temporal_role.shop_order_ordered_at"


def _share(input_id, over, alias="share"):
    return {"expression": {"kind": "share", "input": input_id, "over": over}, "as": alias}


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_share_states_its_denominator_and_unit(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    spec = ENTRIES["shop/store-share-of-orders-is-a-fraction-of-the-kept-total"]
    [[stores]] = source.reference("SELECT COUNT(DISTINCT COALESCE(store_id, '-')) FROM orders")
    result = runtime.query(spec["query"])
    assert result.get("assumptions", []) == [
        {
            "code": "SHARE_OF_TOTAL",
            "column": "order_share",
            "input": "measure.shop.order_count",
            "over": [STORE],
            "unit": "fraction",
            "rows": stores,
        }
    ]


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_share_over_period_is_taken_within_each_store(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    call = {
        "select": ["measure.shop.revenue", _share("measure.shop.revenue", ["period"])],
        "group_by": [STORE],
        "time": {"temporal_role": ORDER_TIME, "grain": "quarter"},
    }
    reference = source.reference(
        "SELECT store_id, date_trunc('quarter', ordered_at) AS period, SUM(amount) AS revenue, "
        "SUM(amount) / CAST(SUM(SUM(amount)) OVER (PARTITION BY store_id) AS DOUBLE PRECISION) "
        "FROM orders GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2"
    )
    result = runtime.query({**call, "order_by": [{"field": STORE}, {"field": "period"}]})
    rows = [(row[STORE], row["period"], row["share"]) for row in result["rows"]]
    assert [(store, share) for store, _, share in rows] == [
        (store, None if share is None else pytest.approx(float(share), abs=1e-12))
        for store, _, _, share in reference
    ]
    by_store: dict = {}
    for store, _, share in rows:
        by_store[store] = by_store.get(store, 0) + (share or 0)
    assert all(total == pytest.approx(1) for total in by_store.values())


def test_a_share_is_taken_before_limit_and_filtered_by_having_after(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    spec = ENTRIES["shop/store-share-of-orders-is-a-fraction-of-the-kept-total"]
    call = {
        **spec["query"],
        "having": [{"field": "order_share", "op": "lt", "value": 0.5}],
        "order_by": [{"field": "order_share", "direction": "desc"}],
        "limit": 1,
    }
    result = runtime.query(call)
    assert [(row[STORE], row["order_share"]) for row in result["rows"]] == [
        ("b", pytest.approx(4 / 11))
    ]


def test_a_share_of_a_zero_or_unknown_total_is_null(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    # Store b's store-a amount is 0, so its total is 0; no refund has a tax amount.
    zero = {
        "select": [_share("measure.shop.store_a_amount", [STORE])],
        "group_by": [STORE],
        "where": [{"field": STORE, "op": "is", "value": "b"}],
    }
    unknown = {"select": [_share("measure.shop.tax_refunded", [STORE])], "group_by": [STORE]}
    for call in (zero, unknown):
        assert {row["share"] for row in runtime.query(call)["rows"]} == {None}


@pytest.mark.parametrize(
    ("input_id", "over", "code", "path"),
    [
        ("measure.shop.average_order", [STORE], "ROLLUP_UNSAFE", None),
        ("metric.shop.average_order_value", [STORE], "ROLLUP_UNSAFE", None),
        ("measure.shop.order_count", ["period"], "INVALID_QUERY", "select[0].expression.over[0]"),
        (
            "measure.shop.order_count",
            ["dimension.shop_customer_channel"],
            "INVALID_QUERY",
            "select[0].expression.over[0]",
        ),
    ],
)
def test_a_share_needs_an_additive_input_over_its_own_columns(
    duckdb_backend, input_id, over, code, path
):
    runtime = duckdb_backend.runtimes["utc_authored"]
    call = {"select": [_share(input_id, over)], "group_by": [STORE]}
    [error] = runtime.validate(call)["errors"]
    assert error["code"] == code
    if path:
        assert error["details"]["path"] == path


CUSTOMER_NAME = "dimension.shop_customer_name"
CUSTOMER_ID = "dimension.shop_customer_id"


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_a_name_groups_by_its_entity_key_and_says_so(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    spec = ENTRIES["shop/top-customers-by-name-rank-by-key-so-namesakes-stay-apart"]
    result = runtime.query(spec["query"])
    assert [row[CUSTOMER_NAME] for row in result["rows"]] == ["Ann", "Ann"]
    assert result.get("assumptions", []) == [
        {"code": "GROUPED_BY_KEY", "dimension": CUSTOMER_NAME, "key": CUSTOMER_ID}
    ]
    # By name alone the namesakes would merge into one row.
    merged = source.reference(
        "SELECT s.name, SUM(o.amount) FROM orders o JOIN signups s ON o.customer_id = s.customer_id "
        "WHERE s.name = 'Ann' GROUP BY s.name"
    )
    assert [(name, int(total)) for name, total in merged] == [("Ann", 46)]


def test_a_call_that_groups_by_the_key_and_the_name_states_nothing(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    call = {"select": ["measure.shop.revenue"], "group_by": [CUSTOMER_ID, CUSTOMER_NAME]}
    assert runtime.query(call).get("assumptions", []) == []


def test_an_entity_with_no_name_is_shown_by_its_key_and_says_so(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    result = runtime.query(
        {"select": ["measure.shop.revenue"], "group_by": ["dimension.shop_order_id"]}
    )
    assert result.get("assumptions", []) == [
        {
            "code": "SHOWN_BY_KEY",
            "entity": "entity.shop_order",
            "dimension": "dimension.shop_order_id",
        }
    ]
    # A dimension that is not an entity's key says nothing.
    assert (
        runtime.query({"select": ["measure.shop.revenue"], "group_by": [STORE]}).get(
            "assumptions", []
        )
        == []
    )


@pytest.mark.parametrize("backend", ["duckdb", "postgres"])
def test_keyword_aliases_are_names_in_every_clause(request, backend):
    source = request.getfixturevalue(f"{backend}_backend")
    runtime = source.runtimes["utc_authored"]
    call = {
        "select": [
            {"expression": "measure.shop.revenue", "as": "end"},
            {"expression": "measure.shop.order_count", "as": "user"},
            {"expression": "measure.shop.largest_order", "as": "order"},
            {"expression": "measure.shop.smallest_order", "as": "period"},
        ],
        "group_by": [STORE],
        "order_by": [{"field": "user", "direction": "desc"}, {"field": "end"}],
        "having": [{"field": "order", "op": "gte", "value": 6}],
        "limit": 2,
    }
    reference = source.reference(
        'SELECT store_id, SUM(amount) AS "end", COUNT(order_id) AS "user", MAX(amount) AS "order", '
        'MIN(amount) AS "period" FROM orders GROUP BY store_id HAVING MAX(amount) >= 6 '
        'ORDER BY "user" DESC, "end" ASC, store_id ASC NULLS LAST LIMIT 2'
    )
    result = runtime.query(call)
    assert [
        (row[STORE], *(Decimal(str(row[key])) for key in ("end", "user", "order", "period")))
        for row in result["rows"]
    ] == [(store, *(Decimal(str(value)) for value in values)) for store, *values in reference]
    for alias in ("end", "user", "order", "period"):
        assert f'AS "{alias}"' in result["rendered_sql"]


def test_a_share_over_a_name_takes_its_total_over_the_namesakes_too(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    spec = ENTRIES["shop/share-over-a-name-spans-every-namesake"]
    result = runtime.query(spec["query"])
    assert [(row[CUSTOMER_ID], row["share"]) for row in result["rows"]] == [
        (101, pytest.approx(0.5)),
        (103, pytest.approx(0.5)),
    ]


def test_a_select_alias_cannot_take_the_name_of_an_implied_key(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    call = {
        "select": [{"expression": "measure.shop.revenue", "as": CUSTOMER_ID}],
        "group_by": [CUSTOMER_NAME],
    }
    [error] = runtime.validate(call)["errors"]
    assert error["code"] == "DUPLICATE_OUTPUT_ALIAS"
    assert error["details"]["path"] == "select[0]"
