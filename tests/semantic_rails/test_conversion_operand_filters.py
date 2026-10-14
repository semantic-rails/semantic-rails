"""Conversion operand filters must reach the answer or fail loudly.

An ad-hoc conversion whose base is "orders containing Product A" and converted is "a later
order containing Product B within 28 days" once validated while both operand filters were
dropped from the compiled SQL: the funnel quietly became "all orders then all orders". Every
operand-level filter either gives the answer reference SQL gives, or the query is rejected with
a structured error.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from semantic_rails.config_validation import resolve_package_reference, validate_config_report
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened


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
    query: dict = {"select": [{"as": "a_then_b_conversion_rate", "expression": expression}]}
    if group_by:
        query["group_by"] = list(group_by)
    return query


_PRODUCT_A_FILTER = {
    "all": [{"field": "dimension.jaffle_product_name", "op": "is", "value": "adele-ade"}]
}
_PRODUCT_B_FILTER = {
    "all": [{"field": "dimension.jaffle_product_name", "op": "is", "value": "chai and mighty"}]
}


_NEW_CUSTOMER_ORDER = {
    "all": [{"field": "dimension.jaffle_order_is_new_customer_order", "op": "is", "value": True}]
}
_REPEAT_ORDER = {
    "all": [{"field": "dimension.jaffle_order_is_new_customer_order", "op": "is", "value": False}]
}
_REPEAT_CUSTOMER = {
    "all": [{"field": "dimension.jaffle_customer_type", "op": "is", "value": "repeat"}]
}
_EVENT_COUNT = {"kind": "entity_count", "accumulation": {"kind": "event"}, "value_type": "count"}
_ORDER_KEY = {"kind": "column", "column": "order_id", "entity": "entity.jaffle_order"}
# Operand measures added to the package, per model file.
_ADDED_MEASURES = {
    "orders.yml": {
        # The entity key, written two ways.
        "keyed_order_count": {**_EVENT_COUNT, "expr": _ORDER_KEY},
        "table_keyed_order_count": {**_EVENT_COUNT, "expr": "jaffle_order.order_id"},
        # ClickHouse treats ORDER_ID and order_id as different columns: the key must match.
        "upper_order_count": {**_EVENT_COUNT, "entity_key": "ORDER_ID"},
        "lifecycle_order_count": {
            **_EVENT_COUNT,
            "expr": {**_ORDER_KEY, "entity": "entity.jaffle_order_lifecycle"},
        },
    },
    "customers.yml": {
        "latest_customer_count": {
            **_EVENT_COUNT,
            "times": ["temporal_role.jaffle_customer_latest_ordered_at"],
        }
    },
}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """jaffle_shop with the added operand measures, shared: every call here only reads."""
    package_dir = copy_package_config(
        tmp_path_factory.mktemp("operands"), "jaffle_shop", preseed_db=True
    )
    for model_file, measures in _ADDED_MEASURES.items():
        model_path = package_dir / "models" / "core" / model_file
        raw = yaml.safe_load(model_path.read_text(encoding="utf-8"))
        raw["model"]["measures"].update(measures)
        model_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    rt = Runtime.from_path(str(package_dir))
    try:
        yield opened(rt)
    finally:
        rt.close()


_PRODUCT_ORDERS = (
    "SELECT DISTINCT o.order_id, o.customer_id, o.ordered_at, o.store_id FROM jaffle_order o "
    "JOIN jaffle_item i ON o.order_id = i.order_id JOIN jaffle_product p ON i.sku = p.sku "
    "WHERE p.product_name = '{}'"
)
_FIRST_ORDERS = (
    "SELECT order_id, customer_id, ordered_at, store_id FROM jaffle_order "
    "WHERE {}is_new_customer_order"
)
_REFERENCE = """
    WITH base AS ({base}), conv AS ({converted})
    SELECT {store} AS store,
      COUNT(DISTINCT CASE WHEN EXISTS (
        SELECT 1 FROM conv c
        WHERE c.customer_id = b.customer_id
          AND c.ordered_at >= b.ordered_at
          AND CAST(c.ordered_at AS TIMESTAMP) < CAST(b.ordered_at AS TIMESTAMP) + INTERVAL 28 DAY
      ) THEN b.order_id END) * 1.0 / COUNT(DISTINCT b.order_id) AS rate
    FROM base b LEFT JOIN jaffle_store s ON s.store_id = b.store_id GROUP BY 1
