"""mf2sr keeps what a MetricFlow metric computes, or says why it can't.

Each test translates an inline MetricFlow project over one small DuckDB
table and queries the package, so a translation that loads but computes
something else fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from mf2sr import translate
from mf2sr.cli import main as cli_main
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.runtime import Runtime

# Orders by month: Jan 2, Feb 3, Mar 3. Delivered: 2, 2, 1. First orders: 2, 2, 2.
ORDERS_SQL = """
CREATE TABLE fct_orders AS SELECT * FROM (VALUES
  (101, TIMESTAMP '2024-01-06 09:15:00', 'delivered', TRUE, 50.0),
  (102, TIMESTAMP '2024-01-18 14:02:00', 'delivered', TRUE, 20.0),
  (103, TIMESTAMP '2024-02-01 11:45:00', 'delivered', FALSE, 100.0),
  (104, TIMESTAMP '2024-02-03 16:20:00', 'delivered', TRUE, 10.0),
  (105, TIMESTAMP '2024-02-21 08:05:00', 'returned', TRUE, 40.0),
  (106, TIMESTAMP '2024-03-02 13:30:00', 'delivered', TRUE, 200.0),
  (107, TIMESTAMP '2024-03-12 19:55:00', 'shipped', TRUE, 30.0),
  (108, TIMESTAMP '2024-03-20 07:40:00', 'placed', FALSE, 5.0)
) AS t(order_id, ordered_at, status, is_first_order, amount);
"""
ORDERS = {
    "name": "orders",
    "node_relation": {"alias": "fct_orders"},
    "defaults": {"agg_time_dimension": "ordered_at"},
    "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
    "dimensions": [
        {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
        {"name": "status", "type": "categorical"},
        {"name": "is_first_order", "type": "categorical"},
    ],
    "measures": [
        {"name": "orders", "expr": "1", "agg": "sum"},
        {"name": "revenue", "expr": "amount", "agg": "sum"},
        {"name": "average_amount", "expr": "amount", "agg": "average"},
        {"name": "statuses", "expr": "status", "agg": "count_distinct"},
    ],
}
MONTH = "temporal_role.shop_order_ordered_at__month"
GRAINS = ("week", "month", "quarter", "year")
DELIVERED = "{{ Dimension('order__status') }} IN ('delivered')"
FIRST_ORDER = "{{ Dimension('order__is_first_order') }}"


def _translate(tmp_path: Path, metrics: list[dict[str, Any]]) -> Any:
    source = tmp_path / "src"
    source.mkdir()
    (source / "semantic.yml").write_text(
        yaml.safe_dump({"semantic_models": [ORDERS], "metrics": metrics}), encoding="utf-8"
    )
    report = translate(source, tmp_path / "out", package_id="shop")
    seed = report.package_dir / "data" / "seed_shop.sql"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(ORDERS_SQL, encoding="utf-8")
    return report


def _metrics(report: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (report.package_dir / "metrics").glob("*.yml"):
        merged.update(yaml.safe_load(path.read_text())["metrics"])
    return merged


def _by_month(report: Any, *names: str, grain: str = "month") -> dict[str, list[Any]]:
    """Each metric by month (or ``grain``), one query per metric.

    Separate queries keep each check about one metric, whatever the engine
    does when metrics over one measure share a query.
    """
    engine = Runtime.from_path(str(report.package_dir))
    values: dict[str, list[Any]] = {}
    try:
        for name in names:
            rows = engine.query(
                {
                    "version": 1,
                    "select": [{"expression": {"metric": f"metric.shop.{name}"}, "as": name}],
                    "time": {
                        "temporal_role": "temporal_role.shop_order_ordered_at",
                        "grain": grain,
                    },
                    "order_by": [{"field": f"temporal_role.shop_order_ordered_at__{grain}"}],
                    "limit": 12,
                }
            )["rows"]
            values[name] = [row[name] for row in rows]
    finally:
        engine.close()
    return values


def _add_calendar(report: Any) -> None:
    """The package calendar a rolling window needs, as an author would add it."""
    package = report.package_dir
    graph = yaml.safe_load((package / "graph.yml").read_text())
    graph["graph"]["entities"]["time"] = {
        "label": "Calendar",
        "kind": "time",
        "key": ["date_day"],
        "model": "calendar",
        "allowed_as_root": False,
    }
    (package / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False))
    starts = {f"{grain}_start": {"label": f"{grain} start", "kind": "date"} for grain in GRAINS}
    calendar = {
        "id": "calendar",
        "relation": "shop_calendar",
        "calendar_id": "default",
        "entities": {"time": {}},
        "times": {
            "date_day": {
                "label": "Day",
                "column": "date_day",
                "kind": "date",
                "class": "calendar_time",
            }
        },
        "dimensions": starts,
    }
    (package / "models" / "calendar.yml").write_text(yaml.safe_dump({"model": calendar}))
    columns = ", ".join(f"date_trunc('{grain}', d)::DATE AS {grain}_start" for grain in GRAINS)
    with (package / "data" / "seed_shop.sql").open("a", encoding="utf-8") as seed:
        seed.write(
            f"CREATE TABLE shop_calendar AS SELECT d::DATE AS date_day, {columns} "
            "FROM range(DATE '2024-01-01', DATE '2024-04-01', INTERVAL 1 DAY) AS t(d);\n"
        )


def _assert_valid(report: Any) -> None:
    parse, _ = parse_config_report(PackageReference(source_path=str(report.package_dir)))
    assert parse["ok"] is True, parse["errors"]


def test_manifest_filters_translate(tmp_path: Path) -> None:
    """semantic_manifest.json holds a metric's filters as where_filters."""
    filtered = {
        "name": "first_delivered",
        "type": "simple",
        "type_params": {"measure": {"name": "orders", "filter": None}},
        "filter": {
            "where_filters": [
                {"where_sql_template": DELIVERED},
                {"where_sql_template": FIRST_ORDER},
            ]
        },
    }
    manifest = tmp_path / "semantic_manifest.json"
    manifest.write_text(json.dumps({"semantic_models": [ORDERS], "metrics": [filtered]}))

    report = translate(manifest, tmp_path / "out", package_id="shop")

    clauses = _metrics(report)["first_delivered"]["expression"]["filter"]["all"]
    assert [clause["field"] for clause in clauses] == [
        "dimension.shop_order_status",
        "dimension.shop_order_is_first_order",
    ]


