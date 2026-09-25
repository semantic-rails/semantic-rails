from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPARISON_ROOT = REPO_ROOT / "comparisons" / "semantic_layers"
SHARED_ROOT = COMPARISON_ROOT / "shared"
RESULTS_ROOT = SHARED_ROOT / "results"
QUESTIONS_PATH = SHARED_ROOT / "questions.yml"
COMPARISON_DATA_PATH = SHARED_ROOT / "comparison_data.json"
CAPABILITY_MATRIX_PATH = SHARED_ROOT / "capability_matrix.json"
VALIDATION_REPORT_PATH = RESULTS_ROOT / "validation" / "output_consistency.json"
RUBRIC_LABELS_PATH = RESULTS_ROOT / "rubric" / "labels.json"

LAYER_ORDER = [
    "semantic_rails",
    "metricflow",
    "cube",
    "malloy",
    "snowflake_semantic_views",
    "ktx",
]


def rel(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).relative_to(REPO_ROOT) if Path(path).is_absolute() else Path(path))


def abs_path(path: str | None) -> Path | None:
    return None if path is None else REPO_ROOT / path


def read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def excerpt_text(
    path: Path | None, *, max_lines: int = 36, around: str | None = None, context: int = 18
) -> str:
    text = read_text(path)
    if not text:
        return ""
    lines = text.splitlines()
    if around:
        for idx, line in enumerate(lines):
            if around in line:
                start = max(idx - 4, 0)
                end = min(idx + context, len(lines))
                return "\n".join(lines[start:end])
    return "\n".join(lines[:max_lines])


def clean_metricflow_sql(text: str) -> str:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("WITH ") or line.startswith("SELECT "):
            return "\n".join(lines[idx:])
    return text.strip()


def csv_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def json_rows(path: Path | None, *, layer: str) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if layer == "semantic_rails":
        return payload.get("rows", [])
    if layer == "cube":
        return payload.get("data", [])
    if layer in {"malloy", "snowflake_semantic_views", "ktx"}:
        return payload
    return []


def rows_for(layer: str, result_path: str | None) -> list[dict[str, Any]]:
    path = abs_path(result_path)
    if layer == "metricflow":
        return csv_rows(path)
    if layer in {"semantic_rails", "cube", "malloy", "snowflake_semantic_views", "ktx"}:
        return json_rows(path, layer=layer)
    return []


def rows_excerpt(rows: list[dict[str, Any]], *, max_rows: int = 8) -> str:
    if not rows:
        return ""
    return json.dumps(rows[:max_rows], indent=2, default=str)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_questions() -> list[dict[str, Any]]:
    payload = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return payload["questions"]


def nonempty_loc(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if line.strip())


def loc_for_paths(paths: list[Path]) -> int:
    return sum(nonempty_loc(path) for path in paths if path.exists())


