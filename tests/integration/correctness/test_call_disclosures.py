"""What an answer states it assumed, checked against independent reference SQL."""

from __future__ import annotations

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
    assert result["assumptions"] == [
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
    assert result["assumptions"] == []
    # Only a statistic the call changes carries its row count out of the leaf.
    assert "_rows" not in result["rendered_sql"].rsplit("SELECT", 1)[1]


def test_validate_states_the_override_without_rows(duckdb_backend):
    runtime = duckdb_backend.runtimes["utc_authored"]
    spec = ENTRIES["shop/largest-order-by-store-overrides-the-revenue-statistic"]
    result = runtime.validate(spec["query"], verbosity="minimal")
    assert result["assumptions"] == [
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