def test_metric_filters_filter_the_rows(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
            {
                "name": "delivered_orders",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": DELIVERED,
            },
            {
                "name": "first_orders",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": FIRST_ORDER,
            },
            {
                "name": "first_delivered",
                "type": "simple",
                "type_params": {"measure": {"name": "orders", "filter": DELIVERED}},
                "filter": FIRST_ORDER,
            },
            {
                "name": "first_delivered_list",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": [DELIVERED, FIRST_ORDER],
            },
            {
                "name": "delivered_equals",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} = 'delivered'",
            },
            {
                "name": "undelivered",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} NOT IN ('delivered')",
            },
        ],
    )

    spec = _metrics(report)["delivered_orders"]["expression"]["filter"]
    assert spec == {
        "all": [{"field": "dimension.shop_order_status", "op": "in", "value": ["delivered"]}]
    }
    _assert_valid(report)
    values = _by_month(
        report,
        "orders",
        "delivered_orders",
        "first_orders",
        "first_delivered",
        "first_delivered_list",
        "delivered_equals",
        "undelivered",
    )
    assert values["orders"] == [2, 3, 3]
    assert values["delivered_orders"] == [2, 2, 1]
    assert values["first_orders"] == [2, 2, 2]
    assert values["first_delivered"] == [2, 1, 1]
    assert values["first_delivered_list"] == [2, 1, 1]
    assert values["delivered_equals"] == [2, 2, 1]
    assert values["undelivered"] == [1, 2]  # February and March; January had none


