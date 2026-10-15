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
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

_NEW_CUSTOMER = {"field": "dimension.jaffle_order_is_new_customer_order", "op": "=", "value": True}
_BARE_COMPARISON = {
    "kind": "comparison",
    "op": "=",
    "left": {"kind": "dimension", "dimension": "dimension.jaffle_order_is_new_customer_order"},
    "right": {"kind": "literal", "value": True},
}

# Each refused filter, and the shape the refusal names: the keys, the type, or the entry.
REFUSED = {
    "bare-comparison": (_BARE_COMPARISON, ["kind", "left", "op", "right"]),
    "bare-metric-predicate": (
        {"kind": "metric_predicate", "metric": "metric.sales.orders", "op": ">", "value": 1},
        ["kind", "metric", "op", "value"],
    ),
    "any": ({"any": [_NEW_CUSTOMER]}, ["any"]),
    "all-and-any": ({"all": [_NEW_CUSTOMER], "any": [_NEW_CUSTOMER]}, ["all", "any"]),
    "all-not-a-list": ({"all": _NEW_CUSTOMER}, ["all"]),
    "list": ([_NEW_CUSTOMER], "list"),
    "empty-list": ([], "list"),
    "empty-string": ("", "str"),
    "zero": (0, "int"),
    "false": (False, "bool"),
    "empty-entry": ({"all": [{}]}, "all[0]"),
    "unknown-entry-key": ({"all": [{"bogus": 1}]}, "all[0]"),
    "null-entry": ({"all": [None]}, "all[0]"),
    "empty-field": ({"all": [{"field": "", "op": "=", "value": True}]}, "all[0]"),
    "unknown-dimension-key": ({"all": [{**_NEW_CUSTOMER, "bogus": 1}]}, "all[0]"),
}


def _aggregate(filter_spec) -> dict:
    return {"kind": "aggregate", "measure": "measure.jaffle.order_count", "filter": filter_spec}


def test_aggregate_filter_rejects_every_shape_but_all():
    for case, (filter_spec, received) in REFUSED.items():
        with pytest.raises(SemanticLayerError) as exc:
            parse_semantic_expression(_aggregate(filter_spec), context="query")
        assert exc.value.code == "INVALID_EXPRESSION_AST", case
        assert exc.value.details == {"expression_kind": "aggregate", "received": received}, case


def test_aggregate_filter_accepts_all_or_nothing():
    for filter_spec in [{"all": [_NEW_CUSTOMER]}, {"all": []}, {}, None]:
        expr = parse_semantic_expression(_aggregate(filter_spec), context="query")
        assert expr.filter == (filter_spec or {})


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(path))
    yield opened(runtime)
    runtime.close()


def _orders_by_year(runtime, filter_spec, operation: str = "validate"):
    return getattr(runtime, operation)(
        {
            "select": [{"expression": _aggregate(filter_spec), "as": "orders"}],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "year"},
        }
    )


def test_a_query_refuses_a_filter_binding_would_ignore(runtime):
    # An any: filter validated and returned every order, [8194, 51458], instead of the
    # new-customer orders, [277, 662].
    for filter_spec in [{"any": [_NEW_CUSTOMER]}, [], {"all": [{}]}, {"all": [{"bogus": 1}]}]:
        report = _orders_by_year(runtime, filter_spec)
        assert report["ok"] is False
        assert report["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
    assert _orders_by_year(runtime, {"all": [_NEW_CUSTOMER]})["ok"] is True


def test_legacy_dimension_clause_is_applied_not_ignored(runtime):
    def rows(filter_spec):
        result = _orders_by_year(runtime, filter_spec, "query")["rows"]
        return sorted(result, key=lambda row: str(row["period"]))

    field_filter = {"all": [_NEW_CUSTOMER]}
    dimension_filter = {"all": [{"dimension": _NEW_CUSTOMER["field"], "op": "=", "value": True}]}
    assert rows(dimension_filter) == rows(field_filter)
    assert rows(field_filter) != rows(None)
    report = _orders_by_year(runtime, {"all": [{"dimension": None, "op": "=", "value": True}]})
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "OBJECT_NOT_FOUND"


def test_package_metrics_with_a_filter_that_is_not_all_fail_validation(tmp_path: Path):
    # Each of these passed package validation. It reports plain messages, so the refusal is
    # found by the shape it names.
    package_dir = copy_package_config(tmp_path, "jaffle_shop")
    path = package_dir / "metrics" / "extensions" / "derived_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for case in ["bare-comparison", "any", "empty-entry"]:
        doc["metrics"]["sales.filter_shape"] = {
            "as": "metric.sales.filter_shape",
            "label": "New customer orders (filter shape)",
            "kind": "derived",
            "value_type": "count",
            "temporal_role": "temporal_role.jaffle_order_time",
            "expression": _aggregate(REFUSED[case][0]),
        }
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        errors = validate_runtime_package(package_dir)
        assert any(
            "metric 'sales.filter_shape'" in error and "{all: [...]}" in error for error in errors
        ), (case, errors)
