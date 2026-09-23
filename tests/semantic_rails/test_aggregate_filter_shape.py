"""An aggregate's ``filter`` is ``{all: [...]}`` or nothing.

Binding reads only ``filter.all``, but other shapes loaded and validated. An
``any:`` list, or ``any:`` next to ``all:``, was silently ignored, so the
aggregate counted every row. A bare ``{kind: comparison, ...}`` node passed
package validation and then failed each query with a generic error. ``all:``
holding one condition instead of a list, or a list in place of the mapping,
crashed with an internal error. They are all rejected now.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config_validation import validate_runtime_package
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from tests.semantic_rails.conftest import copy_package_config

_NEW_CUSTOMER = {"field": "dimension.jaffle_order_is_new_customer_order", "op": "=", "value": True}
_BARE_COMPARISON = {
    "kind": "comparison",
    "op": "=",
    "left": {"kind": "dimension", "dimension": "dimension.jaffle_order_is_new_customer_order"},
    "right": {"kind": "literal", "value": True},
}


def _aggregate(filter_spec) -> dict:
    return {"kind": "aggregate", "measure": "measure.jaffle.order_count", "filter": filter_spec}


@pytest.mark.parametrize(
    "filter_spec",
    [
        _BARE_COMPARISON,
        {"kind": "metric_predicate", "metric": "metric.sales.orders", "op": ">", "value": 1},
        {"any": [_NEW_CUSTOMER]},
        {"all": [_NEW_CUSTOMER], "any": [_NEW_CUSTOMER]},
        {"all": _NEW_CUSTOMER},
        [_NEW_CUSTOMER],
    ],
    ids=[
        "bare-comparison",
        "bare-metric-predicate",
        "any",
        "all-and-any",
        "all-not-a-list",
        "list",
    ],
)
def test_aggregate_filter_rejects_every_shape_but_all(filter_spec):
    with pytest.raises(SemanticLayerError) as exc:
        parse_semantic_expression(_aggregate(filter_spec), context="query")
    assert exc.value.code == "INVALID_EXPRESSION_AST"
    assert "{all: [...]}" in str(exc.value)


@pytest.mark.parametrize("filter_spec", [{"all": [_NEW_CUSTOMER]}, {}, None])
def test_aggregate_filter_accepts_all_or_nothing(filter_spec):
    expr = parse_semantic_expression(_aggregate(filter_spec), context="query")
    assert expr.filter == (filter_spec or {})


def _orders_by_year(runtime, filter_spec) -> dict:
    return runtime.validate(
        {
            "version": 2,
            "select": [{"expression": _aggregate(filter_spec), "as": "orders"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "year"},
        }
    )


def test_query_with_an_any_filter_is_rejected_not_unfiltered(runtime_factory):
    # This validated and returned every order, [8194, 51458], instead of the
    # new-customer orders, [277, 662].
    runtime = runtime_factory("jaffle_shop")
    try:
        report = _orders_by_year(runtime, {"any": [_NEW_CUSTOMER]})
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
        assert _orders_by_year(runtime, {"all": [_NEW_CUSTOMER]})["ok"] is True
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "filter_spec",
    [_BARE_COMPARISON, {"any": [_NEW_CUSTOMER]}, {"all": _NEW_CUSTOMER}],
    ids=["bare-comparison", "any", "all-not-a-list"],
)
def test_package_metric_with_a_filter_that_is_not_all_fails_validation(tmp_path: Path, filter_spec):
    # Each of these passed package validation.
    package_dir = copy_package_config(tmp_path, "jaffle_shop")
    path = package_dir / "metrics" / "extensions" / "derived_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"]["sales.new_customer_orders_filter_shape"] = {
        "as": "metric.sales.new_customer_orders_filter_shape",
        "label": "New customer orders (filter shape)",
        "description": "An aggregate filter written in a shape other than {all: [...]}.",
        "kind": "derived",
        "value_type": "count",
        "temporal_role": "temporal_role.jaffle_order_time",
        "expression": _aggregate(filter_spec),
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    errors = validate_runtime_package(package_dir)

    assert errors, "a filter that isn't {all: [...]} must not load silently"
    assert any("{all: [...]}" in error for error in errors), errors