def test_ratio_filters_apply_to_their_side_and_the_metric_to_both(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {"name": "revenue", "type": "simple", "type_params": {"measure": "revenue"}},
            {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
            {
                "name": "delivered_revenue_per_order",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "revenue", "filter": DELIVERED},
                    "denominator": "orders",
                },
            },
            {
                "name": "first_order_delivered_revenue_per_order",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "revenue", "filter": DELIVERED},
                    "denominator": "orders",
                },
                "filter": FIRST_ORDER,
            },
        ],
    )

    values = _by_month(
        report, "delivered_revenue_per_order", "first_order_delivered_revenue_per_order"
    )
    # Delivered revenue 70, 110, 200 over orders 2, 3, 3.
    assert values["delivered_revenue_per_order"] == pytest.approx([35.0, 110 / 3, 200 / 3])
    # First orders only: delivered revenue 70, 10, 200 over first orders 2, 2, 2.
    assert values["first_order_delivered_revenue_per_order"] == pytest.approx([35.0, 5.0, 100.0])


def test_a_ratio_whose_filter_cannot_be_kept_is_skipped(tmp_path: Path) -> None:
    """Without its filters this ratio would divide orders by orders."""
    report = _translate(
        tmp_path,
        [
            {
                "name": "odd_share",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "orders", "filter": "{{ Entity('order') }} IS NOT NULL"},
                    "denominator": {"name": "orders", "filter": DELIVERED},
                },
            },
            {
                "name": "share_doubled",
                "type": "derived",
                "type_params": {"expr": "odd_share * 2", "metrics": [{"name": "odd_share"}]},
            },
        ],
    )

    assert "odd_share" not in report.metrics_emitted
    assert any(w.startswith("metric `odd_share`:") and "skipped" in w for w in report.warnings)
    # A metric built on a skipped one would fail at query time: skipped as well.
    assert "share_doubled" not in report.metrics_emitted
    assert any(
        w.startswith("metric `share_doubled`:") and "`odd_share`" in w for w in report.warnings
    )
    _assert_valid(report)


@pytest.mark.parametrize(
    ("filter_text", "reason"),
    [
        ("{{ Dimension('order__status') }} NOT BETWEEN 'a' AND 'm'", "NOT BETWEEN"),
        ("{{ Metric('orders', group_by=['order']) }} > 2", "metric predicate"),
        ("{{ Entity('order') }} IS NOT NULL", "entity"),
        ("{{ Dimension('order__nope') }} IN ('x')", "order__nope"),
        ("{{ Dimension('order__status') }} LIKE 'd%'", "could not parse"),
        (
            "{{ Dimension('order__ordered_at') }} BETWEEN '2024-01-01' AND '2024-02-01'",
            "time dimension",
        ),
    ],
)
def test_filters_the_engine_cannot_apply_are_reported(
    tmp_path: Path, filter_text: str, reason: str
) -> None:
    report = _translate(
        tmp_path,
        [
            {
                "name": "odd",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": filter_text,
            }
        ],
    )

    assert "odd" not in report.metrics_emitted
    assert "odd" not in _metrics(report)
    assert any(w.startswith("metric `odd`:") and reason in w for w in report.warnings), (
        report.warnings
    )
    _assert_valid(report)


def test_strict_cli_rejects_an_unsupported_filter(tmp_path: Path, capsys: Any) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "semantic.yml").write_text(
        yaml.safe_dump(
            {
                "semantic_models": [ORDERS],
                "metrics": [
                    {
                        "name": "odd",
                        "type": "simple",
                        "type_params": {"measure": "orders"},
                        "filter": "{{ Dimension('order__status') }} LIKE 'd%'",
                    }
                ],
            }
        )
    )

    args = ["--source", str(source), "--output", str(tmp_path / "out"), "--package-id", "shop"]
    assert cli_main([*args, "--strict"]) == 2
    output = capsys.readouterr().out
    assert "could not parse filter" in output
    assert "Metrics: 0" in output
    assert cli_main(args) == 0
    output = capsys.readouterr().out
    assert "could not parse filter" in output
    assert "Metrics: 0" in output


