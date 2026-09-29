"""A metric recipe keeps every part its author wrote, or refuses to load.

The loader used to rebuild an authored expression from the fields it knew and drop
the rest, so a first-week metric loaded as a bare measure and returned a lifetime
value. These tests pin the two halves: parts survive into the loaded expression,
and a part the loader cannot keep is a load error that names the metric and the part.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import _convert_recipe_expr, load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import ScopedAggregateExpr
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
FIRST_ORDER_AT = "temporal_role.jaffle_customer_first_order_at"
LARGE_ORDER = "dimension.jaffle_order_is_large_order"

FIRST_90_DAYS = {
    "kind": "scoped_aggregate",
    "measure": REVENUE,
    "aggregation": "sum",
    "anchor": {"temporal_role": FIRST_ORDER_AT},
    "window": {"unit": "day", "value": 90, "direction": "forward"},
}
LARGE_ORDERS = {
    "kind": "scoped_aggregate",
    "measure": ORDERS,
    "aggregation": "count_distinct",
    "where": [{"field": LARGE_ORDER, "op": "=", "value": True}],
}


def _package_with_metrics(tmp_path: Path, expressions: dict[str, dict[str, Any]]) -> Path:
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    metrics = {
        f"probe.{key}": {
            "as": f"metric.probe.{key}",
            "label": key,
            "description": key,
            "kind": "derived",
            "value_type": "count",
            "temporal_role": "temporal_role.jaffle_order_time",
            "expression": expression,
        }
        for key, expression in expressions.items()
    }
    path = package_dir / "metrics" / "extensions" / "probe_metrics.yml"
    path.write_text(yaml.safe_dump({"metrics": metrics}), encoding="utf-8")
    return package_dir


def _runtime(monkeypatch: pytest.MonkeyPatch, package_dir: Path) -> Runtime:
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"jaffle_shop": str(package_dir)}
    )
    return Runtime("jaffle_shop")


def _metric_query(key: str) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"as": "value", "expression": {"metric": f"metric.probe.{key}"}}],
    }


def test_scoped_aggregate_recipe_keeps_anchor_window_and_where(tmp_path: Path) -> None:
    package_dir = _package_with_metrics(
        tmp_path, {"first_90_days": FIRST_90_DAYS, "large_orders": LARGE_ORDERS}
    )
    recipes = {m.id: m.expression for m in load_package_config(str(package_dir)).metric_recipes}

    windowed = recipes["metric.probe.first_90_days"]
    assert isinstance(windowed, ScopedAggregateExpr)
    assert windowed.anchor == {"temporal_role": FIRST_ORDER_AT}
    assert windowed.window == {"unit": "day", "value": 90, "direction": "forward"}
    filtered = recipes["metric.probe.large_orders"]
    assert isinstance(filtered, ScopedAggregateExpr)
    assert filtered.where == [{"field": LARGE_ORDER, "op": "=", "value": True}]


def test_anchored_window_metric_refuses_instead_of_computing_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(
        monkeypatch, _package_with_metrics(tmp_path, {"first_90_days": FIRST_90_DAYS})
    )
    try:
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(_metric_query("first_90_days"))
    finally:
        runtime.close()
    assert excinfo.value.code == "INVALID_ANCHOR_ROLE"


def test_filtered_scoped_aggregate_metric_matches_independent_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(monkeypatch, _package_with_metrics(tmp_path, {"large_orders": LARGE_ORDERS}))
    try:
        result = runtime.query(_metric_query("large_orders"))
        connection = duckdb.connect(runtime.db_path, read_only=True)
        try:
            gold = connection.execute(
                "SELECT COUNT(DISTINCT order_id) FROM jaffle_order WHERE is_large_order"
            ).fetchone()
        finally:
            connection.close()
    finally:
        runtime.close()
    assert gold is not None and gold[0] > 0
    assert result["rows"] == [{"value": gold[0]}]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param(
            {"kind": "windowed_total", "measure": REVENUE},
            ["expression", "unknown expression kind 'windowed_total'"],
            id="unknown-kind",
        ),
        pytest.param(
            {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum", "where": []},
            ["expression", "['where']", "'aggregate'"],
            id="aggregate-with-where",
        ),
        pytest.param(
            {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum", "anchor": {}},
            ["['anchor']"],
            id="aggregate-with-anchor",
        ),
        pytest.param(
            {"measure": REVENUE, "aggregation": "sum", "anchor": {"temporal_role": "x"}},
            ["['anchor']", "'shorthand'"],
            id="bare-measure-with-anchor",
        ),
        pytest.param(
            {"kind": "metric", "metric": "metric.jaffle.x", "window": {"unit": "day"}},
            ["['window']", "'metric'"],
            id="metric-ref-with-window",
        ),
        pytest.param(
            {"kind": "scoped_aggregate", "measure": REVENUE, "wehre": []},
            ["['wehre']", "'scoped_aggregate'"],
            id="scoped-aggregate-typo",
        ),
        pytest.param(
            {"kind": "scoped_aggregate", "aggregation": "sum"},
            ["requires 'measure'"],
            id="scoped-aggregate-without-measure",
        ),
        pytest.param(
            {
                "kind": "binary",
                "op": "divide",
                "left": {"kind": "metric", "metric": "metric.jaffle.x"},
                "right": {"kind": "sliding_total", "measure": REVENUE},
            },
            ["expression.right", "unknown expression kind 'sliding_total'"],
            id="nested-unknown-kind",
        ),
        pytest.param(
            {"kind": "binary", "op": "divide", "left": {"kind": "metric", "metric": "m"}},
            ["expression", "requires 'right'"],
            id="missing-operand",
        ),
    ],
)
def test_unsupported_expression_parts_fail_the_load_naming_metric_and_part(
    tmp_path: Path, expression: dict[str, Any], expected: list[str]
) -> None:
    package_dir = _package_with_metrics(tmp_path, {"broken": expression})
    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(package_dir))
    assert excinfo.value.code == "INVALID_CONFIG"
    message = str(excinfo.value)
    assert "metric 'probe.broken'" in message
    for fragment in expected:
        assert fragment in message, message


def test_conversion_keeps_fields_the_parser_accepts() -> None:
    cumulative = _convert_recipe_expr(
        {
            "kind": "cumulative",
            "input": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
            "window_scope": "query",
        }
    )
    assert cumulative["window_scope"] == "query"
    measure = _convert_recipe_expr(
        {
            "measure": REVENUE,
            "aggregation": "sum",
            "temporal_role": "temporal_role.jaffle_order_time",
        }
    )
    assert measure["temporal_role"] == "temporal_role.jaffle_order_time"
