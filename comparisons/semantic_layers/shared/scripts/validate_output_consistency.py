from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SHARED_ROOT = REPO_ROOT / "comparisons" / "semantic_layers" / "shared"
RESULTS_ROOT = SHARED_ROOT / "results"
QUESTIONS_PATH = SHARED_ROOT / "questions.yml"
OUTPUT_DIR = RESULTS_ROOT / "validation"

RUNNABLE_LAYERS = [
    "semantic_rails",
    "metricflow",
    "cube",
    "malloy",
    "snowflake_semantic_views",
    "ktx",
]
DECIMAL_TOLERANCE = Decimal("0.000001")
NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

QUESTION_FIELDS = {
    "q01_orders_by_month": ["month", "orders"],
    "q02_revenue_by_store_by_month": ["month", "store_name", "revenue_usd"],
    "q03_item_revenue_by_product_type_by_month": ["month", "product_type", "item_revenue_usd"],
    "q04_aov_by_store": ["store_name", "aov_usd"],
    "q05_orders_and_item_revenue_by_store_by_month": [
        "month",
        "store_name",
        "orders",
        "item_revenue_usd",
    ],
    "q06_new_customer_orders_by_month": ["month", "new_customer_orders"],
    "q07_delivered_revenue_by_month": ["month", "delivered_revenue"],
    "q08_revenue_by_customer_segment_as_of_order_time": [
        "month",
        "customer_segment",
        "revenue_usd",
    ],
    "q09_session_to_order_conversion_7d": ["month", "session_to_order_conversion_rate_7d"],
    "q10_orders_from_customers_with_10plus_orders_in_month": ["month", "qualifying_orders"],
    "q11_repeat_customer_orders_by_store_by_month": [
        "month",
        "store_name",
        "repeat_customer_orders",
    ],
    "q12_orders_by_month_with_lifetime_spend_500_filter": ["month", "filtered_orders"],
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": ["day", "qualifying_orders"],
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": [
        "month",
        "store_name",
        "qualifying_revenue_usd",
    ],
    "q15_same_store_session_to_order_conversion_7d": ["month", "same_store_conversion_rate_7d"],
    "q16_revenue_by_customer_segment_as_of_delivered_time": [
        "month",
        "customer_segment",
        "delivered_revenue",
    ],
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_questions() -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return {question["id"]: question for question in payload["questions"]}


def _load_summary(layer: str) -> dict[str, Any]:
    return _read_json(RESULTS_ROOT / layer / "summary.json")


def _rows_for(layer: str, result_path: str) -> list[dict[str, Any]]:
    path = REPO_ROOT / result_path
    if layer == "metricflow":
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    payload = _read_json(path)
    if layer == "semantic_rails":
        return payload.get("rows", [])
    if layer == "cube":
        return payload.get("data", [])
    if layer in {"malloy", "snowflake_semantic_views", "ktx"}:
        return payload
    raise ValueError(f"Unsupported layer: {layer}")