def test_metrics_depending_on_skipped_filtered_metrics_are_skipped(tmp_path: Path) -> None:
    unsupported = "{{ Dimension('order__status') }} LIKE 'd%'"
    report = _translate(
        tmp_path,
        [
            {
                "name": "blocked_simple",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": unsupported,
            },
            {**_cumulative("blocked_cumulative", period_agg="last"), "filter": unsupported},
            {
                "name": "simple_doubled",
                "type": "derived",
                "type_params": {
                    "expr": "blocked_simple * 2",
                    "metrics": [{"name": "blocked_simple"}],
                },
            },
            {
                "name": "cumulative_doubled",
                "type": "derived",
                "type_params": {
                    "expr": "blocked_cumulative * 2",
                    "metrics": [{"name": "blocked_cumulative"}],
                },
            },
        ],
    )

    assert not report.metrics_emitted
    assert not _metrics(report)
    for name in ("blocked_simple", "blocked_cumulative"):
        assert any(w.startswith(f"metric `{name}`:") and "skipped" in w for w in report.warnings)
    for name in ("simple_doubled", "cumulative_doubled"):
        assert any(
            w.startswith(f"metric `{name}`:") and "skipped too" in w for w in report.warnings
        )
    _assert_valid(report)


def test_between_becomes_two_bounds(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {
                "name": "mid_status",
                "type": "simple",
                "type_params": {"measure": "orders"},
                "filter": "{{ Dimension('order__status') }} BETWEEN 'd' AND 'r'",
            }
        ],
    )

    field = "dimension.shop_order_status"
    assert _metrics(report)["mid_status"]["expression"]["filter"] == {
        "all": [
            {"field": field, "op": ">=", "value": "d"},
            {"field": field, "op": "<=", "value": "r"},
        ]
    }
    # delivered and placed fall between; returned and shipped sort after 'r'
    assert _by_month(report, "mid_status")["mid_status"] == [2, 2, 2]


def _cumulative(name: str, **params: Any) -> dict[str, Any]:
    legacy = params.pop("legacy", False)
    type_params: dict[str, Any] = {"measure": "orders"}
    if legacy:
        type_params.update(params)
    elif params:
        type_params["cumulative_type_params"] = params
    return {"name": name, "type": "cumulative", "type_params": type_params}


def test_cumulative_metrics_become_the_kind_that_computes_them(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            _cumulative("running_orders", period_agg="last"),
            _cumulative("orders_mtd", grain_to_date="month", period_agg="last"),
            _cumulative("orders_mtd_legacy", grain_to_date="month", legacy=True),
            _cumulative("orders_2m", window="2 months", period_agg="last"),
            _cumulative("orders_2m_legacy", window="2 months", legacy=True),
        ],
    )

    metrics = _metrics(report)
    assert metrics["running_orders"]["kind"] == "cumulative"
    for name in ("orders_mtd", "orders_mtd_legacy"):
        assert metrics[name]["kind"] == "period_to_date"
        assert metrics[name]["period"] == "month"
    for name in ("orders_2m", "orders_2m_legacy"):
        assert metrics[name]["kind"] == "rolling"
        assert metrics[name]["window"] == {"unit": "month", "value": 2}
    assert not any("grain_to_date" in spec for spec in metrics.values())
    assert any("orders_2m, orders_2m_legacy" in w and "calendar" in w for w in report.warnings), (
        report.warnings
    )
    _add_calendar(report)
    _assert_valid(report)
    values = _by_month(
        report, "running_orders", "orders_mtd", "orders_mtd_legacy", "orders_2m", "orders_2m_legacy"
    )
    assert values["running_orders"] == [2, 5, 8]
    assert values["orders_mtd"] == values["orders_mtd_legacy"] == [2, 3, 3]
    assert values["orders_2m"] == values["orders_2m_legacy"] == [2, 5, 6]
    # By day, month-to-date restarts each month: Jan 6, 18; Feb 1, 3, 21; Mar 2, 12, 20.
    assert _by_month(report, "orders_mtd", grain="day")["orders_mtd"] == [1, 2, 1, 2, 3, 1, 2, 3]


