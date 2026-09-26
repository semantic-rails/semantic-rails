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

from pathlib import Path

import pytest
import yaml

from semantic_rails.config_validation import resolve_package_reference, validate_config_report
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config


def _conversion_query(
    *,
    base_filter: dict | None = None,
    converted_filter: dict | None = None,
    base_window: dict | None = None,
    group_by: list[str] | None = None,
    dimension_bindings: dict | None = None,
    base_measure: str = "measure.jaffle.order_count",
    converted_measure: str = "measure.jaffle.order_count",
    entity: str = "entity.jaffle_customer",
) -> dict:
    base: dict = {"kind": "aggregate", "measure": base_measure}
    converted: dict = {"kind": "aggregate", "measure": converted_measure}
    if base_filter is not None:
        base["filter"] = base_filter
    if converted_filter is not None:
        converted["filter"] = converted_filter
    if base_window is not None:
        base["window"] = base_window
    expression: dict = {
        "kind": "conversion",
        "entity": entity,
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
              AND CAST(c.ordered_at AS TIMESTAMP)
                < CAST(b.ordered_at AS TIMESTAMP) + INTERVAL 28 DAY
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
    # `any:` is not an aggregate filter shape anywhere, so it fails as an invalid AST
    # before the conversion checks run.
    assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"


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


# An entity_count measure can count an expression rather than its entity's key:
# new_customer_order_count counts CASE WHEN is_new_customer_order THEN order_id END.
# Conversion lowering keys each event by the entity key, so such an operand used to
# lose its expression silently: every order became a "new customer order".


@pytest.mark.parametrize("side", ["base", "converted"])
def test_operand_measure_counting_an_expression_is_rejected_not_ignored(runtime_factory, side):
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(**{f"{side}_measure": "measure.jaffle.new_customer_order_count"})
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "measure.jaffle.new_customer_order_count" in error["message"]
    assert "filter" in error["message"]


def test_operand_measure_counting_another_column_is_rejected(runtime_factory):
    # ordering_customer_count counts distinct customer_id on the orders model.
    runtime = runtime_factory("jaffle_shop")
    report = runtime.validate(
        _conversion_query(base_measure="measure.jaffle.ordering_customer_count")
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "counts column 'customer_id'" in error["message"]
    # No operand filter turns an order count into a customer count.
    assert "whose rows are the events" in error["message"]
    assert "'filter'" not in error["message"]


_NEW_CUSTOMER_ORDER = {
    "all": [{"field": "dimension.jaffle_order_is_new_customer_order", "op": "=", "value": True}]
}
_REPEAT_ORDER = {
    "all": [{"field": "dimension.jaffle_order_is_new_customer_order", "op": "=", "value": False}]
}


def test_first_order_then_repeat_order_via_operand_filters_matches_oracle(runtime_factory):
    # The supported way to write the rejected measures: count the entity key and
    # put the condition in the operand filter.
    runtime = runtime_factory("jaffle_shop")
    rows = runtime.query(
        _conversion_query(base_filter=_NEW_CUSTOMER_ORDER, converted_filter=_REPEAT_ORDER)
    )["rows"]
    assert len(rows) == 1
    oracle = runtime._get_adapter().query(
        """
        WITH base AS (
          SELECT order_id, customer_id, ordered_at FROM jaffle_order WHERE is_new_customer_order
        ), conv AS (
          SELECT order_id, customer_id, ordered_at FROM jaffle_order WHERE NOT is_new_customer_order
        )
        SELECT
          COUNT(DISTINCT CASE WHEN EXISTS (
            SELECT 1 FROM conv c
            WHERE c.customer_id = b.customer_id
              AND c.ordered_at >= b.ordered_at
              AND CAST(c.ordered_at AS TIMESTAMP)
                < CAST(b.ordered_at AS TIMESTAMP) + INTERVAL 28 DAY
          ) THEN b.order_id END) * 1.0 / COUNT(DISTINCT b.order_id) AS rate
        FROM base b
        """
    )
    assert 0 < oracle[0]["rate"] < 1
    assert rows[0]["a_then_b_conversion_rate"] == oracle[0]["rate"]


def _runtime_with_measure(tmp_path: Path, model_file: str, name: str, spec: dict) -> Runtime:
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    model_path = package_dir / "models" / "core" / model_file
    raw = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    raw["model"]["measures"][name] = spec
    model_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return Runtime.from_path(str(package_dir))


_EVENT_COUNT = {"kind": "entity_count", "accumulation": {"kind": "event"}, "value_type": "count"}


@pytest.mark.parametrize(
    "counted",
    [
        {"expr": {"kind": "column", "column": "order_id", "entity": "entity.jaffle_order"}},
        {"expr": "jaffle_order.order_id"},
    ],
    ids=["entity-qualified", "table-qualified"],
)
def test_operand_measure_counting_its_key_is_accepted_however_it_is_written(tmp_path, counted):
    runtime = _runtime_with_measure(
        tmp_path, "orders.yml", "keyed_order_count", {**_EVENT_COUNT, **counted}
    )
    filters = {"base_filter": _NEW_CUSTOMER_ORDER, "converted_filter": _REPEAT_ORDER}
    keyed = runtime.query(
        _conversion_query(base_measure="measure.jaffle.keyed_order_count", **filters)
    )["rows"]
    plain = runtime.query(_conversion_query(**filters))["rows"]
    assert 0 < plain[0]["a_then_b_conversion_rate"] < 1
    assert keyed == plain


def test_operand_measure_spelling_its_key_in_another_case_is_rejected(tmp_path):
    # ClickHouse treats ORDER_ID and order_id as different columns, so the key must
    # match exactly; the message shows both spellings.
    runtime = _runtime_with_measure(
        tmp_path, "orders.yml", "upper_order_count", {**_EVENT_COUNT, "entity_key": "ORDER_ID"}
    )
    report = runtime.validate(_conversion_query(base_measure="measure.jaffle.upper_order_count"))
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "counts column 'ORDER_ID', not the key 'order_id'" in error["message"]


def test_operand_measure_counting_the_key_column_of_another_entity_is_rejected(tmp_path):
    counted = {"kind": "column", "column": "order_id", "entity": "entity.jaffle_order_lifecycle"}
    runtime = _runtime_with_measure(
        tmp_path, "orders.yml", "lifecycle_order_count", {**_EVENT_COUNT, "expr": counted}
    )
    report = runtime.validate(
        _conversion_query(base_measure="measure.jaffle.lifecycle_order_count")
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "CONVERSION_NOT_SUPPORTED"
    assert (
        "counts column 'order_id' of 'entity.jaffle_order_lifecycle', not the key 'order_id'"
        in error["message"]
    )


def test_fact_model_operand_measure_is_rejected(tmp_path):
    # A fact-model entity_count measure counts its time column, which is also the time
    # entity's key, but it counts rows of the fact relation, not of the calendar table
    # that conversion lowering reads.
    # Matched on the time entity, which the measure can reach, it used to validate and read
    # the calendar table instead of the rollup.
    runtime = _runtime_with_measure(tmp_path, "daily_metrics.yml", "rollup_day_count", _EVENT_COUNT)
    report = runtime.validate(
        _conversion_query(
            base_measure="measure.jaffle.rollup_day_count",
            converted_measure="measure.jaffle.rollup_day_count",
            entity="entity.jaffle_time",
        )
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "CONVERSION_NOT_SUPPORTED"
    assert "counts rows of 'jaffle_daily_metric_rollup'" in error["message"]


def test_curated_conversion_metric_on_an_expression_measure_fails_package_validation(tmp_path):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    metrics_path = package_dir / "metrics" / "extensions" / "advanced_metrics.yml"
    raw = yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
    metric = raw["metrics"]["sales.session_to_order_conversion_rate_7d"]
    metric["expression"]["converted"]["measure"] = "new_customer_order_count"
    metrics_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))

    assert report["ok"] is False
    failed = [probe for probe in report["probes"] if not probe["ok"]]
    assert [probe["object_id"] for probe in failed] == [
        "metric.sales.session_to_order_conversion_rate_7d"
    ]
    assert failed[0]["error"]["code"] == "CONVERSION_NOT_SUPPORTED"


_REPEAT_CUSTOMER = {
    "all": [{"field": "dimension.jaffle_customer_type", "op": "=", "value": "repeat"}]
}


@pytest.mark.parametrize(
    ("base", "converted", "refused"),
    [
        # Each customer is one event on its first-order clock, so it converts to itself.
        ("customer_count", "customer_count", True),
        # A customer, then that customer's orders: the window applies.
        ("customer_count", "order_count", False),
        # The same customer row on another clock is a later event.
        ("customer_count", "latest_customer_count", False),
    ],
)
def test_operands_counting_the_conversion_entity_on_one_clock_are_rejected(
    tmp_path, base, converted, refused
):
    latest = {**_EVENT_COUNT, "times": ["temporal_role.jaffle_customer_latest_ordered_at"]}
    runtime = _runtime_with_measure(tmp_path, "customers.yml", "latest_customer_count", latest)
    report = runtime.validate(
        _conversion_query(
            base_measure=f"measure.jaffle.{base}",
            converted_measure=f"measure.jaffle.{converted}",
            converted_filter=_REPEAT_CUSTOMER,
        )
    )
    assert report["ok"] is not refused, report["errors"]
    if refused:
        error = report["errors"][0]
        assert error["code"] == "CONVERSION_NOT_SUPPORTED"
        assert "the 28-day window can never apply" in error["message"]
        assert "for example 'measure.jaffle.order_count'" in error["message"]
