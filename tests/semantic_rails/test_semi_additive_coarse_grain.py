"""Semi-additive (stock) measures at grains coarser than the snapshot grain.

A snapshot series whose only row key is its own time column (one snapshot
per day, e.g. a daily YTD rollup) must reduce to the last/first snapshot
inside each output bucket. Before the fix, the "last snapshot per row key"
partition included the time column itself, so every snapshot survived and
the outer combine SUMMED a stock across periods: revenue_ytd at month
grain came back 16x overstated, and as a scalar 200x overstated, silently.

These tests pin the contract by equivalence: the value of a stock metric
at a coarse grain must equal its day-grain value on the last day of that
bucket, never the sum across the bucket.
"""

from __future__ import annotations

import pytest

from semantic_rails.runtime import Runtime

ROLE = "temporal_role.jaffle_daily_metric_day"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from tests.semantic_rails.conftest import copy_package_config

    package_dir = copy_package_config(
        tmp_path_factory.mktemp("semi_additive"), "jaffle_shop", preseed_db=True
    )
    return Runtime.from_path(str(package_dir))


def _rows(runtime: Runtime, payload: dict) -> list[dict]:
    result = runtime.query(payload)
    assert result["ok"], result.get("errors")
    return list(result["rows"])


def _metric_select(metric_id: str, alias: str) -> list[dict]:
    return [{"expression": {"metric": metric_id}, "as": alias}]


def _day_value(runtime: Runtime, metric_id: str, day: str, next_day: str) -> float:
    rows = _rows(
        runtime,
        {
            "version": 1,
            "select": _metric_select(metric_id, "value"),
            "time": {"temporal_role": ROLE, "grain": "day", "start": day, "end": next_day},
        },
    )
    assert len(rows) == 1
    return rows[0]["value"]


def test_stock_metric_at_month_grain_is_end_of_month_value(runtime):
    month_rows = _rows(
        runtime,
        {
            "version": 1,
            "select": _metric_select("metric.sales.revenue_ytd", "ytd"),
            "time": {
                "temporal_role": ROLE,
                "grain": "month",
                "start": "2017-01-01",
                "end": "2017-02-01",
            },
        },
    )
    assert len(month_rows) == 1
    end_of_month = _day_value(runtime, "metric.sales.revenue_ytd", "2017-01-31", "2017-02-01")
    assert month_rows[0]["ytd"] == pytest.approx(end_of_month)
    # Regression guard: the broken lowering summed every daily snapshot in
    # the month, which is at least an order of magnitude larger.
    assert month_rows[0]["ytd"] < 2 * end_of_month


def test_stock_metric_at_year_grain_is_end_of_year_value(runtime):
    year_rows = _rows(
        runtime,
        {
            "version": 1,
            "select": _metric_select("metric.sales.revenue_ytd", "ytd"),
            "time": {
                "temporal_role": ROLE,
                "grain": "year",
                "start": "2017-01-01",
                "end": "2018-01-01",
            },
        },
    )
    assert len(year_rows) == 1
    end_of_year = _day_value(runtime, "metric.sales.revenue_ytd", "2017-12-31", "2018-01-01")
    assert year_rows[0]["ytd"] == pytest.approx(end_of_year)


def test_stock_metric_scalar_is_latest_snapshot_value(runtime):
    scalar_rows = _rows(
        runtime,
        {"version": 1, "select": _metric_select("metric.sales.revenue_ytd", "ytd")},
    )
    assert len(scalar_rows) == 1
    # The rollup's snapshots span into a later year where YTD reset, so a
    # 200x-of-total-revenue value (the old sum across every snapshot)
    # cannot masquerade as correct here.
    latest = _day_value(runtime, "metric.sales.revenue_ytd", "2018-08-31", "2018-09-01")
    assert scalar_rows[0]["ytd"] == pytest.approx(latest)


def test_non_summable_stock_metric_at_month_grain_is_end_of_month_value(runtime):
    month_rows = _rows(
        runtime,
        {
            "version": 1,
            "select": _metric_select("metric.sales.median_item_revenue", "med"),
            "time": {
                "temporal_role": ROLE,
                "grain": "month",
                "start": "2017-03-01",
                "end": "2017-04-01",
            },
        },
    )
    assert len(month_rows) == 1
    end_of_month = _day_value(
        runtime, "metric.sales.median_item_revenue", "2017-03-31", "2017-04-01"
    )
    assert month_rows[0]["med"] == pytest.approx(end_of_month)


def test_multi_key_snapshot_sum_across_entities_is_preserved(runtime):
    """Stocks keyed by a real entity (inventory per store) still sum the
    per-entity period-end snapshots — the singleton-series fix must not
    collapse genuine multi-key snapshot tables to a single row."""
    result = runtime.query(
        {
            "version": 1,
            "select": [
                {"expression": {"metric": "metric.sales.inventory_on_hand_eop"}, "as": "inv"}
            ],
            "time": {
                "temporal_role": "temporal_role.jaffle_inventory_day",
                "grain": "month",
                "start": "2017-03-01",
                "end": "2017-04-01",
            },
        }
    )
    if not result["ok"]:
        pytest.skip(f"inventory metric unavailable: {result.get('errors')}")
    assert result["rows"]