def _find_key(row: dict[str, Any], canonical_field: str) -> str | None:
    keys = list(row)

    def first(predicate) -> str | None:
        for key in keys:
            if predicate(key.lower()):
                return key
        return None

    if canonical_field == "month":
        return first(lambda key: key == "metric_time__month") or first(
            lambda key: key.endswith(".month") or key.endswith("__month") or key.endswith("_month")
        )
    if canonical_field == "day":
        return first(lambda key: key == "metric_time__day") or first(
            lambda key: key.endswith(".day") or key.endswith("__day") or key.endswith("_day")
        )
    if canonical_field == "store_name":
        return first(lambda key: "store_name" in key)
    if canonical_field == "product_type":
        return first(lambda key: "product_type" in key)
    if canonical_field == "customer_segment":
        return first(lambda key: "customer_segment" in key or key.endswith("_segment"))
    if canonical_field == "orders":
        return first(
            lambda key: (
                (key == "orders" or key.endswith(".orders"))
                and "new_customer" not in key
                and "qualifying" not in key
                and "orders_from_customers_with_10plus_orders" not in key
            )
        )
    if canonical_field == "new_customer_orders":
        return first(lambda key: "new_customer_orders" in key)
    if canonical_field == "revenue_usd":
        return first(
            lambda key: (
                "revenue" in key and "item_revenue" not in key and "delivered_revenue" not in key
            )
        )
    if canonical_field == "item_revenue_usd":
        return first(lambda key: "item_revenue" in key)
    if canonical_field == "aov_usd":
        return first(lambda key: "aov" in key)
    if canonical_field == "delivered_revenue":
        return first(lambda key: "delivered_revenue" in key)
    if canonical_field == "session_to_order_conversion_rate_7d":
        return first(
            lambda key: "conversion_rate" in key or "session_to_order_conversion_rate" in key
        )
    if canonical_field == "qualifying_orders":
        return first(
            lambda key: (
                "qualifying_orders" in key or "orders_from_customers_with_10plus_orders" in key
            )
        )
    if canonical_field == "repeat_customer_orders":
        return first(lambda key: "repeat_customer_orders" in key) or first(
            lambda key: (key == "orders" or key.endswith(".orders")) and "new_customer" not in key
        )
    if canonical_field == "filtered_orders":
        return (
            first(lambda key: "filtered_orders" in key)
            or first(lambda key: "lifetime_spend_500" in key)
            or first(lambda key: key == "orders" or key.endswith(".orders"))
        )
    if canonical_field == "qualifying_revenue_usd":
        return first(lambda key: "qualifying_revenue" in key) or first(
            lambda key: key.endswith(".revenue_usd") or key == "revenue_usd"
        )
    if canonical_field == "same_store_conversion_rate_7d":
        return first(lambda key: "same_store" in key and "conversion_rate" in key) or first(
            lambda key: "same_store_session_to_order_conversion_rate" in key
        )
    raise ValueError(f"Unsupported canonical field: {canonical_field}")


