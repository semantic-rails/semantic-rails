"""DATE_DIFF averages against hand-counted calendar days on DuckDB and Postgres."""

from __future__ import annotations

import pytest

from .test_correctness import Case, _answer, _assert_rows, _backend


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("surface", ["package", "aggregate_if"])
@pytest.mark.parametrize("null_endpoint", [False, True])
def test_average_days_to_close(request, backend_name, surface, null_endpoint):
    expression = {"measure": "measure.shop.average_days_to_close"}
    if surface == "aggregate_if":
        expression = {
            "kind": "aggregate_if",
            "aggregation": "avg",
            "condition": {"kind": "literal", "value": True},
            "value": {
                "kind": "call",
                "name": "DATE_DIFF",
                "args": [
                    {"kind": "literal", "value": "day"},
                    {"kind": "column", "column": "ordered_at", "entity": "entity.shop_order"},
                    {"kind": "column", "column": "closed_at", "entity": "entity.shop_order"},
                ],
            },
        }
    query = {"select": [{"expression": expression, "as": "days"}]}
    if null_endpoint:
        # Order 8 is the only NULL store and has no close time.
        query["where"] = [{"field": "dimension.shop_order_store_id", "op": "=", "value": None}]
    backend = _backend(request, backend_name)
    expected = [(None,)] if null_endpoint else [(8 / 3,)]
    reference = "SELECT AVG(CAST(closed_at AS DATE) - CAST(ordered_at AS DATE)) FROM orders"
    if null_endpoint:
        reference += " WHERE store_id IS NULL"
    _assert_rows(expected, backend.reference(reference), "seed's hand count: (2 + 5 + 1) / 3")
    _assert_rows(
        expected,
        _answer(backend, Case("average_days_to_close", "utc_authored", query, reference)),
        f"{backend_name}/{surface}: NULL endpoints must not enter the average as zero",
    )