def marker_loc(path: Path, marker: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    count = 0
    for line in lines:
        if marker in line:
            break
        if line.strip():
            count += 1
    return count


# Captures made before the runners recorded a timestamp predate the consistency report generated
# at 2026-06-24T03:47:13Z. Dates here are UTC.
UNRECORDED_CAPTURE = "by 2026-06-24 (exact date not recorded)"

LAYER_META: dict[str, dict[str, Any]] = {
    "semantic_rails": {
        "label": "Semantic Rails",
        # Fallback only: the runner records the engine version with its evidence.
        "version": "not recorded",
        "captured": UNRECORDED_CAPTURE,
        "setup_status": "executed",
        "comparison_type": "runnable",
        "summary_path": RESULTS_ROOT / "semantic_rails" / "summary.json",
        "unsupported_path": None,
        "strengths": [
            "Every question runs through the Semantic Rails runtime without helper views or handwritten SQL; q11 and q12 read precomputed customer lifetime columns from the source table.",
            "Temporal-valid joins, conversion metrics, authored metric predicates, and query-time metric filters stay first-class.",
        ],
        "weaknesses": [
            "This is a project-specific runtime rather than a broadly adopted external ecosystem.",
            "The DSL is specific to Semantic Rails.",
        ],
        "scale": {
            "baseline_files": [
                COMPARISON_ROOT / "semantic_rails" / "package" / "models" / "core" / "orders.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "core"
                / "order_items.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "core"
                / "customers.yml",
                COMPARISON_ROOT / "semantic_rails" / "package" / "models" / "core" / "stores.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "metrics"
                / "core"
                / "derived_metrics.yml",
            ],
            "stretch_files": [
                COMPARISON_ROOT / "semantic_rails" / "package" / "models" / "core" / "orders.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "core"
                / "order_items.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "core"
                / "customers.yml",
                COMPARISON_ROOT / "semantic_rails" / "package" / "models" / "core" / "stores.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "extensions"
                / "customer_history.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "extensions"
                / "order_lifecycle.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "models"
                / "extensions"
                / "storefront_sessions.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "metrics"
                / "core"
                / "derived_metrics.yml",
                COMPARISON_ROOT
                / "semantic_rails"
                / "package"
                / "metrics"
                / "extensions"
                / "advanced_metrics.yml",
            ],
            "baseline_relationships": 3,
            "stretch_relationships": 12,
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/orders.yml",
                "order_count:",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/orders.yml",
                "revenue_usd:",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/order_items.yml",
                "item_revenue_usd:",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/core/derived_metrics.yml",
                "sales.aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/orders.yml",
                "joins:",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/orders.yml",
                "new_customer_order_count:",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/models/extensions/order_lifecycle.yml",
                "delivered_revenue_usd:",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/semantic_rails/package/models/core/orders.yml",
                "temporal_validity:",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/extensions/advanced_metrics.yml",
                "sales.session_to_order_conversion_rate_7d",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/extensions/advanced_metrics.yml",
                "sales.orders_from_customers_with_10plus_orders_in_period",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/extensions/advanced_metrics.yml",
                "sales.repeat_customer_orders",
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/semantic_rails/queries/q12_orders_by_month_with_lifetime_spend_500_filter.json",
                '"kind": "metric_predicate"',
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/semantic_rails/queries/q13_daily_orders_from_customers_with_10plus_orders_in_month.json",
                "orders_from_customers_with_10plus_orders_in_period",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/extensions/advanced_metrics.yml",
                "sales.revenue_from_customers_with_10plus_orders_in_period",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/semantic_rails/package/metrics/extensions/advanced_metrics.yml",
                "sales.session_to_order_conversion_rate_7d_same_store",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/semantic_rails/package/models/extensions/order_lifecycle.yml",
                "relationship.jaffle_order_lifecycle_customer_history",
            ),
        },
        "notes": {
            "q08_revenue_by_customer_segment_as_of_order_time": "Temporal-valid customer history is modeled directly on the order-to-history edge.",
            "q09_session_to_order_conversion_7d": "The 7-day conversion window is expressed as a first-class conversion metric.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Expressed as an authored metric predicate over customer-month order counts.",
            "q11_repeat_customer_orders_by_store_by_month": "Expressed as an authored metric predicate over the precomputed `lifetime_order_count` customer column.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Applied at query time through `metric_filters.expression` over the precomputed `lifetime_spend_cents` customer column.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "The customer-month predicate is evaluated at month grain while the query returns day grain.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "The predicate inherits the outer store grouping through the entity graph.",
            "q15_same_store_session_to_order_conversion_7d": "Same-store matching is expressed as a first-class conversion property constraint.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Delivered time drives both the measure clock and the temporal-valid join into customer history.",
        },
    },
    "metricflow": {
        "label": "MetricFlow",
        "version": "dbt-metricflow 0.11.0 / dbt-duckdb 1.10.1",
        "captured": UNRECORDED_CAPTURE,
        "setup_status": "executed",
        "comparison_type": "runnable",
        "summary_path": RESULTS_ROOT / "metricflow" / "summary.json",
        "unsupported_path": RESULTS_ROOT / "metricflow" / "unsupported.json",
        "strengths": [
            "Temporal validity stays native and readable on both ordered-time and delivered-time questions.",
            "Generated SQL is clear and easy to inspect against the shared dataset.",
        ],
        "weaknesses": [
            "In this pack, q09-q15 run through helper dbt views; MetricFlow's native conversion metrics and metric filters have not been modeled yet.",
        ],
        "scale": {
            "baseline_files": [
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "orders.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "order_items.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "customers.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "stores.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "semantic_models" / "orders.yml",
                COMPARISON_ROOT / "metricflow" / "models" / "metrics.yml",
            ],
            "stretch_files": [
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "orders.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "order_items.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "customers.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "stores.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "customer_history.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "order_lifecycle.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "storefront_sessions.sql",
                COMPARISON_ROOT
                / "metricflow"
                / "models"
                / "staging"
                / "repeat_customer_orders.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "staging" / "high_value_orders_500.sql",
                COMPARISON_ROOT
                / "metricflow"
                / "models"
                / "staging"
                / "high_frequency_store_orders.sql",
                COMPARISON_ROOT
                / "metricflow"
                / "models"
                / "staging"
                / "session_conversions_7d_same_store.sql",
                COMPARISON_ROOT / "metricflow" / "models" / "semantic_models" / "orders.yml",
                COMPARISON_ROOT / "metricflow" / "models" / "semantic_models" / "extensions.yml",
                COMPARISON_ROOT / "metricflow" / "models" / "metrics.yml",
            ],
            "baseline_relationships": 5,
            "stretch_relationships": 16,
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/orders.yml",
                "- name: orders",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/orders.yml",
                "- name: revenue_usd",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/orders.yml",
                "- name: order_items",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/metricflow/models/metrics.yml",
                "- name: aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/orders.yml",
                "- name: order_items",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/orders.yml",
                "- name: new_customer_orders",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/extensions.yml",
                "- name: order_lifecycle",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/extensions.yml",
                "- name: customer_history",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/metricflow/models/metrics.yml",
                "- name: session_to_order_conversion_rate_7d",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/metricflow/models/metrics.yml",
                "- name: qualifying_orders",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/metricflow/models/staging/repeat_customer_orders.sql",
                "where c.lifetime_order_count > 1",
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/metricflow/models/staging/high_value_orders_500.sql",
                "where c.lifetime_spend_cents >= 50000",
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/extensions.yml",
                "- name: high_frequency_orders",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/metricflow/models/staging/high_frequency_store_orders.sql",
                "with customer_store_months as",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/metricflow/models/staging/session_conversions_7d_same_store.sql",
                "and s.store_id = o.store_id",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/metricflow/models/semantic_models/extensions.yml",
                "- name: order_lifecycle",
            ),
        },
        "notes": {
            "q08_revenue_by_customer_segment_as_of_order_time": "MetricFlow's validity parameters keep the as-of join inside the semantic model.",
            "q09_session_to_order_conversion_7d": "Executed through a helper dbt view that materializes session-level 7-day conversion flags; MetricFlow's native conversion metrics are not modeled yet.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Executed through a helper dbt view that precomputes qualifying customer-month orders.",
            "q11_repeat_customer_orders_by_store_by_month": "Executed through a helper dbt view that filters on the precomputed `lifetime_order_count` customer column; MetricFlow's metric filters are not modeled yet.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Executed through a helper dbt view that filters on the precomputed `lifetime_spend_cents` customer column; MetricFlow's metric filters are not modeled yet.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "Reuses the precomputed qualifying customer-month order view, then queries it at day grain.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "Executed through a helper dbt view that materializes qualifying customer store-month orders with revenue attached.",
            "q15_same_store_session_to_order_conversion_7d": "Executed through a helper dbt view that materializes same-store 7-day conversion flags; MetricFlow's native conversion metrics (with constant properties) are not modeled yet.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Delivered revenue stays native because the delivered-time metric and validity-windowed customer history both live inside the semantic model graph.",
        },
    },
    "cube": {
        "label": "Cube",
        "version": "1.6.32",
        # When Cube itself ran: its captured results record lastRefreshTime 2026-04-07T03:04:57Z.
        "captured": "2026-04-07",
        "setup_status": "captured SQL re-executed on the current dataset",
        "comparison_type": "runnable",
        # Cube can't be re-run until its dependency advisories are resolved, so its captured
        # SQL is re-executed on the current dataset (cube/scripts/replay_sql.py).
        "summary_path": RESULTS_ROOT / "cube_sql_replay" / "summary.json",
        "unsupported_path": RESULTS_ROOT / "cube" / "unsupported.json",
        "strengths": [
            "The baseline cubes are compact and the local DuckDB setup is straightforward.",
            "Temporal history can be modeled with explicit join SQL when needed.",
        ],
        "weaknesses": [
            "In this pack, q05 and q09-q16 run through helper cubes or joined rollup filters; Cube's multi-fact queries, multi-stage measures and subquery dimensions have not been modeled yet.",
        ],
        "capture_notes": [
            "Cube 1.6.32 can't be reinstalled until the captured lockfile's dependency advisories are resolved, so its captured SQL is re-executed on the current dataset.",
        ],
        "scale": {
            "baseline_files": [
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "orders.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "order_items.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "customers.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "stores.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "order_metrics.yml",
            ],
            "stretch_files": [
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "orders.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "order_items.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "customers.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "stores.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "order_metrics.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "customer_history.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "order_lifecycle.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "storefront_sessions.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "session_conversions_7d.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "qualified_orders.yml",
                COMPARISON_ROOT / "cube" / "model" / "cubes" / "high_frequency_store_orders.yml",
                COMPARISON_ROOT
                / "cube"
                / "model"
                / "cubes"
                / "session_conversions_7d_same_store.yml",
                COMPARISON_ROOT
                / "cube"
                / "model"
                / "cubes"
                / "delivered_orders_with_customer_segment.yml",
            ],
            "baseline_relationships": 4,
            "stretch_relationships": 9,
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/orders.yml",
                "- name: orders",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/orders.yml",
                "- name: revenue_usd",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/order_items.yml",
                "- name: item_revenue_usd",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/cube/model/cubes/orders.yml",
                "- name: aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/order_metrics.yml",
                "- name: order_metrics",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/orders.yml",
                "- name: new_customer_orders",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/cube/model/cubes/order_lifecycle.yml",
                "- name: delivered_revenue",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/cube/model/cubes/orders.yml",
                "- name: customer_history",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/cube/model/cubes/session_conversions_7d.yml",
                "- name: session_to_order_conversion_rate_7d",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/cube/model/cubes/qualified_orders.yml",
                "- name: qualifying_orders",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/cube/queries/q11_repeat_customer_orders_by_store_by_month.json",
                '"customers.lifetime_order_count"',
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/cube/queries/q12_orders_by_month_with_lifetime_spend_500_filter.json",
                '"customers.lifetime_spend_usd"',
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/cube/model/cubes/qualified_orders.yml",
                "date_trunc('month', ordered_at)",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/cube/model/cubes/high_frequency_store_orders.yml",
                "with customer_store_months as",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/cube/model/cubes/session_conversions_7d_same_store.yml",
                "and s.store_id = o.store_id",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/cube/model/cubes/delivered_orders_with_customer_segment.yml",
                "left join comparison_customer_history",
            ),
        },
        "notes": {
            "q05_orders_and_item_revenue_by_store_by_month": "Executed through a helper order-grain cube that rolls item revenue up before Cube aggregates it with order count; Cube's multi-fact queries are not modeled yet.",
            "q08_revenue_by_customer_segment_as_of_order_time": "Modeled as a declared join from orders to customer history whose `sql` carries the validity condition.",
            "q09_session_to_order_conversion_7d": "Executed through a dedicated helper cube that materializes the 7-day session-to-order match.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Executed through a dedicated helper cube that materializes qualifying customer-month orders.",
            "q11_repeat_customer_orders_by_store_by_month": "Executed by filtering the orders cube on the joined, precomputed customer lifetime order count.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Executed by filtering the orders cube on the joined, precomputed customer lifetime spend.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "Executed through the precomputed qualifying-orders helper cube at day grain.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "Executed through a helper cube that materializes qualifying customer store-month revenue.",
            "q15_same_store_session_to_order_conversion_7d": "Executed through a helper cube that materializes same-store 7-day session matches.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Executed through a helper cube that bakes the delivered-time as-of join into SQL.",
        },
    },
    "malloy": {
        "label": "Malloy",
        "version": "0.0.52",
        "captured": UNRECORDED_CAPTURE,
        "setup_status": "executed",
        "comparison_type": "runnable",
        "summary_path": RESULTS_ROOT / "malloy" / "summary.json",
        "unsupported_path": None,
        "strengths": [
            "The authored surface stays compact, especially for mixed-grain q05.",
            "Join-tree aggregation is expressive without a large semantic scaffolding layer.",
        ],
        "weaknesses": [
            "In this pack, q08-q16 run through SQL sources or query-level filters; Malloy's arbitrary-condition joins and query-derived join sources have not been modeled yet.",
        ],
        "scale": {
            "baseline_files": [COMPARISON_ROOT / "malloy" / "models" / "jaffle.malloy"],
            "stretch_files": [COMPARISON_ROOT / "malloy" / "models" / "jaffle.malloy"],
            "baseline_relationships": 3,
            "stretch_relationships": 4,
            "baseline_marker": "# Stretch scope adds an alternate clock plus SQL-backed workaround sources.",
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: orders is",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "measure: revenue_usd",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: order_items",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "measure: aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "query: q05_orders_and_item_revenue_by_store_by_month",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "measure: new_customer_orders",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: order_lifecycle",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: orders_with_customer_segment",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: session_conversions_7d",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: orders_from_high_frequency_customers",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "query: q11_repeat_customer_orders_by_store_by_month",
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "query: q12_orders_by_month_with_lifetime_spend_500_filter",
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "query: q13_daily_orders_from_customers_with_10plus_orders_in_month",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: revenue_from_high_frequency_store_customers",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: session_conversions_7d_same_store",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/malloy/models/jaffle.malloy",
                "source: delivered_orders_with_customer_segment",
            ),
        },
        "notes": {
            "q05_orders_and_item_revenue_by_store_by_month": "Malloy's join-tree aggregation keeps the mixed-grain query native and compact.",
            "q08_revenue_by_customer_segment_as_of_order_time": "Executed via a SQL source embedded inside the Malloy model.",
            "q09_session_to_order_conversion_7d": "Executed via a SQL source that materializes the 7-day matching window.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Executed via a SQL source that precomputes qualifying customer-months.",
            "q11_repeat_customer_orders_by_store_by_month": "Executed as a query-level filter on the joined, precomputed customer lifetime order count.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Executed as a query-level filter on the joined, precomputed customer lifetime spend.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "Executed through the SQL-backed qualifying-order source at day grain.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "Executed through a SQL source that materializes qualifying customer store-month revenue.",
            "q15_same_store_session_to_order_conversion_7d": "Executed through a SQL source that materializes same-store session matches.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Executed through a SQL source that bakes the delivered-time temporal join into the query model.",
        },
    },
    "snowflake_semantic_views": {
        "label": "Snowflake Semantic Views",
        "version": "Snowflake CLI + semantic view trial account",
        "captured": UNRECORDED_CAPTURE,
        "setup_status": "executed",
        "comparison_type": "runnable",
        "summary_path": RESULTS_ROOT / "snowflake_semantic_views" / "summary.json",
        "unsupported_path": RESULTS_ROOT / "snowflake_semantic_views" / "unsupported.json",
        "strengths": [
            "q01-q07 execute through the native `SEMANTIC_VIEW(...)` surface on a real Snowflake semantic view.",
            "Semantic views package derived metrics, access modifiers, and query onboarding directly in the warehouse catalog.",
        ],
        "weaknesses": [
            "This pack's semantic view defines its time dimensions at timestamp grain, so month-grain questions apply `DATE_TRUNC(...)` in the query; the view could define month-grain dimensions instead.",
            "In this pack, q08-q16 run as SQL outside `SEMANTIC_VIEW(...)`; range joins, announced in preview on 2026-02-25, have not been modeled yet.",
        ],
        "capture_notes": [
            "The capture comes from a trial account and cannot be re-run without a live Snowflake account.",
        ],
        "scale": {
            "baseline_files": [
                COMPARISON_ROOT / "snowflake_semantic_views" / "jaffle_semantic_view.yaml"
            ],
            "stretch_files": [
                COMPARISON_ROOT / "snowflake_semantic_views" / "jaffle_semantic_view.yaml"
            ],
            "baseline_relationships": 3,
            "stretch_relationships": 7,
            "baseline_marker": "  # Stretch tables",
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: orders",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: revenue_usd",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: item_revenue_usd",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: order_items_to_orders",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: new_customer_orders",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/jaffle_semantic_view.yaml",
                "- name: delivered_revenue",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q08_revenue_by_customer_segment_as_of_order_time",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q09_session_to_order_conversion_7d",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q10_orders_from_customers_with_10plus_orders_in_month",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q11_repeat_customer_orders_by_store_by_month",
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q12_orders_by_month_with_lifetime_spend_500_filter",
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q13_daily_orders_from_customers_with_10plus_orders_in_month",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q14_revenue_from_customers_with_10plus_orders_same_store_month",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q15_same_store_session_to_order_conversion_7d",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql",
                "q16_revenue_by_customer_segment_as_of_delivered_time",
            ),
        },
        "notes": {
            "q05_orders_and_item_revenue_by_store_by_month": "This mixed-grain question stays native as long as the query explicitly defines month grain in the `SEMANTIC_VIEW(...)` call.",
            "q08_revenue_by_customer_segment_as_of_order_time": "Executed as SQL on the comparison tables; range joins, which could express the as-of join inside the semantic view, are not modeled yet.",
            "q09_session_to_order_conversion_7d": "Executed as SQL on the comparison tables.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Executed as SQL on the comparison tables.",
            "q11_repeat_customer_orders_by_store_by_month": "Executed as SQL over the precomputed customer lifetime order count.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Executed as SQL over the precomputed customer lifetime spend.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "Executed as SQL on the comparison tables.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "Executed as SQL on the comparison tables.",
            "q15_same_store_session_to_order_conversion_7d": "Executed as SQL on the comparison tables; same-store event-pair matching is not modeled in the semantic view.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Executed as SQL on the comparison tables; range joins are not modeled yet.",
        },
    },
    "ktx": {
        "label": "KtX",
        "version": "ktx-sl 0.13.1 / KtX a155c0b",
        "captured": UNRECORDED_CAPTURE,
        "setup_status": "executed",
        "comparison_type": "runnable",
        "summary_path": RESULTS_ROOT / "ktx" / "summary.json",
        "unsupported_path": None,
        "strengths": [
            "The Python semantic layer compiles compact YAML sources to DuckDB SQL and executes the portable q01-q07 suite cleanly.",
            "Aggregate locality keeps the mixed-grain q05 orders-plus-item-revenue query native without a helper mart.",
        ],
        "weaknesses": [
            "In this pack, q08-q16 run through SQL-backed sources or query-level filters.",
            "This pack exercises ktx-sl directly, not the broader KtX context ingestion, wiki/search, daemon, and MCP stack.",
        ],
        "scale": {
            "baseline_files": [
                COMPARISON_ROOT / "ktx" / "sources" / "orders.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "order_items.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "customers.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "stores.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "order_lifecycle.yaml",
            ],
            "stretch_files": [
                COMPARISON_ROOT / "ktx" / "sources" / "orders.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "order_items.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "customers.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "stores.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "order_lifecycle.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "revenue_by_customer_segment_order_time.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "session_conversion_7d.yaml",
                COMPARISON_ROOT
                / "ktx"
                / "sources"
                / "orders_from_high_frequency_customers_month.yaml",
                COMPARISON_ROOT
                / "ktx"
                / "sources"
                / "daily_orders_from_high_frequency_customers_month.yaml",
                COMPARISON_ROOT
                / "ktx"
                / "sources"
                / "revenue_from_high_frequency_store_customers.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "session_conversion_7d_same_store.yaml",
                COMPARISON_ROOT / "ktx" / "sources" / "delivered_revenue_by_customer_segment.yaml",
            ],
            "baseline_relationships": 5,
            "stretch_relationships": 5,
        },
        "snippets": {
            "q01_orders_by_month": (
                "comparisons/semantic_layers/ktx/sources/orders.yaml",
                "name: orders",
            ),
            "q02_revenue_by_store_by_month": (
                "comparisons/semantic_layers/ktx/sources/orders.yaml",
                "revenue_usd",
            ),
            "q03_item_revenue_by_product_type_by_month": (
                "comparisons/semantic_layers/ktx/sources/order_items.yaml",
                "item_revenue_usd",
            ),
            "q04_aov_by_store": (
                "comparisons/semantic_layers/ktx/sources/orders.yaml",
                "aov_usd",
            ),
            "q05_orders_and_item_revenue_by_store_by_month": (
                "comparisons/semantic_layers/ktx/sources/order_items.yaml",
                "to: orders",
            ),
            "q06_new_customer_orders_by_month": (
                "comparisons/semantic_layers/ktx/sources/orders.yaml",
                "new_customer_orders",
            ),
            "q07_delivered_revenue_by_month": (
                "comparisons/semantic_layers/ktx/sources/order_lifecycle.yaml",
                "delivered_revenue",
            ),
            "q08_revenue_by_customer_segment_as_of_order_time": (
                "comparisons/semantic_layers/ktx/sources/revenue_by_customer_segment_order_time.yaml",
                "comparison_customer_history",
            ),
            "q09_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/ktx/sources/session_conversion_7d.yaml",
                "interval '7 day'",
            ),
            "q10_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/ktx/sources/orders_from_high_frequency_customers_month.yaml",
                "monthly_orders > 10",
            ),
            "q11_repeat_customer_orders_by_store_by_month": (
                "comparisons/semantic_layers/ktx/queries/q11_repeat_customer_orders_by_store_by_month.json",
                "customers.lifetime_order_count > 1",
            ),
            "q12_orders_by_month_with_lifetime_spend_500_filter": (
                "comparisons/semantic_layers/ktx/queries/q12_orders_by_month_with_lifetime_spend_500_filter.json",
                "customers.lifetime_spend_cents >= 50000",
            ),
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": (
                "comparisons/semantic_layers/ktx/sources/daily_orders_from_high_frequency_customers_month.yaml",
                "ordered_day",
            ),
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": (
                "comparisons/semantic_layers/ktx/sources/revenue_from_high_frequency_store_customers.yaml",
                "customer_store_months",
            ),
            "q15_same_store_session_to_order_conversion_7d": (
                "comparisons/semantic_layers/ktx/sources/session_conversion_7d_same_store.yaml",
                "s.store_id = o.store_id",
            ),
            "q16_revenue_by_customer_segment_as_of_delivered_time": (
                "comparisons/semantic_layers/ktx/sources/delivered_revenue_by_customer_segment.yaml",
                "l.delivered_at >= h.valid_from",
            ),
        },
        "notes": {
            "q05_orders_and_item_revenue_by_store_by_month": "KtX aggregate locality keeps order-grain and item-grain measures from silently fanning out.",
            "q08_revenue_by_customer_segment_as_of_order_time": "Executed through a KtX SQL source that authors the as-of customer-history join by hand.",
            "q09_session_to_order_conversion_7d": "Executed through a KtX SQL source that materializes the 7-day session-to-order match.",
            "q10_orders_from_customers_with_10plus_orders_in_month": "Executed through a KtX SQL source that precomputes qualifying customer-month orders.",
            "q11_repeat_customer_orders_by_store_by_month": "Executed as a query-level filter on the joined, precomputed customer lifetime order count.",
            "q12_orders_by_month_with_lifetime_spend_500_filter": "Executed as a query-level filter on the joined, precomputed customer lifetime spend.",
            "q13_daily_orders_from_customers_with_10plus_orders_in_month": "Executed through the SQL-backed qualifying-order source at day grain.",
            "q14_revenue_from_customers_with_10plus_orders_same_store_month": "Executed through a KtX SQL source that materializes qualifying customer store-month revenue.",
            "q15_same_store_session_to_order_conversion_7d": "Executed through a KtX SQL source that materializes same-store session matches.",
            "q16_revenue_by_customer_segment_as_of_delivered_time": "Executed through a KtX SQL source that bakes the delivered-time temporal join into the model.",
        },
    },
}


