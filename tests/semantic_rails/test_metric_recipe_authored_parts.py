"""A metric keeps every part its author wrote, or refuses to load.

The loader used to rebuild an authored expression from the fields it knew and drop
the rest, so a first-week metric loaded as a bare measure and returned a lifetime
value, and a rolling metric written with ``partition_by`` lost it. Now the authored
expression goes to the expression parser unchanged, and the direct fields a metric
kind takes are all carried into it. These tests pin both halves: parts survive into
the loaded metric (and compute what they say), and a part the metric cannot keep is
a load error that names the metric and the part.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config
from semantic_rails.db import Database
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import (
    OffsetWindowExpr,
    ScopedAggregateExpr,
    parse_semantic_expression,
)
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
FIRST_ORDER_AT = "temporal_role.jaffle_customer_first_order_at"
ORDER_TIME = "temporal_role.jaffle_order_time"
LARGE_ORDER = "dimension.jaffle_order_is_large_order"
PRODUCT_TYPE = "dimension.jaffle_item_product_type"
WEEK = {"unit": "day", "value": 7}
AGGREGATE = {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"}
SCOPED = {"kind": "scoped_aggregate", "measure": REVENUE, "aggregation": "sum"}

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
# The package-relative spelling every packaged metric uses: a short measure key and a
# short dimension key on the measure's own model.
LARGE_ORDERS_SHORT_KEYS = {
    "kind": "scoped_aggregate",
    "measure": "order_count",
    "aggregation": "count_distinct",
    "where": [{"field": "is_large_order", "op": "=", "value": True}],
}


def _package_with_metrics(
    tmp_path: Path, metrics: dict[str, dict[str, Any]], *, preseed_db: bool = False
) -> Path:
    """A jaffle_shop copy with one probe metric per entry (its authored fields)."""
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=preseed_db)
    probes = {
        f"probe.{key}": {
            "as": f"metric.probe.{key}",
            "label": key,
            "description": key,
            "value_type": "number",
            "temporal_role": ORDER_TIME,
            **fields,
        }
        for key, fields in metrics.items()
    }
    path = package_dir / "metrics" / "extensions" / "probe_metrics.yml"
    path.write_text(yaml.safe_dump({"metrics": probes}), encoding="utf-8")
    return package_dir


def _recipe_package(tmp_path: Path, expressions: dict[str, dict[str, Any]]) -> Path:
    return _package_with_metrics(
        tmp_path,
        {key: {"kind": "derived", "expression": expr} for key, expr in expressions.items()},
        preseed_db=True,
    )


def _runtime(monkeypatch: pytest.MonkeyPatch, package_dir: Path) -> Runtime:
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"jaffle_shop": str(package_dir)}
    )
    return Runtime("jaffle_shop")


def _metric_query(key: str, *, by_product_type: bool = False) -> dict[str, Any]:
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": "value", "expression": {"metric": f"metric.probe.{key}"}}],
    }
    if by_product_type:
        query["time"] = {"temporal_role": ORDER_TIME, "grain": "day"}
        query["group_by"] = [PRODUCT_TYPE]
    return query


def _gold(runtime: Runtime, sql: str) -> list[tuple[Any, ...]]:
    connection = Database.connect(runtime.db_path, read_only=True)
    try:
        return connection.conn.execute(sql).fetchall()
    finally:
        connection.close()


def _load_error(tmp_path: Path, metrics: dict[str, dict[str, Any]]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as excinfo:
        load_package_config(str(_package_with_metrics(tmp_path, metrics)))
    return excinfo.value


def _probe(tmp_path: Path, key: str, fields: dict[str, Any]) -> Any:
    package = _package_with_metrics(tmp_path, {key: fields})
    recipes = {m.id: m.expression for m in load_package_config(str(package)).metric_recipes}
    return recipes[f"metric.probe.{key}"]


# --- expression: blocks -------------------------------------------------------------


def test_scoped_aggregate_recipe_keeps_anchor_window_and_where(tmp_path: Path) -> None:
    package_dir = _recipe_package(
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
    runtime = _runtime(monkeypatch, _recipe_package(tmp_path, {"first_90_days": FIRST_90_DAYS}))
    try:
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(_metric_query("first_90_days"))
        envelope = runtime.validate(_metric_query("first_90_days"))
    finally:
        runtime.close()
    # The lowering refusal itself, not an undefined anchor role.
    assert excinfo.value.code == "INVALID_ANCHOR_ROLE"
    assert excinfo.value.details["feature_status"] == "ir_contract_only"
    error = envelope["errors"][0]
    assert error["details"]["feature_status"] == "ir_contract_only"
    hints = {hint["kind"]: hint for hint in error["recovery_hints"]}
    assert "use_authored_windowed_measure" not in hints
    assert "column" in hints["use_anchor_offset_column"]["message"]
    assert "not a workaround" in hints["use_anchor_offset_column"]["message"]


def test_filtered_scoped_aggregate_metric_matches_independent_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(monkeypatch, _recipe_package(tmp_path, {"large_orders": LARGE_ORDERS}))
    try:
        result = runtime.query(_metric_query("large_orders"))
        [(large,)] = _gold(
            runtime, "SELECT COUNT(DISTINCT order_id) FROM jaffle_order WHERE is_large_order"
        )
        [(everything,)] = _gold(runtime, "SELECT COUNT(DISTINCT order_id) FROM jaffle_order")
    finally:
        runtime.close()
    assert 0 < large < everything
    assert result["rows"] == [{"value": large}]


def test_scoped_aggregate_recipe_resolves_short_measure_and_dimension_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = _recipe_package(tmp_path, {"large_orders": LARGE_ORDERS_SHORT_KEYS})
    recipe = {m.id: m.expression for m in load_package_config(str(package_dir)).metric_recipes}[
        "metric.probe.large_orders"
    ]
    assert isinstance(recipe, ScopedAggregateExpr)
    assert recipe.measure == ORDERS
    assert recipe.where == [{"field": LARGE_ORDER, "op": "=", "value": True}]

    runtime = _runtime(monkeypatch, package_dir)
    try:
        result = runtime.query(_metric_query("large_orders"))
        [(large,)] = _gold(
            runtime, "SELECT COUNT(DISTINCT order_id) FROM jaffle_order WHERE is_large_order"
        )
    finally:
        runtime.close()
    assert result["rows"] == [{"value": large}]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param(
            {"kind": "windowed_total", "measure": REVENUE},
            ["Unsupported expression kind"],
            id="unknown-kind",
        ),
        pytest.param(
            {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum", "where": []},
            ["kind='aggregate'", "['where']"],
            id="aggregate-with-where",
        ),
        pytest.param(
            {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum", "anchor": {}},
            ["kind='aggregate'", "['anchor']"],
            id="aggregate-with-anchor",
        ),
        pytest.param(
            {"measure": REVENUE, "aggregation": "sum", "anchor": {"temporal_role": "x"}},
            ["kind='measure'", "['anchor']"],
            id="bare-measure-with-anchor",
        ),
        pytest.param(
            {"kind": "metric", "metric": "metric.jaffle.x", "window": {"unit": "day"}},
            ["kind='metric'", "['window']"],
            id="metric-ref-with-window",
        ),
        pytest.param(
            {"kind": "scoped_aggregate", "measure": REVENUE, "wehre": []},
            ["kind='scoped_aggregate'", "['wehre']"],
            id="scoped-aggregate-typo",
        ),
        pytest.param(
            {"kind": "scoped_aggregate", "aggregation": "sum"},
            ["scoped_aggregate expressions require 'measure'"],
            id="scoped-aggregate-without-measure",
        ),
        pytest.param(
            {
                "kind": "binary",
                "op": "divide",
                "left": {"kind": "metric", "metric": "metric.jaffle.x"},
                "right": {"kind": "sliding_total", "measure": REVENUE},
            },
            ["Unsupported expression kind"],
            id="nested-unknown-kind",
        ),
        pytest.param(
            {"kind": "binary", "op": "divide", "left": {"kind": "metric", "metric": "m"}},
            ["expression must be an object"],
            id="missing-operand",
        ),
        pytest.param(
            {
                "kind": "rolling",
                "input": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
                "window": 7,
            },
            ["Rolling expressions require window as an object"],
            id="scalar-window",
        ),
        pytest.param(
            {
                "kind": "prior_period",
                "input": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
                "offset": -1,
            },
            ["Prior-period (IR shape) expressions require offset as an object"],
            id="scalar-offset",
        ),
        pytest.param(
            {
                **SCOPED,
                "anchor": "temporal_role.jaffle_customer_first_order_at",
                "window": {"unit": "day", "value": 90},
            },
            ["scoped_aggregate.anchor must be a non-empty object"],
            id="scoped-aggregate-string-anchor",
        ),
        pytest.param(
            {**SCOPED, "anchor": {"temporal_role": "x"}, "window": 90},
            ["scoped_aggregate.window must be a non-empty object"],
            id="scoped-aggregate-scalar-window",
        ),
        pytest.param(
            {**SCOPED, "anchor": {}, "window": {}},
            ["scoped_aggregate.anchor must be a non-empty object"],
            id="scoped-aggregate-empty-anchor-and-window",
        ),
    ],
)
def test_unsupported_expression_parts_fail_the_load_naming_metric_and_part(
    tmp_path: Path, expression: dict[str, Any], expected: list[str]
) -> None:
    error = _load_error(tmp_path, {"broken": {"kind": "derived", "expression": expression}})
    assert error.code.startswith("INVALID_EXPRESSION"), error.code
    message = str(error)
    assert "metric 'probe.broken'" in message
    for fragment in expected:
        assert fragment in message, message


def test_prior_period_shorthand_loads_as_the_parser_reads_it(tmp_path: Path) -> None:
    expression = _probe(
        tmp_path,
        "last_month",
        {
            "kind": "derived",
            "expression": {
                "kind": "prior_period",
                "measure": REVENUE,
                "offset": -1,
                "grain": "month",
            },
        },
    )
    assert isinstance(expression, OffsetWindowExpr)
    assert (expression.kind, expression.unit, expression.value) == ("prior_period", "month", 1)


def test_prior_period_shorthand_resolves_a_short_measure_key_and_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metric = {
        "kind": "derived",
        "expression": {
            "kind": "prior_period",
            "measure": "revenue_usd",
            "offset": -1,
            "grain": "month",
        },
    }
    this_month = {"kind": "aggregate", "measure": "revenue_usd", "aggregation": "sum"}
    package_dir = _package_with_metrics(
        tmp_path, {"last_month": metric, "this_month": this_month}, preseed_db=True
    )
    recipes = {m.id: m.expression for m in load_package_config(str(package_dir)).metric_recipes}
    assert recipes["metric.probe.last_month"].input.measure == REVENUE  # type: ignore[union-attr]

    runtime = _runtime(monkeypatch, package_dir)
    try:
        query = {
            "version": 1,
            "select": [
                {"as": "prior", "expression": {"metric": "metric.probe.last_month"}},
                {"as": "current", "expression": {"metric": "metric.probe.this_month"}},
            ],
            "time": {"temporal_role": ORDER_TIME, "grain": "month"},
        }
        rows = sorted(
            runtime.query(query)["rows"], key=lambda row: str(row[f"{ORDER_TIME}__month"])
        )
    finally:
        runtime.close()
    assert len(rows) > 2
    assert rows[0]["prior"] is None
    assert [row["prior"] for row in rows[1:]] == pytest.approx(
        [row["current"] for row in rows[:-1]]
    )


def test_a_window_value_that_is_not_a_number_fails_the_load_naming_the_metric(
    tmp_path: Path,
) -> None:
    error = _load_error(
        tmp_path,
        {
            "broken": {
                "kind": "rolling",
                "measure": "item_revenue_usd",
                "aggregation": "sum",
                "window": {"unit": "day", "value": "7d"},
            }
        },
    )
    assert error.code == "INVALID_EXPRESSION_AST"
    assert "metric 'probe.broken'" in str(error)


def test_the_parser_refuses_a_rolling_window_value_that_is_not_a_number() -> None:
    expression = {
        "kind": "rolling",
        "input": {"measure": "measure.probe.item_revenue_usd", "aggregation": "sum"},
        "window": {"unit": "day", "value": "7d"},
    }
    with pytest.raises(SemanticLayerError) as excinfo:
        parse_semantic_expression(expression, context="config")
    assert excinfo.value.code == "INVALID_EXPRESSION_AST"


# --- direct fields ------------------------------------------------------------------


def _by_product_type_gold(runtime: Runtime, window_sql: str) -> dict[tuple[str, date], float]:
    """Independent SQL: revenue per product type and day, then the window over it."""
    rows = _gold(
        runtime,
        f"""
        WITH daily AS (
            SELECT i.product_type AS product_type,
                   CAST(o.ordered_at AS DATE) AS day,
                   SUM(i.item_revenue_cents) / 100.0 AS revenue
            FROM jaffle_item i JOIN jaffle_order o ON o.order_id = i.order_id
            GROUP BY 1, 2
        )
        SELECT product_type, day, {window_sql} FROM daily
        """,
    )
    return {(product_type, day): float(value) for product_type, day, value in rows}


def _by_product_type_result(runtime: Runtime, key: str) -> dict[tuple[str, date], float]:
    out: dict[tuple[str, date], float] = {}
    for row in runtime.query(_metric_query(key, by_product_type=True))["rows"]:
        day = row[f"{ORDER_TIME}__day"]
        day = datetime.fromisoformat(day).date()
        out[(row[PRODUCT_TYPE], day)] = float(row["value"])
    return out


PARTITIONED = [
    pytest.param(
        {"kind": "rolling", "window": WEEK, "partition_by": [PRODUCT_TYPE]},
        "SUM(revenue) OVER (PARTITION BY product_type ORDER BY day "
        "ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)",
        id="rolling-partition_by",
    ),
    pytest.param(
        {"kind": "period_to_date", "period": "month", "partition_by": [PRODUCT_TYPE]},
        "SUM(revenue) OVER (PARTITION BY product_type, DATE_TRUNC('month', day) ORDER BY day)",
        id="period_to_date-partition_by",
    ),
    pytest.param(
        {"kind": "cumulative", "partition_by": [PRODUCT_TYPE]},
        "SUM(revenue) OVER (PARTITION BY product_type ORDER BY day)",
        id="cumulative-partition_by",
    ),
]


@pytest.mark.parametrize(("fields", "window_sql"), PARTITIONED)
def test_direct_field_partition_by_must_be_grouped_by_and_the_window_runs_per_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any], window_sql: str
) -> None:
    metric = {"measure": "item_revenue_usd", "aggregation": "sum", **fields}
    runtime = _runtime(
        monkeypatch, _package_with_metrics(tmp_path, {"windowed": metric}, preseed_db=True)
    )
    try:
        gold = _by_product_type_gold(runtime, window_sql)
        result = _by_product_type_result(runtime, "windowed")
        with pytest.raises(SemanticLayerError) as excinfo:
            # A partition the query does not group by is refused, never answered globally.
            runtime.query(
                {**_metric_query("windowed"), "time": {"temporal_role": ORDER_TIME, "grain": "day"}}
            )
    finally:
        runtime.close()
    assert excinfo.value.code == "INVALID_QUERY"
    assert excinfo.value.details == {"partition_by_missing_from_group_by": [PRODUCT_TYPE]}
    assert "metric.probe.windowed" in str(excinfo.value)
    assert len(gold) > 100
    assert result.keys() == gold.keys()
    assert all(result[key] == pytest.approx(gold[key]) for key in gold)


@pytest.mark.parametrize(
    ("fields", "loaded"),
    [
        pytest.param(
            {"kind": "cumulative", "window_scope": "query_period"},
            {"kind": "cumulative", "window_scope": "query_period"},
            id="cumulative-window_scope",
        ),
        pytest.param(
            {"kind": "rolling", "window": WEEK, "partition_by": [PRODUCT_TYPE]},
            {"kind": "rolling", "partition_by": [PRODUCT_TYPE], "unit": "day", "value": 7},
            id="rolling-partition_by",
        ),
        pytest.param(
            {"kind": "period_to_date", "period": "month", "partition_by": [PRODUCT_TYPE]},
            {"kind": "period_to_date", "partition_by": [PRODUCT_TYPE], "period": "month"},
            id="period_to_date-partition_by",
        ),
        pytest.param(
            {"kind": "cumulative", "partition_by": [PRODUCT_TYPE]},
            {"kind": "cumulative", "partition_by": [PRODUCT_TYPE]},
            id="cumulative-partition_by",
        ),
        pytest.param(
            {"kind": "prior_period", "offset": {"unit": "day", "value": 7}},
            {"kind": "prior_period", "unit": "day", "value": 7},
            id="prior_period-offset",
        ),
    ],
)
def test_direct_fields_reach_the_loaded_expression(
    tmp_path: Path, fields: dict[str, Any], loaded: dict[str, Any]
) -> None:
    expression = _probe(
        tmp_path, "windowed", {"measure": "item_revenue_usd", "aggregation": "sum", **fields}
    )
    assert isinstance(expression, OffsetWindowExpr)
    assert {name: getattr(expression, name) for name in loaded} == loaded


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        pytest.param({"kind": "cumulative", "window": WEEK}, "window", id="cumulative-window"),
        pytest.param({"kind": "cumulative", "order_by": "x"}, "order_by", id="cumulative-order_by"),
        pytest.param(
            {"kind": "prior_period", "offset": WEEK, "partition_by": [PRODUCT_TYPE]},
            "partition_by",
            id="prior_period-partition_by",
        ),
        pytest.param(
            {"kind": "period_to_date", "period": "month", "window": WEEK},
            "window",
            id="period_to_date-window",
        ),
        pytest.param(
            {"kind": "rolling", "window": WEEK, "period": "month"}, "period", id="rolling-period"
        ),
        pytest.param(
            {"kind": "aggregate", "partition_by": [PRODUCT_TYPE]},
            "partition_by",
            id="aggregate-partition_by",
        ),
        pytest.param(
            {"kind": "semi_additive", "offset": WEEK}, "offset", id="semi_additive-offset"
        ),
        pytest.param(
            {
                "kind": "ratio",
                "numerator": "order_count",
                "denominator": "order_count",
                "window": WEEK,
            },
            "window",
            id="ratio-window",
        ),
    ],
)
def test_direct_field_its_kind_cannot_keep_fails_the_load_naming_metric_and_field(
    tmp_path: Path, fields: dict[str, Any], field: str
) -> None:
    # A ratio takes numerator/denominator; every other kind here reads one measure.
    measure = (
        {} if fields["kind"] == "ratio" else {"measure": "item_revenue_usd", "aggregation": "sum"}
    )
    error = _load_error(tmp_path, {"broken": {**measure, **fields}})
    assert error.code == "INVALID_EXPRESSION_KEY"
    assert "metric 'probe.broken'" in str(error)
    assert f"['{field}']" in str(error)
    assert error.details["unknown_keys"] == [field]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"kind": "rolling", "window": 7}, "window as an object", id="rolling-window"),
        pytest.param(
            {"kind": "prior_period", "offset": -1}, "offset as an object", id="prior_period-offset"
        ),
    ],
)
def test_direct_field_scalar_shapes_get_the_parsers_error(
    tmp_path: Path, fields: dict[str, Any], expected: str
) -> None:
    error = _load_error(
        tmp_path, {"broken": {"measure": "item_revenue_usd", "aggregation": "sum", **fields}}
    )
    assert error.code == "INVALID_EXPRESSION_AST"
    assert "metric 'probe.broken'" in str(error)
    assert expected in str(error)


@pytest.mark.parametrize(
    "metric",
    [
        pytest.param(
            {"kind": "aggregate", "measure": "item_revenue_usd", "aggregation": "sum", "window": 7},
            id="aggregate-window-int",
        ),
        pytest.param(
            {
                "kind": "aggregate",
                "measure": "item_revenue_usd",
                "aggregation": "sum",
                "window": "7d",
            },
            id="aggregate-window-string",
        ),
        pytest.param(
            {
                "kind": "aggregate",
                "measure": "item_revenue_usd",
                "aggregation": "sum",
                "window": True,
            },
            id="aggregate-window-bool",
        ),
        pytest.param(
            {"kind": "semi_additive", "measure": "item_revenue_usd", "window": 7},
            id="semi_additive-window-int",
        ),
        pytest.param(
            {"kind": "derived", "expression": {**AGGREGATE, "parameters": 5}},
            id="expression-parameters",
        ),
        pytest.param(
            {"kind": "derived", "expression": {**SCOPED, "where": ["x"]}},
            id="expression-scoped-where",
        ),
    ],
)
def test_malformed_scalar_parts_fail_the_load_naming_the_metric(
    tmp_path: Path, metric: dict[str, Any]
) -> None:
    error = _load_error(tmp_path, {"broken": metric})
    assert error.code == "INVALID_EXPRESSION_AST"
    assert "metric 'probe.broken'" in str(error)


def test_direct_aggregate_window_is_refused_when_queried_never_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metric = {"kind": "aggregate", "measure": "item_revenue_usd", "aggregation": "sum"}
    package_dir = _package_with_metrics(
        tmp_path, {"windowed": {**metric, "window": WEEK}}, preseed_db=True
    )
    expression = {m.id: m.expression for m in load_package_config(str(package_dir)).metric_recipes}[
        "metric.probe.windowed"
    ]
    assert expression.window == WEEK  # type: ignore[union-attr]
    runtime = _runtime(monkeypatch, package_dir)
    try:
        with pytest.raises(SemanticLayerError) as excinfo:
            runtime.query(_metric_query("windowed"))
    finally:
        runtime.close()
    assert excinfo.value.code == "INVALID_EXPRESSION_AST"
    assert excinfo.value.details["window"] == WEEK


# --- an expression: block and direct fields never mix -------------------------------

ROLLING_EXPRESSION = {
    "kind": "rolling",
    "input": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
    "window": WEEK,
}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("measure", "item_revenue_usd", id="measure"),
        pytest.param("aggregation", "sum", id="aggregation"),
        pytest.param("numerator", "order_count", id="numerator"),
        pytest.param("denominator", "order_count", id="denominator"),
        pytest.param("window", WEEK, id="window"),
        pytest.param("window_scope", "query_period", id="window_scope"),
        pytest.param("offset", WEEK, id="offset"),
        pytest.param("period", "month", id="period"),
        pytest.param("partition_by", [PRODUCT_TYPE], id="partition_by"),
        pytest.param("order_by", "day", id="order_by"),
    ],
)
def test_expression_block_beside_a_direct_field_refuses_naming_metric_and_field(
    tmp_path: Path, field: str, value: Any
) -> None:
    error = _load_error(
        tmp_path,
        {"mixed": {"kind": "rolling", "expression": ROLLING_EXPRESSION, field: value}},
    )
    assert error.code == "INVALID_CONFIG"
    assert "metric 'probe.mixed'" in str(error)
    assert field in str(error)
    assert error.details["conflicting_fields"] == [field]


def test_expression_block_beside_several_direct_fields_names_them_all(tmp_path: Path) -> None:
    error = _load_error(
        tmp_path,
        {
            "mixed": {
                "kind": "derived",
                "expression": {"kind": "aggregate", "measure": REVENUE, "aggregation": "sum"},
                "window": WEEK,
                "partition_by": [PRODUCT_TYPE],
            }
        },
    )
    assert error.code == "INVALID_CONFIG"
    assert error.details["conflicting_fields"] == ["partition_by", "window"]


def test_expression_block_with_only_a_kind_beside_it_still_loads(tmp_path: Path) -> None:
    expression = _probe(tmp_path, "rolling", {"kind": "rolling", "expression": ROLLING_EXPRESSION})
    assert isinstance(expression, OffsetWindowExpr)
    assert (expression.unit, expression.value) == ("day", 7)


def test_the_central_translation_refuses_the_mix_whoever_calls_it() -> None:
    with pytest.raises(SemanticLayerError) as excinfo:
        config_module._translate_metric_direct_fields(
            {"expression": ROLLING_EXPRESSION, "window": WEEK},
            resolve=lambda ref: ref,
            context="pkg: metric 'mixed'",
        )
    assert excinfo.value.code == "INVALID_CONFIG"
    assert excinfo.value.details["conflicting_fields"] == ["window"]


# --- partition_by names dimensions of the package -----------------------------------

MEASURE_INPUT = {"kind": "aggregate", "measure": "item_revenue_usd", "aggregation": "sum"}
NOT_A_DIMENSION = "dimension.jaffle_item_not_a_column"
INPUT_NOT_ONE_MEASURE = {
    "kind": "binary",
    "op": "divide",
    "left": {"kind": "metric", "metric": "metric.jaffle.revenue_usd"},
    "right": {"kind": "metric", "metric": "metric.jaffle.order_count"},
}


def _authored_window(kind: str, partition_by: Any, **extra: Any) -> dict[str, Any]:
    expression = {"kind": kind, "input": MEASURE_INPUT, "partition_by": partition_by, **extra}
    return {"kind": "derived", "expression": expression}


def test_partition_by_short_key_resolves_to_the_dimension_id(tmp_path: Path) -> None:
    direct = _probe(
        tmp_path,
        "direct",
        {
            "kind": "rolling",
            "measure": "item_revenue_usd",
            "window": WEEK,
            "partition_by": ["product_type"],
        },
    )
    authored = _probe(
        tmp_path, "authored", _authored_window("rolling", ["product_type"], window=WEEK)
    )
    assert direct.partition_by == [PRODUCT_TYPE]  # type: ignore[union-attr]
    assert authored.partition_by == [PRODUCT_TYPE]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("metric", "unknown"),
    [
        pytest.param(
            {
                "kind": "rolling",
                "measure": "item_revenue_usd",
                "window": WEEK,
                "partition_by": ["prodcut_type"],
            },
            "prodcut_type",
            id="direct-typo",
        ),
        pytest.param(
            {
                "kind": "cumulative",
                "measure": "item_revenue_usd",
                "partition_by": [NOT_A_DIMENSION],
            },
            NOT_A_DIMENSION,
            id="direct-unknown-full-id",
        ),
        pytest.param(
            {"kind": "cumulative", "measure": "item_revenue_usd", "partition_by": ["order_count"]},
            "order_count",
            id="direct-measure-is-not-a-dimension",
        ),
        pytest.param(
            {
                "kind": "period_to_date",
                "measure": "item_revenue_usd",
                "period": "month",
                "partition_by": ["is_large_order"],
            },
            "is_large_order",
            id="direct-key-of-another-model",
        ),
        pytest.param(
            {"kind": "cumulative", "measure": "item_revenue_usd", "partition_by": "product_type"},
            "product_type",
            id="direct-bare-string",
        ),
        pytest.param(
            {"kind": "cumulative", "measure": "item_revenue_usd", "partition_by": 0},
            "0",
            id="direct-zero",
        ),
        pytest.param(
            {"kind": "cumulative", "measure": "item_revenue_usd", "partition_by": False},
            "False",
            id="direct-false",
        ),
        pytest.param(_authored_window("rolling", 0, window=WEEK), "0", id="authored-zero"),
        pytest.param(
            _authored_window("rolling", ["prodcut_type"], window=WEEK),
            "prodcut_type",
            id="authored-typo",
        ),
        pytest.param(
            {
                "kind": "derived",
                "expression": {
                    "kind": "cumulative",
                    "input": INPUT_NOT_ONE_MEASURE,
                    "partition_by": ["product_type"],
                },
            },
            "product_type",
            id="authored-short-key-without-one-measure",
        ),
        pytest.param(
            {
                "kind": "derived",
                "expression": {
                    "kind": "binary",
                    "op": "add",
                    "left": {"kind": "metric", "metric": "metric.jaffle.order_count"},
                    "right": _authored_window("period_to_date", [NOT_A_DIMENSION], period="month")[
                        "expression"
                    ],
                },
            },
            NOT_A_DIMENSION,
            id="authored-nested-in-another-node",
        ),
    ],
)
def test_partition_by_entry_that_is_not_a_dimension_refuses_at_load(
    tmp_path: Path, metric: dict[str, Any], unknown: str
) -> None:
    error = _load_error(tmp_path, {"partitioned": metric})
    assert error.code == "INVALID_CONFIG"
    assert "metric 'probe.partitioned'" in str(error)
    assert "partition_by" in str(error)
    assert error.details["unknown_partition_by"] == [unknown]


def test_the_central_translation_refuses_partition_by_when_no_dimension_is_known() -> None:
    # A caller that supplies no dimensions gets a refusal, not a pass-through.
    with pytest.raises(SemanticLayerError) as excinfo:
        config_module._translate_metric_direct_fields(
            {"kind": "cumulative", "measure": "revenue_usd", "partition_by": [PRODUCT_TYPE]},
            resolve=lambda ref: ref,
            context="pkg: metric 'partitioned'",
        )
    assert excinfo.value.details["unknown_partition_by"] == [PRODUCT_TYPE]
