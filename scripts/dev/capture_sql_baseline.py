"""Capture SQL + row-level baseline for every governed jaffle metric.

Used to verify that SQL-quality refactors preserve outputs.

Usage:
    UV_INDEX_URL=https://pypi.org/simple uv run python scripts/capture_sql_baseline.py /tmp/sql_baseline.json
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from semantic_rails import config as config_module
from semantic_rails.config import resolve_repo_path
from semantic_rails.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRATCH_ROOT = REPO_ROOT / ".tmp-baseline"


# Mirror the metric payloads from tests/semantic_rails/test_real_duckdb_data.py
# Each entry is (alias, payload). alias matches the metric id segment for clarity.
PAYLOADS: list[dict[str, Any]] = [
    {
        "metric_id": "metric.sales.aov_usd",
        "payload": {
            "select": [
                {"expression": {"metric": "metric.sales.aov_usd"}, "as": "aov_usd"},
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "order_by": [{"field": "aov_usd", "direction": "DESC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.average_customer_lifetime_value",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.average_customer_lifetime_value"},
                    "as": "avg_clv",
                },
            ],
            "group_by": ["dimension.jaffle_customer_type"],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.food_revenue_share",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.food_revenue_share"},
                    "as": "food_revenue_share",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.drink_revenue_share",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.drink_revenue_share"},
                    "as": "drink_revenue_share",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.customer_count",
        "payload": {
            "select": [
                {"expression": {"metric": "metric.sales.customer_count"}, "as": "customer_count"},
            ],
            "group_by": ["dimension.jaffle_customer_type"],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.cumulative_revenue",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.cumulative_revenue"},
                    "as": "cumulative_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.rolling_28d_revenue",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.rolling_28d_revenue"},
                    "as": "rolling_28d_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "order_by": [
                {"field": "temporal_role.jaffle_daily_metric_day__day", "direction": "ASC"}
            ],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.revenue_qtd",
        "payload": {
            "select": [
                {"expression": {"metric": "metric.sales.revenue_qtd"}, "as": "revenue_qtd"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "order_by": [
                {"field": "temporal_role.jaffle_daily_metric_day__day", "direction": "ASC"}
            ],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.revenue_ytd",
        "payload": {
            "select": [
                {"expression": {"metric": "metric.sales.revenue_ytd"}, "as": "revenue_ytd"},
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "order_by": [
                {"field": "temporal_role.jaffle_daily_metric_day__day", "direction": "ASC"}
            ],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.median_item_revenue",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.median_item_revenue"},
                    "as": "median_item_revenue",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_daily_metric_day", "grain": "day"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.month_over_month_revenue_growth",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.month_over_month_revenue_growth"},
                    "as": "month_over_month_revenue_growth",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "order_by": [{"field": "temporal_role.jaffle_order_time__month", "direction": "ASC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.gross_margin_pct",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.gross_margin_pct"},
                    "as": "gross_margin_pct",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.rolling_7d_revenue_direct",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.rolling_7d_revenue_direct"},
                    "as": "rolling_7d_revenue_direct",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            "filters": [{"column": "jaffle_order.ordered_at", "op": "<", "value": "2017-01-08"}],
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.prior_week_revenue_direct",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.prior_week_revenue_direct"},
                    "as": "prior_week_revenue_direct",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            "filters": [{"column": "jaffle_order.ordered_at", "op": "<", "value": "2017-01-15"}],
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.revenue_mtd_direct",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.revenue_mtd_direct"},
                    "as": "revenue_mtd_direct",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "day"},
            "filters": [{"column": "jaffle_order.ordered_at", "op": "<", "value": "2017-01-08"}],
            "order_by": [{"field": "temporal_role.jaffle_order_time__day", "direction": "ASC"}],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.repeat_customer_orders",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.repeat_customer_orders"},
                    "as": "repeat_customer_orders",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.high_value_customer_orders",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.high_value_customer_orders"},
                    "as": "high_value_customer_orders",
                },
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.session_to_order_conversion_rate_7d",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.session_to_order_conversion_rate_7d"},
                    "as": "conversion_rate",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.session_to_order_conversion_rate_7d_same_store",
        "payload": {
            "select": [
                {
                    "expression": {
                        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                    },
                    "as": "same_store_conversion_rate",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.adoption.signup_to_send_conversion_rate_28d",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.adoption.signup_to_send_conversion_rate_28d"},
                    "as": "signup_to_send_conversion_rate_28d",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "day"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.inventory_on_hand_eop",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.inventory_on_hand_eop"},
                    "as": "inventory_on_hand_eop",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "order_by": [
                {"field": "temporal_role.jaffle_inventory_day__month", "direction": "ASC"}
            ],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.inventory_on_hand_sop",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.inventory_on_hand_sop"},
                    "as": "inventory_on_hand_sop",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "order_by": [
                {"field": "temporal_role.jaffle_inventory_day__month", "direction": "ASC"}
            ],
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.active_menu_count_eop",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.active_menu_count_eop"},
                    "as": "active_menu_count_eop",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "limit": 10000,
        },
    },
    {
        "metric_id": "metric.sales.open_store_count_eop",
        "payload": {
            "select": [
                {
                    "expression": {"metric": "metric.sales.open_store_count_eop"},
                    "as": "open_store_count_eop",
                },
            ],
            "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
            "limit": 10000,
        },
    },
]


def _setup_runtime() -> Runtime:
    """Create a one-shot Runtime against a fresh, seeded DuckDB copy of jaffle_shop."""
    package_id = "jaffle_shop"
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = SCRATCH_ROOT / package_id / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)

    package_dir = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    seeded_db = Path(resolve_repo_path("data/jaffle_shop.duckdb"))

    target = run_dir / package_id
    shutil.copytree(package_dir, target)
    package_path = target / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    default_db = run_dir / f"{package_id}.duckdb"
    package["default_db"] = str(default_db)
    raw["package"] = package
    package_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    if seeded_db.exists():
        shutil.copy2(seeded_db, default_db)

    config_module.list_package_paths = lambda: {package_id: str(target)}  # type: ignore[assignment]
    return Runtime(package_id)


def _normalize_rows(rows: Any) -> list[dict[str, Any]]:
    """Coerce row values to JSON-stable forms (datetimes -> isoformat)."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        norm: dict[str, Any] = {}
        for k, v in row.items():
            try:
                json.dumps(v)
                norm[k] = v
            except TypeError:
                norm[k] = str(v)
        out.append(norm)
    return out