def default_note(status: str) -> str:
    return {
        "native": "Executed with the layer's own semantic constructs.",
        "workaround": "Executed through SQL written by hand for this pack.",
        "precomputed": "Executed by reading a rollup column that the question declares.",
        "doc_backed": "Represented from the public spec/docs, but not executed locally in this repo.",
        "unsupported": "Not executed in this pack.",
    }.get(status, f"Labeled {status}.")


def load_summary_entries(layer_id: str) -> dict[str, dict[str, Any]]:
    meta = LAYER_META[layer_id]
    if meta["summary_path"] is None:
        entries = {}
        for question in load_questions():
            qid = question["id"]
            entries[qid] = {"question_id": qid, "status": "doc_backed"}
        return entries
    payload = load_json(meta["summary_path"])
    entries = {entry["question_id"]: entry for entry in payload["questions"]}
    unsupported_path = meta["unsupported_path"]
    if unsupported_path and unsupported_path.exists():
        unsupported = load_json(unsupported_path)
        entries.update(
            {
                qid: {
                    "question_id": qid,
                    "status": item["status"],
                    "reason": item["reason"],
                }
                for qid, item in unsupported.items()
            }
        )
    return entries


def layer_scale(layer_id: str) -> dict[str, Any]:
    """Authored-size counts for the 4-model and 7-model sets (not question slices)."""
    meta = LAYER_META[layer_id]["scale"]
    baseline_files = meta["baseline_files"]
    stretch_files = meta["stretch_files"]

    if "baseline_marker" in meta:
        baseline_loc = marker_loc(baseline_files[0], meta["baseline_marker"])
    else:
        baseline_loc = loc_for_paths(baseline_files)

    return {
        "baseline": {
            "models": 4,
            "files": len(baseline_files),
            "loc": baseline_loc,
            "relationships": meta["baseline_relationships"],
        },
        "stretch": {
            "models": 7,
            "files": len(stretch_files),
            "loc": loc_for_paths(stretch_files),
            "relationships": meta["stretch_relationships"],
        },
    }


