"""Conversion operand filters must land in SQL or fail loudly.

External-agent feedback round three: an ad-hoc conversion whose base is
"orders containing Product A" and converted is "a later order containing
Product B within 28 days" validated cleanly while both operand filters
were silently dropped from the compiled SQL — the funnel quietly became
"all orders then all orders". These tests pin the contract: every
operand-level filter either appears in the rendered plan or the query is
rejected with a structured error.
"""

from __future__ import annotations


def _conversion_query(
    *,
    base_filter: dict | None = None,
    converted_filter: dict | None = None,
    base_window: dict | None = None,
    group_by: list[str] | None = None,
    dimension_bindings: dict | None = None,
) -> dict:
    base: dict = {"kind": "aggregate", "measure": "measure.jaffle.order_count"}
    converted: dict = {"kind": "aggregate", "measure": "measure.jaffle.order_count"}
    if base_filter is not None:
        base["filter"] = base_filter
    if converted_filter is not None:
        converted["filter"] = converted_filter
    if base_window is not None:
        base["window"] = base_window
    expression: dict = {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
        "base": base,
        "converted": converted,
    }
    if dimension_bindings is not None:
        expression["dimension_bindings"] = dimension_bindings
    query: dict = {
        "version": 2,
        "select": [{"as": "a_then_b_conversion_rate", "expression": expression}],
        "verbosity": "full",
    }
    if group_by:
        query["group_by"] = list(group_by)
    return query


_PRODUCT_A_FILTER = {
    "all": [{"field": "dimension.jaffle_product_name", "op": "=", "value": "adele-ade"}]
}
_PRODUCT_B_FILTER = {
    "all": [{"field": "dimension.jaffle_product_name", "op": "=", "value": "chai and mighty"}]
}


def test_base_and_converted_operand_filters_land_in_compiled_sql(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(base_filter=_PRODUCT_A_FILTER, converted_filter=_PRODUCT_B_FILTER)
    )
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    base_cte, converted_cte = rendered.split("conversion_converted_1 AS (", 1)
    assert "adele-ade" in base_cte
    assert "adele-ade" not in converted_cte
    assert "chai and mighty" in converted_cte


def test_product_a_then_product_b_conversion_matches_oracle(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    rows = runtime.query(
        _conversion_query(base_filter=_PRODUCT_A_FILTER, converted_filter=_PRODUCT_B_FILTER)
    )["rows"]
    assert len(rows) == 1
    oracle = runtime._get_adapter().query(
        """
        WITH base AS (
          SELECT DISTINCT o.order_id, o.customer_id, o.ordered_at
          FROM jaffle_order o
          JOIN jaffle_item i ON o.order_id = i.order_id
          JOIN jaffle_product p ON i.sku = p.sku
          WHERE p.product_name = 'adele-ade'
        ), conv AS (
          SELECT DISTINCT o.order_id, o.customer_id, o.ordered_at
          FROM jaffle_order o
          JOIN jaffle_item i ON o.order_id = i.order_id
          JOIN jaffle_product p ON i.sku = p.sku
          WHERE p.product_name = 'chai and mighty'
        )
        SELECT
          COUNT(DISTINCT CASE WHEN EXISTS (
            SELECT 1 FROM conv c
            WHERE c.customer_id = b.customer_id
              AND c.ordered_at >= b.ordered_at
              AND DATE_DIFF(
                'day', CAST(b.ordered_at AS TIMESTAMP), CAST(c.ordered_at AS TIMESTAMP)
              ) <= 28
          ) THEN b.order_id END) * 1.0 / COUNT(DISTINCT b.order_id) AS rate
        FROM base b
        """
    )
    assert rows[0]["a_then_b_conversion_rate"] == oracle[0]["rate"]


def test_operand_filters_apply_alongside_group_by(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    query = _conversion_query(
        base_filter=_PRODUCT_A_FILTER,
        converted_filter=_PRODUCT_B_FILTER,
        group_by=["dimension.jaffle_store_name"],
    )
    report = runtime.validate(query)
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    assert "adele-ade" in rendered
    assert "chai and mighty" in rendered
    rows = runtime.query(query)["rows"]
    assert rows, "expected at least one store-level conversion row"
    assert all(0 <= row["a_then_b_conversion_rate"] <= 1 for row in rows)


def test_operand_window_is_rejected_with_structured_error(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(base_window={"kind": "rolling", "unit": "day", "value": 7})
    )
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "window" in report["errors"][0]["message"].lower()


def test_operand_expression_filter_clause_is_rejected_not_dropped(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(
            base_filter={
                "all": [
                    {
                        "expression": {
                            "kind": "metric_predicate",
                            "entity": "entity.jaffle_customer",
                            "scope_mode": "entity_only",
                            "input": {"measure": "measure.jaffle.lifetime_order_count"},
                            "op": ">",
                            "value": 1,
                        }
                    }
                ]
            }
        )
    )
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "metric_filters" in report["errors"][0]["message"]


def test_operand_filter_with_unsupported_combinator_is_rejected(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(
            base_filter={
                "any": [{"field": "dimension.jaffle_product_name", "op": "=", "value": "adele-ade"}]
            }
        )
    )
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "CONVERSION_NOT_SUPPORTED"


def test_operand_filter_with_unknown_dimension_is_rejected(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(
            base_filter={"all": [{"field": "dimension.does_not_exist", "op": "=", "value": "x"}]}
        )
    )
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "OBJECT_NOT_FOUND"


def test_unfiltered_conversion_sql_is_unchanged_by_the_filter_path(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(_conversion_query())
    assert report["ok"] is True
    rendered = report["explain"]["rendered_sql"]
    assert "jaffle_item" not in rendered
    assert "jaffle_product" not in rendered