def test_the_current_cumulative_fields_win_over_the_legacy_ones(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {
                "name": "windowed",
                "type": "cumulative",
                "type_params": {
                    "measure": "orders",
                    "window": "7 days",
                    "cumulative_type_params": {"window": "2 months", "period_agg": "last"},
                },
            },
            {
                "name": "to_date",
                "type": "cumulative",
                "type_params": {
                    "measure": "orders",
                    "grain_to_date": "year",
                    "cumulative_type_params": {"grain_to_date": "month", "period_agg": "last"},
                },
            },
        ],
    )

    metrics = _metrics(report)
    assert metrics["windowed"]["window"] == {"unit": "month", "value": 2}
    assert metrics["to_date"]["period"] == "month"


def test_manifest_windows_translate(tmp_path: Path) -> None:
    """semantic_manifest.json holds windows as objects, and fills both locations."""
    window = {"count": 2, "granularity": "month"}
    metric = {
        "name": "orders_2m",
        "type": "cumulative",
        "type_params": {
            "measure": {"name": "orders", "filter": None, "alias": None},
            "window": window,
            "grain_to_date": None,
            "cumulative_type_params": {
                "window": window,
                "grain_to_date": None,
                "period_agg": "last",
            },
        },
    }
    manifest = tmp_path / "semantic_manifest.json"
    manifest.write_text(json.dumps({"semantic_models": [ORDERS], "metrics": [metric]}))

    report = translate(manifest, tmp_path / "out", package_id="shop")

    assert _metrics(report)["orders_2m"]["window"] == {"unit": "month", "value": 2}
    (caveat,) = [w for w in report.warnings if w.startswith("metric `orders_2m`")]
    assert "whole calendar months" in caveat


def test_cumulative_metrics_keep_their_filters(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {**_cumulative("delivered_running", period_agg="last"), "filter": DELIVERED},
            {
                **_cumulative("delivered_mtd", grain_to_date="month", period_agg="last"),
                "filter": DELIVERED,
            },
            {
                **_cumulative("delivered_2m", window="2 months", period_agg="last"),
                "filter": DELIVERED,
            },
        ],
    )

    for name in ("delivered_running", "delivered_mtd", "delivered_2m"):
        assert _metrics(report)[name]["expression"]["input"]["filter"] == {
            "all": [{"field": "dimension.shop_order_status", "op": "in", "value": ["delivered"]}]
        }
    _add_calendar(report)
    values = _by_month(report, "delivered_running", "delivered_mtd", "delivered_2m")
    assert values["delivered_running"] == [2, 4, 5]
    assert values["delivered_mtd"] == [2, 2, 1]
    assert values["delivered_2m"] == [2, 4, 3]


@pytest.mark.parametrize(
    ("measure", "reason"),
    [
        ("average_amount", "aggregates with avg"),
        ("statuses", "counts distinct values"),
        ("buyers", "counts distinct customer_id values"),
    ],
)
def test_running_totals_of_measures_that_do_not_add_up_are_skipped(
    tmp_path: Path, measure: str, reason: str
) -> None:
    """The engine adds up each period's value, which a count of distinct buyers can't be."""
    orders = {
        **ORDERS,
        "entities": [
            *ORDERS["entities"],
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
        ],
        "measures": [
            *ORDERS["measures"],
            {"name": "buyers", "expr": "customer_id", "agg": "count_distinct"},
        ],
    }
    customers = {
        "name": "customers",
        "node_relation": {"alias": "dim_customers"},
        "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        "dimensions": [{"name": "country", "type": "categorical"}],
    }
    metric = {
        "name": "odd",
        "type": "cumulative",
        "type_params": {"measure": measure, "cumulative_type_params": {"grain_to_date": "month"}},
    }
    source = tmp_path / "src"
    source.mkdir()
    (source / "semantic.yml").write_text(
        yaml.safe_dump({"semantic_models": [orders, customers], "metrics": [metric]})
    )

    report = translate(source, tmp_path / "out", package_id="shop")

    assert "odd" not in report.metrics_emitted
    assert any(w.startswith("metric `odd`:") and reason in w for w in report.warnings), (
        report.warnings
    )


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"window": "7 days", "grain_to_date": "month"}, "both a window and grain_to_date"),
        ({"window": "3 hours"}, "hour windows"),
        ({"window": "a fortnight"}, "isn't `<count> <granularity>`"),
        ({"grain_to_date": "day"}, "day-to-date"),
    ],
)
def test_cumulative_metrics_the_engine_cannot_compute_are_skipped(
    tmp_path: Path, params: dict[str, Any], reason: str
) -> None:
    report = _translate(tmp_path, [_cumulative("odd", **params, period_agg="last")])

    assert "odd" not in report.metrics_emitted
    assert any(w.startswith("metric `odd`:") and reason in w for w in report.warnings), (
        report.warnings
    )


