"""Metric filters retain their declared grain across grouping and branch lowering."""

from __future__ import annotations

import pytest

from semantic_rails.errors import SemanticLayerError

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
    _item,
    _predicate,
)


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["", "month", "year"])
@pytest.mark.parametrize("threshold", [1, 2])
@pytest.mark.parametrize("where_store", [False, True])
def test_contextual_predicate_groups_by_fact_attributes(
    request, backend_name, grain, threshold, where_store
):
    backend = _backend(request, backend_name)
    query = {
        "select": [_item(ORDERS, "orders"), _item(REVENUE, "revenue")],
        "group_by": [] if where_store else [STORE],
        "metric_filters": [
            _predicate("entity.shop_customer", "contextual", ORDERS, ">=", threshold)
        ],
    }
    groups = [] if where_store else ["o.store_id"]
    context = [] if where_store else ["p.store_id IS NOT DISTINCT FROM o.store_id"]
    where = "o.store_id = 'a'" if where_store else "TRUE"
    if where_store:
        query["where"] = [{"field": STORE, "op": "=", "value": "a"}]
    if grain:
        query["time"] = {"temporal_role": ROLE, "grain": grain}
        groups.append(f"date_trunc('{grain}', o.ordered_at)")
        context.append(f"date_trunc('{grain}', p.ordered_at) = date_trunc('{grain}', o.ordered_at)")
    qualify = " AND ".join(["p.customer_id = o.customer_id", *context])
    predicate_where = "p.store_id = 'a'" if where_store else "TRUE"
    reference = (
        "SELECT "
        + ", ".join([*groups, "COUNT(*)", "COALESCE(SUM(o.amount), 0)"])
        + f" FROM orders o WHERE {where} AND EXISTS ("
        + "SELECT 1 FROM orders p "
        + f"WHERE {predicate_where} AND {qualify} "
        + f"HAVING COUNT(*) >= {threshold})"
        + (" GROUP BY " + ", ".join(groups) if groups else "")
    )
    _assert_rows(
        backend.reference(reference),
        _answer(backend, Case("contextual_fact_attributes", "utc_authored", query, reference)),
        "contextual predicate must count customers within each returned group",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("grain", ["", "month"])
@pytest.mark.parametrize("median", ["none", "inline", "recipe"])
@pytest.mark.parametrize(
    ("measure", "aggregate", "op", "threshold"),
    [
        (REVENUE, "COALESCE(SUM(o.amount), 0)", "<", 30),
        (REVENUE, "COALESCE(SUM(o.amount), 0)", ">=", 30),
        (ORDERS, "COUNT(*)", ">=", 2),
        (ORDERS, "COUNT(*)", ">=", 5),
    ],
)
def test_comparison_filter_beside_distribution_matches_having(
    request, backend_name, grain, median, measure, aggregate, op, threshold
):
    backend = _backend(request, backend_name)
    select = [_item(REVENUE, "revenue"), _item(ORDERS, "orders")]
    values = ["COALESCE(SUM(o.amount), 0)", "COUNT(*)"]
    if median != "none":
        select.append(
            _distribution("median", "median")
            if median == "inline"
            else _item({"metric": "metric.shop.order_revenue_median"}, "median")
        )
        # The NULL amount's per-order sum settles to 0 because revenue has data in scope.
        values.append(MEDIAN.replace("o.amount", "COALESCE(o.amount, 0)"))
    query = {
        "select": select,
        "group_by": [STORE],
        "metric_filters": [
            {
                "expression": {
                    "kind": "comparison",
                    "op": op,
                    "left": measure,
                    "right": {"kind": "literal", "value": threshold},
                },
                "op": "=",
                "value": True,
            }
        ],
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
    expected = backend.reference(reference)
    if median != "none":
        with pytest.raises(SemanticLayerError, match="returned group's grain") as exc:
            _answer(backend, Case("comparison_at_output_grain", "utc_authored", query, reference))
        assert exc.value.code == "REWRITE_NOT_SUPPORTED"
        return
    _assert_rows(
        expected,
        _answer(backend, Case("comparison_at_output_grain", "utc_authored", query, reference)),
        "comparison filters must retain whole groups and their unfiltered medians",
    )


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
def test_distribution_filter_cannot_bypass_fill_refusal(request, backend_name):
    runtime = _backend(request, backend_name).runtimes["utc_authored"]
    query = {
        "select": [_item({"metric": "metric.shop.order_revenue_median"}, "median")],
        "group_by": [STORE],
        "time": {"temporal_role": ROLE, "grain": "month", "fill": True},
        "metric_filters": [{"expression": REVENUE, "op": "<", "value": 30}],
    }
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(query)
    assert exc.value.code == "REWRITE_NOT_SUPPORTED"


@pytest.mark.parametrize("backend_name", ["duckdb", "postgres"])
@pytest.mark.parametrize("scope", ["contextual", "entity_only"])
@pytest.mark.parametrize("entity", ["entity.shop_order", "entity.shop_customer"])
def test_distribution_predicate_retains_declared_grain(request, backend_name, scope, entity):
    backend = _backend(request, backend_name)
    customer = entity == "entity.shop_customer"
    query = {
        "select": [
            _item(ORDERS, "orders"),
            _item({"metric": "metric.shop.order_revenue_median"}, "median"),
        ],
        "group_by": [STORE],
        "metric_filters": [
            _predicate(entity, scope, ORDERS if customer else REVENUE, ">=", 2 if customer else 7)
        ],
    }
    if customer and scope == "contextual":
        with pytest.raises(SemanticLayerError, match="entity_only") as exc:
            backend.runtimes["utc_authored"].query(query)
        assert exc.value.code == "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE"
        return
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
                    "left": {"metric": "metric.shop.order_revenue_median"},
                    "right": {"kind": "literal", "value": 1},
                },
                "shifted_median",
            )
        ]
    }
    with pytest.raises(SemanticLayerError, match="select the distribution separately") as exc:
        runtime.query(query)
    assert exc.value.code == "REWRITE_NOT_SUPPORTED"