def entry_for_question(
    layer_id: str, question: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    qid = question["id"]
    meta = LAYER_META[layer_id]
    status = entry["status"]
    note = meta["notes"].get(qid) or entry.get("reason") or default_note(status)

    snippet_path, search = meta["snippets"][qid]
    query_path = entry.get("query_path")
    result_path = entry.get("result_path")
    sql_path = entry.get("sql_path")
    if query_path is None:
        if layer_id == "snowflake_semantic_views":
            query_path = "comparisons/semantic_layers/snowflake_semantic_views/query_examples.sql"
        else:
            query_path = snippet_path
    if result_path and not abs_path(result_path).exists():
        result_path = None
    if sql_path and not abs_path(sql_path).exists():
        sql_path = None

    snippet_excerpt = excerpt_text(abs_path(snippet_path), around=search)
    query_excerpt = excerpt_text(
        abs_path(query_path),
        around=qid if query_path and qid in read_text(abs_path(query_path)) else None,
        max_lines=24,
    )
    rows = rows_for(layer_id, result_path)
    result_excerpt = rows_excerpt(rows)

    sql_excerpt = ""
    if sql_path:
        sql_text = read_text(abs_path(sql_path))
        if layer_id == "metricflow":
            sql_text = clean_metricflow_sql(sql_text)
        elif layer_id == "cube":
            try:
                payload = json.loads(sql_text)
                sql_text = payload["sql"]["sql"][0][0]
            except Exception:  # noqa: BLE001
                pass
        # Excerpts show the SQL itself; captured comments are commentary, not evidence.
        sql_lines = [line for line in sql_text.splitlines() if not line.lstrip().startswith("--")]
        sql_excerpt = "\n".join(sql_lines[:36])
    elif layer_id == "snowflake_semantic_views":
        sql_excerpt = excerpt_text(
            COMPARISON_ROOT / "snowflake_semantic_views" / "query_examples.sql",
            around=qid,
            max_lines=32,
        )

    return {
        "question_id": qid,
        "support_status": status,
        "label_evidence": entry.get("label_evidence", []),
        "snippet_path": snippet_path,
        "query_path": query_path,
        "result_path": result_path,
        "sql_path": sql_path,
        "notes": note,
        "row_count": len(rows) if rows else None,
        "snippet_excerpt": snippet_excerpt,
        "query_excerpt": query_excerpt,
        "result_excerpt": result_excerpt,
        "sql_excerpt": sql_excerpt,
    }


SUPPORT_STATUSES = ("native", "workaround", "precomputed", "doc_backed", "unsupported")
ANSWER_KEY = "answer_key"
ANSWER_KEY_DISCLOSURES = [
    "The answer key is SQL written directly against the shared views, not generated by any layer. "
    "An agent wrote it from the question text and the raw data without seeing any layer's models "
    "or outputs, and a second agent that didn't write it reviewed it (shared/oracle/SEMANTICS.md).",
    "On this data several questions can't tell a right answer from a common wrong one: delivered "
    "time never moves an order into another month, no customer orders at two stores, all sessions "
    "are at one store on one day, and customer history covers 4 customers. Matching the answer "
    "key there is weak evidence of the intended semantics (shared/oracle/SEMANTICS.md).",
]
SLICE_LABELS = {
    "shared": "Shared questions",
    "semantic_rails_targeted": "Semantic-Rails-targeted questions",
}
SCALE_UP_CAVEAT = (
    "Authored-size counts are not yet uniform across layers: the Semantic Rails count omits "
    "graph.yml, core_metrics.yml and package.yml. Do not compare sizes until one script counts "
    "every layer's authored files the same way. 'baseline' and 'stretch' here are the 4-model "
    "and 7-model sets, not question slices."
)

# Findings that describe how this pack models each layer. They must not rank the layers on the
# Semantic-Rails-targeted questions until every layer is modeled with the features it ships.
LAYER_FINDINGS = [
    "MetricFlow answers q08 and q16 with validity-windowed semantic models; this pack answers q09, q10 and q13-q15 through helper dbt views, and q11-q12 through helper views over the precomputed rollups. MetricFlow's native conversion metrics and metric filters have not been modeled yet.",
    "Cube answers q08 through a declared join that carries the validity condition; this pack answers q05, q09, q10 and q13-q16 through helper cubes, and q11-q12 through filters on joined rollup columns. Cube's multi-fact queries, multi-stage measures and subquery dimensions have not been modeled yet.",
    "Malloy answers q08-q16 through SQL sources or query-level filters in this pack; Malloy's arbitrary-condition joins and query-derived join sources have not been modeled yet.",
    "Snowflake Semantic Views answers q01-q07 through `SEMANTIC_VIEW(...)` and q08-q16 as SQL on the same tables; range joins have not been modeled yet.",
    "KtX answers q01-q07 through its Python semantic layer (ktx-sl) and q08-q16 through SQL-backed sources or query-level filters in this pack.",
    "The numeric suite is still not the whole story: MetricFlow keeps meaningful compiler-surface strengths on controls like metric-time-only planning and duplicate-alias rejection that are documented separately, not scored here.",
]


def status_totals(statuses: list[str]) -> dict[str, int]:
    counts = Counter(statuses)
    # Unexpected labels are kept rather than dropped silently.
    return {status: counts.get(status, 0) for status in SUPPORT_STATUSES} | {
        status: count for status, count in counts.items() if status not in SUPPORT_STATUSES
    }


def short_id(question_id: str) -> str:
    return question_id.split("_", 1)[0]


def id_range(question_ids: list[str]) -> str:
    """Compact question ids into runs, for example `q01-q07` or `q01-q06, q08`."""
    runs: list[list[int]] = []
    for number in sorted(int(short_id(qid)[1:]) for qid in question_ids):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    return ", ".join(
        f"q{run[0]:02d}" if len(run) == 1 else f"q{run[0]:02d}-q{run[-1]:02d}" for run in runs
    )


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def recorded_version(layer_id: str, summary: dict[str, Any]) -> str:
    recorded = summary.get("semantic_rails_version")
    if not recorded:
        return str(LAYER_META[layer_id]["version"])
    # An engine that isn't exactly a release is never labeled as that release.
    if "engine_release" in summary and summary["engine_release"] is None:
        tree = summary.get("semantic_rails_tree")
        detail = f"engine tree {tree[:7]}" if tree else "engine source not recorded"
        return f"{recorded}, not a release ({detail})"
    return str(recorded)


def recorded_capture(layer_id: str, summary: dict[str, Any]) -> str:
    """The capture date in UTC, taken from the evidence when it records a timestamp."""
    generated_at = summary.get("generated_at")
    if not generated_at:
        return LAYER_META[layer_id]["captured"]
    return datetime.fromisoformat(generated_at).astimezone(UTC).date().isoformat()


def claim_findings(
    validation_report: dict[str, Any],
    questions: list[dict[str, Any]],
    slice_ids: dict[str, list[str]],
    layers_payload: list[dict[str, Any]],
) -> list[str]:
    """Headline claims generated from the consistency report, mismatches included."""
    # Every claim below says "the answer key"; a report checked against anything else can't back it.
    if validation_report.get("reference_layer") != ANSWER_KEY:
        raise SystemExit(
            "The validation report doesn't check the layers against the answer key; "
            "run validate_output_consistency.py first."
        )
    summary = validation_report["summary"]
    items = validation_report["questions"]
    total = len(questions)
    title_by_id = {question["id"]: question["title"] for question in questions}
    label = {layer_id: LAYER_META[layer_id]["label"] for layer_id in LAYER_ORDER}
    label[ANSWER_KEY] = "the answer key"
    layer_sets = {tuple(item["current_layers"]) for item in items}
    if layer_sets == {tuple(LAYER_ORDER)}:
        scope = f"all {len(LAYER_ORDER)} layers"
    elif len(layer_sets) == 1:
        names = [label[layer_id] for layer_id in layer_sets.pop()]
        scope = f"the {len(names)} layers checked on the current dataset ({join_names(names)})"
    else:
        scope = "the layers that executed them on the current dataset"
    mismatched = [item for item in items if item["comparison_status"] == "mismatched"]
    if summary["mismatched"] == 0 and summary["not_comparable"] == 0:
        output_check = (
            f"On all {total} questions, {scope} return the independent answer key's normalized "
            "outputs, with numbers matching within 1e-6."
        )
    else:
        output_check = (
            f"On {summary['matched']} of {total} questions, {scope} return the independent answer "
            "key's normalized outputs, with numbers matching within 1e-6."
        )
        if mismatched:
            listed = "; ".join(
                f"{short_id(item['question_id'])} {title_by_id[item['question_id']]}"
                for item in mismatched
            )
            output_check += f" On {len(mismatched)}, at least one layer differs: {listed}."
        if summary["not_comparable"]:
            output_check += f" {summary['not_comparable']} could not be compared."
    claims = [output_check]
    for layer in layers_payload:
        # A replay on an earlier dataset is reported with the stale captures below instead.
        if layer.get("re_executed") and layer["dataset"] == "current":
            claims.append(
                f"{layer['label']} {layer['version']} was not re-run: the SQL it generated on "
                f"{layer['captured']} was re-executed on the current dataset on "
                f"{layer['re_executed']}."
            )
    # The date each layer itself ran; a replay's report records when it was re-executed.
    captured = {layer["id"]: layer["captured"] for layer in layers_payload}
    for layer_id, checks in validation_report["stale_layers"].items():
        differs = ", ".join(short_id(qid) for qid in checks["mismatched"]) or "none"
        claims.append(
            f"{label[layer_id]} was captured on {captured[layer_id]} on an earlier dataset and "
            "has not been re-run, so it is left out of that count. Its capture matches the "
            f"answer key on {len(checks['matched'])} questions and differs on: {differs}."
        )

    # Say who disagrees with whom, so a mismatch isn't read as a competitor's error.
    splits: dict[str, list[str]] = {}
    for item in mismatched:
        parts = []
        for group in sorted(item["agreement_groups"], key=len, reverse=True):
            names = [label[layer_id] for layer_id in group]
            parts.append(
                f"{names[0]} differs"
                if len(names) == 1
                else f"{join_names(names)} agree with each other"
            )
        splits.setdefault("; ".join(parts), []).append(short_id(item["question_id"]))
    claims += [f"On {join_names(qids)}: {text}." for text, qids in splits.items()]
    claims += ANSWER_KEY_DISCLOSURES

    unsupported: dict[str, list[str]] = {}
    for item in items:
        for layer_id, status in item["layer_statuses"].items():
            if status == "unsupported":
                unsupported.setdefault(layer_id, []).append(item["question_id"])
    claims += [
        f"{label[layer_id]} did not execute {id_range(qids)}."
        for layer_id, qids in unsupported.items()
    ]

    by_slice = validation_report["summary_by_slice"]
    claims.append(
        " ".join(
            f"{SLICE_LABELS[name]} ({id_range(ids)}): {by_slice[name]['matched']} of "
            f"{by_slice[name]['questions']} match."
            for name, ids in slice_ids.items()
        )
    )
    targeted = slice_ids.get("semantic_rails_targeted", [])
    if targeted:
        claims.append(
            f"{len(targeted)} of the {total} questions ({id_range(targeted)}) were chosen to "
            "exercise features Semantic Rails ships. The Semantic Rails authors wrote every "
            "layer's models, and several layers are not yet modeled with native features they "
            "ship, so these questions are a capability showcase, not a ranking."
        )

    claims.append(
        "Every support label comes from one executable rubric applied to every layer, Semantic "
        "Rails included (shared/rubric.md): unsupported when a layer didn't execute the question, "
        "precomputed when its answer reads a rollup column the question declares, workaround "
        "when the answer depends on SQL written by hand for this pack, and native otherwise."
    )
    for question in questions:
        columns = question.get("bypass_columns") or []
        if not columns:
            continue
        by_label: dict[str, list[str]] = {}
        for layer in layers_payload:
            label_of = next(
                item["support_status"]
                for item in layer["questions"]
                if item["question_id"] == question["id"]
            )
            by_label.setdefault(label_of, []).append(layer["label"])
        described = "; ".join(
            f"{label_of} for {join_names(names)}" for label_of, names in by_label.items()
        )
        claims.append(
            f"{short_id(question['id'])} asks for a rollup that the shared data already holds in "
            f"{join_names([f'`{c}`' for c in columns])}; it is labeled {described}."
        )
    return claims


def build_contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    questions = load_questions()
    question_by_id = {question["id"]: question for question in questions}
    validation_report = load_json(VALIDATION_REPORT_PATH)
    rubric_output = load_json(RUBRIC_LABELS_PATH)
    rubric = rubric_output["labels"]
    validation_by_question = {item["question_id"]: item for item in validation_report["questions"]}
    slice_ids: dict[str, list[str]] = {}
    for item in validation_report["questions"]:
        slice_ids.setdefault(item["slice"], []).append(item["question_id"])
    layers_payload = []

    for layer_id in LAYER_ORDER:
        summary_path = LAYER_META[layer_id]["summary_path"]
        summary = load_json(summary_path) if summary_path else {}
        entries = {
            qid: {
                **entry,
                "status": rubric[layer_id][qid]["label"],
                "label_evidence": rubric[layer_id][qid]["evidence"],
            }
            for qid, entry in load_summary_entries(layer_id).items()
        }
        status_map = {qid: entry["status"] for qid, entry in entries.items()}

        question_entries = [
            entry_for_question(layer_id, question_by_id[qid], entries[qid])
            for qid in question_by_id
        ]
        # A replay (its summary records a method) keeps the date the layer itself ran.
        replayed = bool(summary.get("method"))
        layers_payload.append(
            {
                "id": layer_id,
                "label": LAYER_META[layer_id]["label"],
                "version": recorded_version(layer_id, summary),
                "captured": (
                    LAYER_META[layer_id]["captured"]
                    if replayed
                    else recorded_capture(layer_id, summary)
                ),
                "re_executed": recorded_capture(layer_id, summary) if replayed else None,
                "dataset": "stale" if layer_id in validation_report["stale_layers"] else "current",
                # What each runner recorded about how it ran: tool versions, replay method.
                "environment": summary.get("environment"),
                "method": summary.get("method"),
                "setup_status": LAYER_META[layer_id]["setup_status"],
                "comparison_type": LAYER_META[layer_id]["comparison_type"],
                "strengths": LAYER_META[layer_id]["strengths"],
                "weaknesses": LAYER_META[layer_id]["weaknesses"],
                "capture_notes": LAYER_META[layer_id].get("capture_notes", []),
                # Scored per slice only: the targeted questions are not a ranking.
                "status_totals_by_slice": {
                    slice_name: status_totals([status_map[qid] for qid in ids])
                    for slice_name, ids in slice_ids.items()
                },
                "scale": layer_scale(layer_id),
                "questions": question_entries,
            }
        )

    findings = claim_findings(validation_report, questions, slice_ids, layers_payload)
    comparison_data = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "headline_findings": findings + LAYER_FINDINGS,
        "validation_summary": validation_report["summary"],
        "validation_summary_by_slice": validation_report["summary_by_slice"],
        "scale_up_caveat": SCALE_UP_CAVEAT,
        "questions": questions,
        "layers": layers_payload,
    }

    matrix_rows = []
    for question in questions:
        qid = question["id"]
        row = {
            "question_id": qid,
            "title": question["title"],
            "category": question["category"],
            "scope_level": question["scope_level"],
            "slice": validation_by_question[qid]["slice"],
            "business_question": question["business_question"],
            "expected_semantics": question["expected_semantics"],
            "consistency_status": validation_by_question[qid]["comparison_status"],
            "statuses": {
                layer["id"]: next(
                    item for item in layer["questions"] if item["question_id"] == qid
                )["support_status"]
                for layer in layers_payload
            },
        }
        matrix_rows.append(row)

    capability_matrix = {
        "generated_at": comparison_data["generated_at"],
        "claims": findings,
        "rubric": {"rules": rubric_output["rules"], "labels": "shared/results/rubric/labels.json"},
        "layers": [
            {
                "id": layer["id"],
                "label": layer["label"],
                "version": layer["version"],
                "captured": layer["captured"],
                "re_executed": layer["re_executed"],
                "dataset": layer["dataset"],
                "environment": layer["environment"],
                "method": layer["method"],
                "setup_status": layer["setup_status"],
                "status_totals_by_slice": layer["status_totals_by_slice"],
            }
            for layer in layers_payload
        ],
        "rows": matrix_rows,
        "summary": {
            "output_consistency": validation_report["summary"],
            "output_consistency_by_slice": validation_report["summary_by_slice"],
            "scale_up_caveat": SCALE_UP_CAVEAT,
            "scale_up": [
                {
                    "layer": layer["id"],
                    "label": layer["label"],
                    **{
                        f"baseline_{key}": value
                        for key, value in layer["scale"]["baseline"].items()
                    },
                    **{f"stretch_{key}": value for key, value in layer["scale"]["stretch"].items()},
                }
                for layer in layers_payload
            ],
        },
    }

    return comparison_data, capability_matrix


def main() -> None:
    comparison_data, capability_matrix = build_contracts()
    COMPARISON_DATA_PATH.write_text(json.dumps(comparison_data, indent=2), encoding="utf-8")
    CAPABILITY_MATRIX_PATH.write_text(json.dumps(capability_matrix, indent=2), encoding="utf-8")
    print(f"Wrote {COMPARISON_DATA_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {CAPABILITY_MATRIX_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
