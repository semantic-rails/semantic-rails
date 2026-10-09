"""Metric filters retain their declared grain across grouping and branch lowering."""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.result_helpers import typed_rows

from .test_correctness import (
    MEDIAN,
    ORDERS,
    REVENUE,
    ROLE,
    STORE,
    Case,
    _answer,
    _assert_rows,
    _backend,
    _distribution,
    _having,
    _item,
)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["", "month", "year"])
@pytest.mark.parametrize("threshold", [1, 2])
@pytest.mark.parametrize("where_store", [False, True])
def test_contextual_predicate_groups_by_fact_attributes(
    request, backend_name, grain, threshold, where_store
):
    # A call's entity filter is never grouped by the query's other dimensions; a package's
    # contextual predicate is, so this reads the engine's own form.
    backend = _backend(request, backend_name)
    runtime = backend.runtimes["utc_authored"]
    query = {
        "select": [_item(ORDERS, "orders"), _item(REVENUE, "revenue")],
        "group_by": [] if where_store else [STORE],
    }
    groups = [] if where_store else ["o.store_id"]
    context = [] if where_store else ["p.store_id IS NOT DISTINCT FROM o.store_id"]
    where = "o.store_id = 'a'" if where_store else "TRUE"
    if where_store:
        query["where"] = [{"field": STORE, "op": "is", "value": "a"}]
        # A where filter beside a metric predicate is judged inside the filters only.
        query["observation_scope"] = "query"
    if grain:
        query["time"] = {"temporal_role": ROLE, "grain": grain}
        groups.append(f"date_trunc('{grain}', o.ordered_at)")
        context.append(f"date_trunc('{grain}', p.ordered_at) = date_trunc('{grain}', o.ordered_at)")
    qualify = " AND ".join(["p.customer_id = o.customer_id", *context])
    predicate_where = "p.store_id = 'a'" if where_store else "TRUE"
    # Every group has rows: SQL SUM stays NULL when all its amounts are NULL (order 7's group).
    reference = (
        "SELECT "
        + ", ".join([*groups, "COUNT(*)", "SUM(o.amount)"])
        + f" FROM orders o WHERE {where} AND EXISTS ("
        + "SELECT 1 FROM orders p "
        + f"WHERE {predicate_where} AND {qualify} "
        + f"HAVING COUNT(*) >= {threshold})"
        + (" GROUP BY " + ", ".join(groups) if groups else "")
    )
    payload = runtime.engine_payload(query)
    predicate = {
        "kind": "metric_predicate",
        "entity": "entity.shop_customer",
        "scope_mode": "contextual",
        "input": {"kind": "measure", "measure": ORDERS},
        "op": ">=",
        "value": threshold,
    }
    payload["metric_filters"] = [{"expression": predicate, "op": "=", "value": True}]
    _assert_rows(
        backend.reference(reference),
        [tuple(row.values()) for row in typed_rows(runtime.query_internal(payload))],
        "contextual predicate must count customers within each returned group",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["", "month"])
@pytest.mark.parametrize(
    ("alias", "aggregate", "op", "threshold"),
    [
        ("revenue", "SUM(o.amount)", "<", 30),
        ("revenue", "SUM(o.amount)", ">=", 30),
        ("orders", "COUNT(*)", ">=", 2),
        ("orders", "COUNT(*)", ">=", 5),
    ],
)
def test_comparison_filter_matches_having(
    request, backend_name, grain, alias, aggregate, op, threshold
):
    backend = _backend(request, backend_name)
    select = [_item(REVENUE, "revenue"), _item(ORDERS, "orders")]
    values = ["SUM(o.amount)", "COUNT(*)"]
    query = {
        "select": select,
        "group_by": [STORE],
        "having": [{"field": alias, "op": op, "value": threshold}],
    }
    groups = ["o.store_id"]
    if grain:
        query["time"] = {"temporal_role": ROLE, "grain": grain}
        groups.append(f"date_trunc('{grain}', o.ordered_at)")
    # SQL projection order follows Query IR: dimensions, time, then selected expressions.
    reference = (
        "SELECT "
        + ", ".join([*groups, *values])
        + " FROM orders o GROUP BY "
        + ", ".join(groups)
        + f" HAVING {aggregate} {op} {threshold}"
    )
    _assert_rows(
        backend.reference(reference),
        _answer(backend, Case("comparison_at_output_grain", "utc_authored", query, reference)),
        "comparison filters must retain whole groups",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("median", ["inline", "recipe"])
def test_comparison_filter_beside_distribution_refuses_changed_grain(request, backend_name, median):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    query = {
        "select": [
            _item(REVENUE, "revenue"),
            _item(ORDERS, "orders"),
            _distribution("median", "median")
            if median == "inline"
            else _item("metric.shop.order_revenue_median", "median"),
        ],
        "group_by": [STORE],
        "having": [{"field": "revenue", "op": "lt", "value": 30}],
    }
    with pytest.raises(SemanticLayerError, match="returned group's grain") as exc:
        runtime.query(query)
    assert exc.value.code == "REWRITE_NOT_SUPPORTED"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_distribution_filter_cannot_bypass_fill_refusal(request, backend_name):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    query = {
        "select": [_item("metric.shop.order_revenue_median", "median")],
        "group_by": [STORE],
        "time": {"temporal_role": ROLE, "grain": "month", "fill": True},
        "having": [{"field": REVENUE, "op": "lt", "value": 30}],
    }
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)
    assert exc.value.code == "REWRITE_NOT_SUPPORTED"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("entity", ["entity.shop_order", "entity.shop_customer"])
def test_distribution_predicate_retains_declared_grain(request, backend_name, entity):
    backend = _backend(request, backend_name)
    customer = entity == "entity.shop_customer"
    query = {
        "select": [_item(ORDERS, "orders"), _item("metric.shop.order_revenue_median", "median")],
        "group_by": [STORE],
        "where": [
            _having(
                entity, "all_time", ORDERS if customer else REVENUE, "gte", 2 if customer else 7
            )
        ],
    }
    where = (
        "o.customer_id IN (SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) >= 2)"
        if customer
        else "o.amount >= 7"
    )
    reference = f"SELECT o.store_id, COUNT(*), {MEDIAN} FROM orders o WHERE {where} GROUP BY 1"
    _assert_rows(
        backend.reference(reference),
        _answer(backend, Case("distribution_entity_predicate", "utc_authored", query, reference)),
        "entity predicates must preserve the distribution's population",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_nested_distribution_cannot_bypass_grain_guard(request, backend_name):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    query = {
        "select": [
            _item(
                {
                    "kind": "arithmetic",
                    "op": "add",
                    "left": "metric.shop.order_revenue_median",
                    "right": {"kind": "literal", "value": 1},
                },
                "shifted_median",
            )
        ]
    }
    with pytest.raises(SemanticLayerError, match="select the distribution separately") as exc:
        runtime.query(query)
    assert exc.value.code == "REWRITE_NOT_SUPPORTED"