"""
# Each funnel's operand filters, and its base and converted events in raw SQL.
_FUNNELS = {
    # Orders containing Product A, then a later order containing Product B within 28 days.
    "product_a_then_b": (
        (_PRODUCT_A_FILTER, _PRODUCT_B_FILTER),
        _PRODUCT_ORDERS.format("adele-ade"),
        _PRODUCT_ORDERS.format("chai and mighty"),
    ),
    # The supported way to write a counted condition: count the entity key and put the
    # condition in the operand filter.
    "first_then_repeat": (
        (_NEW_CUSTOMER_ORDER, _REPEAT_ORDER),
        _FIRST_ORDERS.format(""),
        _FIRST_ORDERS.format("NOT "),
    ),
}


@pytest.mark.parametrize(
    ("funnel", "base_measure", "by_store"),
    [
        ("product_a_then_b", "order_count", False),
        # The filters still apply beside a group_by, read on the base event's store.
        ("product_a_then_b", "order_count", True),
        # The entity key may be written however the author likes.
        ("first_then_repeat", "order_count", False),
        ("first_then_repeat", "keyed_order_count", False),
        ("first_then_repeat", "table_keyed_order_count", False),
    ],
)
def test_operand_filters_answer_like_the_reference(
    runtime: Runtime, funnel: str, base_measure: str, by_store: bool
) -> None:
    (base_filter, converted_filter), base, converted = _FUNNELS[funnel]
    query = _conversion_query(
        base_filter=base_filter,
        converted_filter=converted_filter,
        base_measure=f"measure.jaffle.{base_measure}",
        group_by=["dimension.jaffle_store_name"] if by_store else None,
    )
    rows = runtime.query(query)["rows"]
    store = "s.store_name" if by_store else "''"
    reference = runtime._get_adapter().query(
        _REFERENCE.format(base=base, converted=converted, store=store)
    )
    assert all(0 < row["rate"] < 1 for row in reference)
    got = {
        row.get("dimension.jaffle_store_name", ""): row["a_then_b_conversion_rate"] for row in rows
    }
    assert got == {row["store"]: row["rate"] for row in reference}


def test_unfiltered_conversion_sql_is_unchanged_by_the_filter_path(runtime: Runtime) -> None:
    rendered = runtime.validate(_conversion_query(), verbosity="full")["explain"]["rendered_sql"]
    assert "jaffle_item" not in rendered
    assert "jaffle_product" not in rendered


_REPEAT_CLAUSE = {
    "kind": "metric_predicate",
    "entity": "entity.jaffle_customer",
    "input": "measure.jaffle.lifetime_order_count",
    "op": "gt",
    "value": 1,
    "over": "all_time",
}


def _refused(measure: str, side: str = "base", column: str = "", entity: str = "") -> dict:
    """The details of an operand measure that doesn't count its entity's key: the column it
    counts instead, when it counts one."""
    details = {"side": side, "measure": f"measure.jaffle.{measure}"}
    if column:
        details["measure_expr"] = {
            "kind": "column",
            "column": column,
            "entity": entity,
            "table": "",
        }
    return details


@pytest.mark.parametrize(
    ("call", "code", "details", "absent"),
    [
        # A window on an operand, an expression clause in its filter, an unsupported
        # combinator or an unknown dimension is refused, never dropped.
        pytest.param(
            {"base_window": {"kind": "rolling", "unit": "day", "value": 7}},
            "CONVERSION_NOT_SUPPORTED",
            {"side": "base", "window": {"kind": "rolling", "unit": "day", "value": 7}},
            None,
            id="operand_window",
        ),
        pytest.param(
            {"base_filter": {"all": [{"expression": _REPEAT_CLAUSE}]}},
            "CONVERSION_NOT_SUPPORTED",
            {"side": "base"},
            None,
            id="expression_clause",
        ),
        pytest.param(
            # `any:` fails as an invalid AST before conversion checks run.
            {"base_filter": {"any": _PRODUCT_A_FILTER["all"]}},
            "INVALID_EXPRESSION_AST",
            {},
            None,
            id="unsupported_combinator",
        ),
        pytest.param(
            {
                "base_filter": {
                    "all": [{"field": "dimension.does_not_exist", "op": "is", "value": "x"}]
                }
            },
            "OBJECT_NOT_FOUND",
            {},
            None,
            id="unknown_dimension",
        ),
        # Conversion lowering keys each event by the entity key, so an operand measure that
        # counts anything else would silently lose its definition: it is refused.
        pytest.param(
            # CASE WHEN is_new_customer_order THEN order_id END: put the condition in 'filter'.
            {"converted_measure": "measure.jaffle.new_customer_order_count"},
            "CONVERSION_NOT_SUPPORTED",
            _refused("new_customer_order_count", "converted"),
            None,
            id="counts_an_expression",
        ),
        pytest.param(
            # No operand filter turns an order count into a customer count.
            {"base_measure": "measure.jaffle.ordering_customer_count"},
            "CONVERSION_NOT_SUPPORTED",
            _refused("ordering_customer_count", column="customer_id"),
            "'filter'",
            id="counts_another_column",
        ),
        pytest.param(
            {"base_measure": "measure.jaffle.upper_order_count"},
            "CONVERSION_NOT_SUPPORTED",
            {**_refused("upper_order_count", column="ORDER_ID"), "entity_key": ["order_id"]},
            None,
            id="key_in_another_case",
        ),
        pytest.param(
            {"base_measure": "measure.jaffle.lifecycle_order_count"},
            "CONVERSION_NOT_SUPPORTED",
            _refused(
                "lifecycle_order_count", column="order_id", entity="entity.jaffle_order_lifecycle"
            ),
            None,
            id="key_of_another_entity",
        ),
        # Each customer is one event on its first-order clock, so it converts to itself.
        pytest.param(
            {
                "base_measure": "measure.jaffle.customer_count",
                "converted_measure": "measure.jaffle.customer_count",
                "converted_filter": _REPEAT_CUSTOMER,
            },
            "CONVERSION_NOT_SUPPORTED",
            {
                "conversion_single_event": True,
                "entity": "entity.jaffle_customer",
                "temporal_role": "temporal_role.jaffle_customer_first_order_at",
            },
            "measure.jaffle.order_count",
            id="one_event_per_customer",
        ),
        # A customer, then that customer's orders: the window applies.
        pytest.param(
            {
                "base_measure": "measure.jaffle.customer_count",
                "converted_measure": "measure.jaffle.order_count",
                "converted_filter": _REPEAT_CUSTOMER,
            },
            None,
            {},
            None,
            id="customer_then_orders",
        ),
        # The same customer row on another clock is a later event.
        pytest.param(
            {
                "base_measure": "measure.jaffle.customer_count",
                "converted_measure": "measure.jaffle.latest_customer_count",
                "converted_filter": _REPEAT_CUSTOMER,
            },
            None,
            {},
            None,
            id="customer_on_another_clock",
        ),
    ],
)
def test_operand_calls_validate_or_are_refused(
    runtime: Runtime,
    call: dict[str, Any],
    code: str | None,
    details: dict[str, Any],
    absent: str | None,
) -> None:
    report = runtime.validate(_conversion_query(**call))
    assert report["ok"] is (code is None), report["errors"]
    if code is None:
        return
    error = report["errors"][0]
    assert error["code"] == code
    assert details.items() <= error["details"].items()
    if absent:
        assert absent not in error["message"]
    if error["details"].get("conversion_single_event"):
        assert "measure.jaffle.order_count" in error["details"]["candidate_measures"]
    if error["details"].get("entity") == "entity.jaffle_time":
        # The measure counts its key, so only the message says which rows it counts.
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
