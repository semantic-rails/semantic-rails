from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
RESULTS_ROOT = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results"
REPORT_JSON = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "determinism_report.json"
REPORT_MD = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "determinism_report.md"

QUESTION_ORDER = [
    "q01_orders_by_month",
    "q02_revenue_by_store_by_month",
    "q03_item_revenue_by_product_type_by_month",
    "q04_aov_by_store",
    "q05_orders_and_item_revenue_by_store_by_month",
    "q06_new_customer_orders_by_month",
    "q07_delivered_revenue_by_month",
    "q08_revenue_by_customer_segment_as_of_order_time",
    "q09_session_to_order_conversion_7d",
    "q10_orders_from_customers_with_10plus_orders_in_month",
]

MEASURE_KEYS = {
    "q01_orders_by_month": ["orders"],
    "q02_revenue_by_store_by_month": ["revenue_usd"],
    "q03_item_revenue_by_product_type_by_month": ["item_revenue_usd"],
    "q04_aov_by_store": ["aov_usd"],
    "q05_orders_and_item_revenue_by_store_by_month": ["orders", "item_revenue_usd"],
    "q06_new_customer_orders_by_month": ["new_customer_orders"],
    "q07_delivered_revenue_by_month": ["delivered_revenue"],
    "q08_revenue_by_customer_segment_as_of_order_time": ["revenue_usd"],
    "q09_session_to_order_conversion_7d": ["session_to_order_conversion_rate_7d"],
    "q10_orders_from_customers_with_10plus_orders_in_month": ["qualifying_orders"],
}

DIMENSION_KEYS = {
    "q01_orders_by_month": ["month"],
    "q02_revenue_by_store_by_month": ["month", "store_name"],
    "q03_item_revenue_by_product_type_by_month": ["month", "product_type"],
    "q04_aov_by_store": ["store_name"],
    "q05_orders_and_item_revenue_by_store_by_month": ["month", "store_name"],
    "q06_new_customer_orders_by_month": ["month"],
    "q07_delivered_revenue_by_month": ["month"],
    "q08_revenue_by_customer_segment_as_of_order_time": ["month", "customer_segment"],
    "q09_session_to_order_conversion_7d": ["month"],
    "q10_orders_from_customers_with_10plus_orders_in_month": ["month"],
}


@dataclass(frozen=True)
class LayerQuestion:
    layer: str
    question_id: str
    status: str
    path: Path | None


LAYER_STATUS: dict[str, dict[str, str]] = {
    "semantic_rails": {
        entry["question_id"]: entry["status"]
        for entry in json.loads((RESULTS_ROOT / "semantic_rails" / "summary.json").read_text())[
            "questions"
        ]
    },
    "metricflow": {
        **{
            entry["question_id"]: entry["status"]
            for entry in json.loads((RESULTS_ROOT / "metricflow" / "summary.json").read_text())[
                "questions"
            ]
        },
        **{
            qid: item["status"]
            for qid, item in json.loads(
                (RESULTS_ROOT / "metricflow" / "unsupported.json").read_text()
            ).items()
        },
    },
    "cube": {
        **{
            entry["question_id"]: entry["status"]
            for entry in json.loads((RESULTS_ROOT / "cube" / "summary.json").read_text())[
                "questions"
            ]
        },
        **{
            qid: item["status"]
            for qid, item in json.loads(
                (RESULTS_ROOT / "cube" / "unsupported.json").read_text()
            ).items()
        },
    },
    "malloy": {
        entry["question_id"]: entry["status"]
        for entry in json.loads((RESULTS_ROOT / "malloy" / "summary.json").read_text())["questions"]
    },
}


def _semantic_rails_path(question_id: str) -> Path:
    return RESULTS_ROOT / "semantic_rails" / question_id / "result.json"


def _metricflow_path(question_id: str) -> Path | None:
    path = RESULTS_ROOT / "metricflow" / question_id / "result.csv"
    return path if path.exists() else None


def _cube_path(question_id: str) -> Path | None:
    path = RESULTS_ROOT / "cube" / question_id / "load.json"
    return path if path.exists() else None


def _malloy_path(question_id: str) -> Path | None:
    path = RESULTS_ROOT / "malloy" / question_id / "result.json"
    return path if path.exists() else None


def _normalize_month(value: Any) -> str | None:
    if value in ("", None):
        return None
    text = str(value)
    return text[:10]


def _normalize_scalar(value: Any) -> Any:
    if value == "":
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        try:
            return Decimal(value)
        except Exception:  # noqa: BLE001
            return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return value