def test_period_agg_other_than_last_is_reported(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            _cumulative("first_by_default"),
            _cumulative("averaged", period_agg="average"),
            _cumulative("closing", period_agg="last"),
        ],
    )

    warned = {w.split("`")[1] for w in report.warnings if "period_agg" in w}
    assert warned == {"first_by_default", "averaged"}
    assert any("period_agg: first" in w for w in report.warnings)


def test_derived_inputs_mf2sr_cannot_express_are_reported(tmp_path: Path) -> None:
    report = _translate(
        tmp_path,
        [
            {"name": "orders", "type": "simple", "type_params": {"measure": "orders"}},
            {
                "name": "orders_growth",
                "type": "derived",
                "type_params": {
                    "expr": "orders - orders_prev",
                    "metrics": [
                        {"name": "orders"},
                        {"name": "orders", "offset_window": "1 month", "alias": "orders_prev"},
                    ],
                },
            },
            {
                "name": "orders_mtd_to_date",
                "type": "derived",
                "type_params": {
                    "expr": "orders - orders_start",
                    "metrics": [
                        {"name": "orders"},
                        {"name": "orders", "offset_to_grain": "month", "alias": "orders_start"},
                    ],
                },
            },
            {
                "name": "delivered_twice",
                "type": "derived",
                "type_params": {
                    "expr": "delivered * 2",
                    "metrics": [{"name": "orders", "filter": DELIVERED, "alias": "delivered"}],
                },
            },
            {
                "name": "first_twice",
                "type": "derived",
                "type_params": {"expr": "orders * 2", "metrics": [{"name": "orders"}]},
                "filter": FIRST_ORDER,
            },
            {
                "name": "growth_doubled",
                "type": "derived",
                "type_params": {
                    "expr": "orders_growth * 2",
                    "metrics": [{"name": "orders_growth"}],
                },
            },
        ],
    )

    # An offset input would compute orders - orders: skipped, not emitted wrong.
    for name, reason in (
        ("orders_growth", "offset_window"),
        ("orders_mtd_to_date", "offset_to_grain"),
    ):
        assert name not in report.metrics_emitted
        assert any(w.startswith(f"metric `{name}`:") and reason in w for w in report.warnings)
    # A filtered input would be computed unfiltered: skipped too.
    assert "delivered_twice" not in report.metrics_emitted
    assert any(
        w.startswith("metric `delivered_twice`:") and "filters on its inputs" in w
        for w in report.warnings
    )
    # A derived metric's own filter cannot be carried into its expression.
    assert "first_twice" not in report.metrics_emitted
    assert any(w.startswith("metric `first_twice`:") and "skipped" in w for w in report.warnings)
    # A metric built on a skipped one would fail at query time: skipped as well.
    assert "growth_doubled" not in report.metrics_emitted
    assert any(
        w.startswith("metric `growth_doubled`:") and "`orders_growth`" in w for w in report.warnings
    )
    _assert_valid(report)