def main(out_path: str) -> int:
    runtime = _setup_runtime()
    baseline: dict[str, Any] = {}
    failures: list[str] = []
    for entry in PAYLOADS:
        metric_id = entry["metric_id"]
        payload = entry["payload"]
        slot: dict[str, Any] = {}
        try:
            compiled = runtime.compile(payload)
            slot["compile"] = {
                "sql": compiled.get("rendered_sql") or compiled.get("sql"),
                "warnings": compiled.get("warnings", []),
            }
        except Exception as exc:
            slot["compile_error"] = f"{type(exc).__name__}: {exc}"
            failures.append(metric_id)

        try:
            queried = runtime.query(payload)
            rows = _normalize_rows(queried.get("rows"))
            slot["query"] = {
                "rows": rows,
                "row_count": len(rows),
                "columns": queried.get("columns"),
            }
        except Exception as exc:
            slot["query_error"] = f"{type(exc).__name__}: {exc}"
            failures.append(metric_id)

        baseline[metric_id] = slot

    Path(out_path).write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
    captured = sum(1 for v in baseline.values() if "query" in v)
    total = len(PAYLOADS)
    print(f"Captured {captured}/{total} metrics. Output: {out_path}")
    if failures:
        print(f"WARN: {len(set(failures))} metric(s) had errors: {sorted(set(failures))}")
        return 1
    return 0


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sql_baseline.json"
    raise SystemExit(main(out))