def _canonicalize(question_id: str, row: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for target, source in mapping.items():
        value = row.get(source)
        canonical[target] = (
            _normalize_month(value) if target == "month" else _normalize_scalar(value)
        )
    return canonical


SEMANTIC_RAILS_MAPPING = {
    "q01_orders_by_month": {"month": "temporal_role.jaffle_order_time__month", "orders": "orders"},
    "q02_revenue_by_store_by_month": {
        "month": "temporal_role.jaffle_order_time__month",
        "store_name": "dimension.jaffle_store_name",
        "revenue_usd": "revenue_usd",
    },
    "q03_item_revenue_by_product_type_by_month": {
        "month": "temporal_role.jaffle_order_time__month",
        "product_type": "dimension.jaffle_item_product_type",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q04_aov_by_store": {"store_name": "dimension.jaffle_store_name", "aov_usd": "aov_usd"},
    "q05_orders_and_item_revenue_by_store_by_month": {
        "month": "temporal_role.jaffle_order_time__month",
        "store_name": "dimension.jaffle_store_name",
        "orders": "orders",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q06_new_customer_orders_by_month": {
        "month": "temporal_role.jaffle_order_time__month",
        "new_customer_orders": "new_customer_orders",
    },
    "q07_delivered_revenue_by_month": {
        "month": "temporal_role.jaffle_lifecycle_delivered_at__month",
        "delivered_revenue": "delivered_revenue",
    },
    "q08_revenue_by_customer_segment_as_of_order_time": {
        "month": "temporal_role.jaffle_order_time__month",
        "customer_segment": "dimension.jaffle_customer_history_segment",
        "revenue_usd": "revenue_usd",
    },
    "q09_session_to_order_conversion_7d": {
        "month": "temporal_role.jaffle_session_started_at__month",
        "session_to_order_conversion_rate_7d": "session_to_order_conversion_rate_7d",
    },
    "q10_orders_from_customers_with_10plus_orders_in_month": {
        "month": "temporal_role.jaffle_order_time__month",
        "qualifying_orders": "orders_from_customers_with_10plus_orders_in_period",
    },
}

METRICFLOW_MAPPING = {
    "q01_orders_by_month": {"month": "metric_time__month", "orders": "orders"},
    "q02_revenue_by_store_by_month": {
        "month": "metric_time__month",
        "store_name": "store__store_name",
        "revenue_usd": "revenue_usd",
    },
    "q03_item_revenue_by_product_type_by_month": {
        "month": "metric_time__month",
        "product_type": "item__product_type",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q04_aov_by_store": {"store_name": "store__store_name", "aov_usd": "aov_usd"},
    "q05_orders_and_item_revenue_by_store_by_month": {
        "month": "metric_time__month",
        "store_name": "store__store_name",
        "orders": "orders",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q06_new_customer_orders_by_month": {
        "month": "metric_time__month",
        "new_customer_orders": "new_customer_orders",
    },
    "q07_delivered_revenue_by_month": {
        "month": "metric_time__month",
        "delivered_revenue": "delivered_revenue",
    },
    "q08_revenue_by_customer_segment_as_of_order_time": {
        "month": "metric_time__month",
        "customer_segment": "customer__customer_segment",
        "revenue_usd": "revenue_usd",
    },
    "q09_session_to_order_conversion_7d": {
        "month": "metric_time__month",
        "session_to_order_conversion_rate_7d": "session_to_order_conversion_rate_7d",
    },
}

CUBE_MAPPING = {
    "q01_orders_by_month": {"month": "orders.ordered_at.month", "orders": "orders.orders"},
    "q02_revenue_by_store_by_month": {
        "month": "orders.ordered_at.month",
        "store_name": "stores.store_name",
        "revenue_usd": "orders.revenue_usd",
    },
    "q03_item_revenue_by_product_type_by_month": {
        "month": "order_items.ordered_at.month",
        "product_type": "order_items.product_type",
        "item_revenue_usd": "order_items.item_revenue_usd",
    },
    "q04_aov_by_store": {"store_name": "stores.store_name", "aov_usd": "orders.aov_usd"},
    "q06_new_customer_orders_by_month": {
        "month": "orders.ordered_at.month",
        "new_customer_orders": "orders.new_customer_orders",
    },
    "q07_delivered_revenue_by_month": {
        "month": "order_lifecycle.delivered_at.month",
        "delivered_revenue": "order_lifecycle.delivered_revenue",
    },
    "q08_revenue_by_customer_segment_as_of_order_time": {
        "month": "orders.ordered_at.month",
        "customer_segment": "customer_history.customer_segment",
        "revenue_usd": "orders.revenue_usd",
    },
    "q09_session_to_order_conversion_7d": {
        "month": "storefront_sessions.started_at.month",
        "session_to_order_conversion_rate_7d": "storefront_sessions.session_to_order_conversion_rate_7d",
    },
}

MALLOY_MAPPING = {
    "q01_orders_by_month": {"month": "ordered_month", "orders": "orders"},
    "q02_revenue_by_store_by_month": {
        "month": "ordered_month",
        "store_name": "store_name",
        "revenue_usd": "revenue_usd",
    },
    "q03_item_revenue_by_product_type_by_month": {
        "month": "ordered_month",
        "product_type": "product_type",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q04_aov_by_store": {"store_name": "store_name", "aov_usd": "aov_usd"},
    "q05_orders_and_item_revenue_by_store_by_month": {
        "month": "ordered_month",
        "store_name": "store_name",
        "orders": "orders",
        "item_revenue_usd": "item_revenue_usd",
    },
    "q06_new_customer_orders_by_month": {
        "month": "ordered_month",
        "new_customer_orders": "new_customer_orders",
    },
    "q07_delivered_revenue_by_month": {
        "month": "delivered_month",
        "delivered_revenue": "delivered_revenue_usd",
    },
    "q08_revenue_by_customer_segment_as_of_order_time": {
        "month": "ordered_month",
        "customer_segment": "customer_segment",
        "revenue_usd": "revenue_total_usd",
    },
    "q09_session_to_order_conversion_7d": {
        "month": "session_month",
        "session_to_order_conversion_rate_7d": "conversion_rate_7d",
    },
    "q10_orders_from_customers_with_10plus_orders_in_month": {
        "month": "ordered_month",
        "qualifying_orders": "qualifying_orders",
    },
}


def load_semantic_rails(question_id: str) -> list[dict[str, Any]]:
    payload = json.loads(_semantic_rails_path(question_id).read_text())
    return [
        _canonicalize(question_id, row, SEMANTIC_RAILS_MAPPING[question_id])
        for row in payload["rows"]
    ]


def load_metricflow(question_id: str) -> list[dict[str, Any]]:
    path = _metricflow_path(question_id)
    if path is None:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [_canonicalize(question_id, row, METRICFLOW_MAPPING[question_id]) for row in rows]


def load_cube(question_id: str) -> list[dict[str, Any]]:
    path = _cube_path(question_id)
    if path is None:
        return []
    payload = json.loads(path.read_text())
    return [_canonicalize(question_id, row, CUBE_MAPPING[question_id]) for row in payload["data"]]


def load_malloy(question_id: str) -> list[dict[str, Any]]:
    path = _malloy_path(question_id)
    if path is None:
        return []
    payload = json.loads(path.read_text())
    return [_canonicalize(question_id, row, MALLOY_MAPPING[question_id]) for row in payload]


LOADERS = {
    "semantic_rails": load_semantic_rails,
    "metricflow": load_metricflow,
    "cube": load_cube,
    "malloy": load_malloy,
}


def sort_rows(question_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dims = DIMENSION_KEYS[question_id]
    return sorted(
        rows,
        key=lambda row: tuple("" if row.get(key) is None else str(row.get(key)) for key in dims),
    )


def rows_equal(
    question_id: str, left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[bool, list[dict[str, Any]]]:
    left_sorted = sort_rows(question_id, left)
    right_sorted = sort_rows(question_id, right)
    diffs: list[dict[str, Any]] = []
    max_len = max(len(left_sorted), len(right_sorted))
    for index in range(max_len):
        left_row = left_sorted[index] if index < len(left_sorted) else None
        right_row = right_sorted[index] if index < len(right_sorted) else None
        if left_row != right_row:
            diffs.append({"index": index, "reference": left_row, "candidate": right_row})
    return not diffs, diffs[:5]


def main() -> None:
    report: dict[str, Any] = {"reference_layer": "semantic_rails", "questions": []}
    md_lines = [
        "# Determinism Report",
        "",
        "Semantic Rails is treated as the reference implementation for row-level equality.",
        "",
        "| Question | MetricFlow | Cube | Malloy |",
        "| --- | --- | --- | --- |",
    ]

    for question_id in QUESTION_ORDER:
        reference = load_semantic_rails(question_id)
        question_report: dict[str, Any] = {
            "question_id": question_id,
            "reference_rows": len(reference),
            "comparisons": {},
        }

        row_cells = [question_id]
        for layer in ["metricflow", "cube", "malloy"]:
            status = LAYER_STATUS[layer].get(question_id, "unsupported")
            if status == "unsupported":
                question_report["comparisons"][layer] = {"status": "unsupported"}
                row_cells.append("unsupported")
                continue

            candidate = LOADERS[layer](question_id)
            equal, diffs = rows_equal(question_id, reference, candidate)
            comparison_status = "match" if equal else "mismatch"
            question_report["comparisons"][layer] = {
                "status": comparison_status,
                "reference_rows": len(reference),
                "candidate_rows": len(candidate),
                "sample_diffs": diffs,
                "layer_support_status": status,
            }
            row_cells.append(comparison_status)

        report["questions"].append(question_report)
        md_lines.append(f"| {' | '.join(row_cells)} |")

    REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    REPORT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT_JSON.relative_to(REPO_ROOT)}")
    print(f"Wrote {REPORT_MD.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