def _normalize_month(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    if " " in text:
        return datetime.fromisoformat(text).date().isoformat()
    return text[:10]


def _normalize_number(value: Any) -> int | float | Decimal | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        decimal_value = Decimal(str(value))
    else:
        text = str(value).strip()
        if not NUMERIC_RE.match(text):
            return None
        try:
            decimal_value = Decimal(text)
        except InvalidOperation:
            return None
    return (
        int(decimal_value)
        if decimal_value == decimal_value.to_integral_value()
        else decimal_value.normalize()
    )


def _normalize_scalar(field: str, value: Any) -> Any:
    if field in {"month", "day"}:
        return _normalize_month(value)
    if field in {
        "orders",
        "new_customer_orders",
        "revenue_usd",
        "item_revenue_usd",
        "aov_usd",
        "delivered_revenue",
        "session_to_order_conversion_rate_7d",
        "qualifying_orders",
        "repeat_customer_orders",
        "filtered_orders",
        "qualifying_revenue_usd",
        "same_store_conversion_rate_7d",
    }:
        number = _normalize_number(value)
        return number if number is not None else value
    if value == "":
        return None
    return value


def _normalize_rows(question_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical_fields = QUESTION_FIELDS[question_id]
    normalized: list[dict[str, Any]] = []
    for row in rows:
        canonical_row: dict[str, Any] = {}
        for field in canonical_fields:
            key = _find_key(row, field)
            canonical_row[field] = _normalize_scalar(field, row.get(key) if key else None)
        normalized.append(canonical_row)
    return sorted(normalized, key=lambda item: json.dumps(_json_safe(item), sort_keys=True))


def _value_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        try:
            return abs(Decimal(str(left)) - Decimal(str(right))) <= DECIMAL_TOLERANCE
        except InvalidOperation:
            return left == right
    return left == right


def _rows_equal(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> tuple[bool, dict[str, Any] | None]:
    if len(left) != len(right):
        return False, {"type": "row_count", "left": len(left), "right": len(right)}
    for index, (left_row, right_row) in enumerate(zip(left, right, strict=True)):
        keys = sorted(set(left_row) | set(right_row))
        differing = [key for key in keys if not _value_equal(left_row.get(key), right_row.get(key))]
        if differing:
            return False, {
                "type": "row_value",
                "index": index,
                "fields": differing,
                "left_row": _json_safe(left_row),
                "right_row": _json_safe(right_row),
            }
    return True, None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def main() -> None:
    questions = _load_questions()
    summaries = {layer: _load_summary(layer) for layer in RUNNABLE_LAYERS}

    results: list[dict[str, Any]] = []
    summary_counts = {"matched": 0, "mismatched": 0, "not_comparable": 0}

    for question_id, metadata in questions.items():
        comparable_layers: list[str] = []
        layer_statuses: dict[str, str] = {}
        normalized_rows_by_layer: dict[str, list[dict[str, Any]]] = {}

        for layer in RUNNABLE_LAYERS:
            entry = next(
                item for item in summaries[layer]["questions"] if item["question_id"] == question_id
            )
            status = entry["status"]
            layer_statuses[layer] = status
            if status == "unsupported":
                continue
            comparable_layers.append(layer)
            normalized_rows_by_layer[layer] = _normalize_rows(
                question_id, _rows_for(layer, entry["result_path"])
            )

        question_result: dict[str, Any] = {
            "question_id": question_id,
            "title": metadata["title"],
            "layer_statuses": layer_statuses,
            "comparable_layers": comparable_layers,
        }

        if len(comparable_layers) < 2:
            question_result["comparison_status"] = "not_comparable"
            question_result["reason"] = "Fewer than two runnable layers executed this question."
            summary_counts["not_comparable"] += 1
        else:
            reference_rows = normalized_rows_by_layer["semantic_rails"]
            mismatches: list[dict[str, Any]] = []
            for layer in comparable_layers:
                if layer == "semantic_rails":
                    continue
                equal, detail = _rows_equal(reference_rows, normalized_rows_by_layer[layer])
                if not equal:
                    mismatches.append(
                        {
                            "layer": layer,
                            "detail": detail,
                        }
                    )
            if mismatches:
                question_result["comparison_status"] = "mismatched"
                question_result["mismatches"] = mismatches
                summary_counts["mismatched"] += 1
            else:
                question_result["comparison_status"] = "matched"
                summary_counts["matched"] += 1

        question_result["normalized_rows"] = {
            layer: _json_safe(rows) for layer, rows in normalized_rows_by_layer.items()
        }
        results.append(question_result)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reference_layer": "semantic_rails",
        "summary": summary_counts,
        "questions": results,
    }
    (OUTPUT_DIR / "output_consistency.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

    markdown_lines = [
        "# Output Consistency",
        "",
        f"Generated at `{report['generated_at']}` using `semantic_rails` as the reference layer.",
        "",
        f"- Matched: `{summary_counts['matched']}`",
        f"- Mismatched: `{summary_counts['mismatched']}`",
        f"- Not comparable: `{summary_counts['not_comparable']}`",
        "",
    ]

    for item in results:
        statuses = ", ".join(
            f"{layer}={status}" for layer, status in item["layer_statuses"].items()
        )
        markdown_lines.append(f"## {item['question_id']} {item['title']}")
        markdown_lines.append("")
        markdown_lines.append(f"- Layer statuses: `{statuses}`")
        markdown_lines.append(f"- Comparison status: `{item['comparison_status']}`")
        if item["comparison_status"] == "mismatched":
            for mismatch in item["mismatches"]:
                markdown_lines.append(
                    f"- Mismatch vs semantic_rails on `{mismatch['layer']}`: `{json.dumps(mismatch['detail'], sort_keys=True)}`"
                )
        elif item["comparison_status"] == "not_comparable":
            markdown_lines.append(f"- Reason: {item['reason']}")
        else:
            markdown_lines.append(f"- Comparable layers: `{', '.join(item['comparable_layers'])}`")
        markdown_lines.append("")

    (OUTPUT_DIR / "output_consistency.md").write_text("\n".join(markdown_lines), encoding="utf-8")
    print(f"Wrote consistency report to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
