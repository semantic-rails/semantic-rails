from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from bootstrap_shared_duckdb import dataset_fingerprint
from run_oracle import answer_key_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[4]
SHARED_ROOT = REPO_ROOT / "comparisons" / "semantic_layers" / "shared"
RESULTS_ROOT = SHARED_ROOT / "results"
QUESTIONS_PATH = SHARED_ROOT / "questions.yml"
ORACLE_DIR = SHARED_ROOT / "oracle"
OUTPUT_DIR = RESULTS_ROOT / "validation"

RUNNABLE_LAYERS = [
    "semantic_rails",
    "metricflow",
    "cube",
    "malloy",
    "snowflake_semantic_views",
    "ktx",
]
# The reference is the independent answer key (shared/oracle/), not one of the layers.
ANSWER_KEY = "answer_key"
COLUMN_MAPS_PATH = SHARED_ROOT / "column_maps.yml"
RESULT_DIRS = {layer: layer for layer in RUNNABLE_LAYERS} | {ANSWER_KEY: "oracle"}
DECIMAL_TOLERANCE = Decimal("0.000001")
NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
# Published scoring keeps the 7 shared questions apart from the 9 that were chosen to
# exercise Semantic Rails features, and both apart from the 8 frozen-model variants
# (questions.yml `scope_level`).
SLICE_BY_SCOPE = {
    "required": "shared",
    "stretch": "semantic_rails_targeted",
    "variant": "frozen_model",
}

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
    "q17_session_to_order_conversion_14d": ["month", "session_to_order_conversion_rate_14d"],
    "q18_same_store_session_to_order_conversion_50m": ["month", "same_store_conversion_rate_50m"],
    "q19_trailing_3_month_revenue_by_month": ["month", "trailing_3_month_revenue_usd"],
    "q20_revenue_and_prior_month_revenue_by_month": [
        "month",
        "revenue_usd",
        "prior_month_revenue_usd",
    ],
    "q21_revenue_and_large_order_revenue_by_month": [
        "month",
        "revenue_usd",
        "large_order_revenue_usd",
    ],
    "q22_average_and_max_item_revenue_by_product_type_by_month": [
        "month",
        "product_type",
        "avg_item_revenue_usd",
        "max_item_revenue_usd",
    ],
    "q23_orders_from_customers_with_5plus_orders_in_month": ["month", "qualifying_orders"],
    "q24_orders_by_month_with_lifetime_spend_1000_filter": ["month", "filtered_orders"],
}
# Fields that aren't numbers: everything else is compared as a number.
TEXT_FIELDS = {"month", "day", "store_name", "product_type", "customer_segment"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_questions() -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = {question["id"]: question for question in payload["questions"]}
    unknown = sorted(
        qid
        for qid, question in questions.items()
        if question.get("scope_level") not in SLICE_BY_SCOPE
    )
    if unknown:
        raise SystemExit(
            f"questions.yml has a scope_level outside {sorted(SLICE_BY_SCOPE)}: {unknown}"
        )
    return questions


def _load_summary(layer: str) -> dict[str, Any]:
    return _read_json(RESULTS_ROOT / RESULT_DIRS[layer] / "summary.json")


def _rows_for(layer: str, result_path: str) -> list[dict[str, Any]]:
    path = REPO_ROOT / result_path
    if layer == "metricflow":
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    payload = _read_json(path)
    if layer == ANSWER_KEY:
        return payload
    if layer == "semantic_rails":
        return payload.get("rows", [])
    if layer == "cube":
        return payload.get("data", [])
    if layer in {"malloy", "snowflake_semantic_views", "ktx"}:
        return payload
    raise ValueError(f"Unsupported layer: {layer}")


def _load_column_maps() -> dict[str, dict[str, dict[str, str]]]:
    """Which result column holds each field, per layer and question (no name guessing).

    A layer maps each question it answers; a question it executed without a map fails the check."""
    maps = yaml.safe_load(COLUMN_MAPS_PATH.read_text(encoding="utf-8"))
    for layer in RUNNABLE_LAYERS:
        for question_id, columns in maps.get(layer, {}).items():
            fields = sorted(QUESTION_FIELDS.get(question_id, []))
            if sorted(columns) != fields:
                raise SystemExit(
                    f"column_maps.yml: {layer} {question_id} maps {sorted(columns)}, expected {fields}"
                )
    return maps


def _columns(maps: dict[str, dict[str, dict[str, str]]], layer: str, question_id: str) -> dict:
    columns = maps.get(layer, {}).get(question_id)
    if columns is None:
        raise SystemExit(f"column_maps.yml has no map for {layer} {question_id}, which it executed")
    return columns


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
    if field not in TEXT_FIELDS:
        number = _normalize_number(value)
        return number if number is not None else value
    if value == "":
        return None
    return value


def _normalize_rows(
    question_id: str, rows: list[dict[str, Any]], columns: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Read each field from its mapped column; the answer key uses the field names directly."""
    canonical_fields = QUESTION_FIELDS[question_id]
    normalized: list[dict[str, Any]] = []
    for row in rows:
        canonical_row: dict[str, Any] = {}
        for field in canonical_fields:
            key = columns[field] if columns else field
            if key not in row:
                raise SystemExit(
                    f"{question_id}: column {key!r} for {field!r} is missing; the row has {sorted(row)}"
                )
            canonical_row[field] = _normalize_scalar(field, row[key])
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


def _agreement_groups(rows_by_layer: dict[str, list[dict[str, Any]]]) -> list[list[str]]:
    """Group layers whose normalized rows are equal, so a report says who disagrees with whom.

    Equality within a tolerance isn't transitive, so a layer joins a group only if it equals
    every member: every advertised group agrees pairwise.
    """
    groups: list[list[str]] = []
    for layer, rows in rows_by_layer.items():
        for group in groups:
            if all(_rows_equal(rows_by_layer[member], rows)[0] for member in group):
                group.append(layer)
                break
        else:
            groups.append([layer])
    return groups


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
    column_maps = _load_column_maps()
    summaries = {layer: _load_summary(layer) for layer in [*RUNNABLE_LAYERS, ANSWER_KEY]}
    # A capture made on other data can't be compared like for like: report it separately.
    current_dataset = dataset_fingerprint()
    if summaries[ANSWER_KEY].get("dataset_fingerprint") != current_dataset:
        raise SystemExit("The answer key predates the current dataset; run run_oracle.py first.")
    if summaries[ANSWER_KEY].get("answer_key_fingerprint") != answer_key_fingerprint(
        ORACLE_DIR, QUESTIONS_PATH
    ):
        raise SystemExit(
            "The answer key's queries or the questions changed since it last ran; "
            "run run_oracle.py first."
        )
    stale_layers = [
        layer
        for layer in RUNNABLE_LAYERS
        if summaries[layer].get("dataset_fingerprint") != current_dataset
    ]
    stale_checks: dict[str, dict[str, list[str]]] = {
        layer: {"matched": [], "mismatched": []} for layer in stale_layers
    }

    results: list[dict[str, Any]] = []
    summary_counts = {"matched": 0, "mismatched": 0, "not_comparable": 0}

    for question_id, metadata in questions.items():
        comparable_layers: list[str] = []
        layer_statuses: dict[str, str] = {}
        key_entry = next(
            item
            for item in summaries[ANSWER_KEY]["questions"]
            if item["question_id"] == question_id
        )
        key_rows = _normalize_rows(question_id, _rows_for(ANSWER_KEY, key_entry["result_path"]))
        normalized_rows_by_layer: dict[str, list[dict[str, Any]]] = {ANSWER_KEY: key_rows}

        for layer in RUNNABLE_LAYERS:
            entry = next(
                (
                    item
                    for item in summaries[layer]["questions"]
                    if item["question_id"] == question_id
                ),
                None,
            )
            # Only whether it ran: support labels come from the rubric, and a pinned capture's
            # summary may still carry old hand labels. A layer with no entry didn't attempt the
            # question: a frozen-model variant it can't express, or a capture that can't re-run.
            if entry is None:
                status = "not_run"
            else:
                status = "unsupported" if entry["status"] == "unsupported" else "executed"
            layer_statuses[layer] = status
            if status != "executed":
                continue
            comparable_layers.append(layer)
            normalized_rows_by_layer[layer] = _normalize_rows(
                question_id,
                _rows_for(layer, entry["result_path"]),
                _columns(column_maps, layer, question_id),
            )

        question_result: dict[str, Any] = {
            "question_id": question_id,
            "title": metadata["title"],
            "slice": SLICE_BY_SCOPE[metadata["scope_level"]],
            "layer_statuses": layer_statuses,
            "comparable_layers": comparable_layers,
        }

        current_layers = [layer for layer in comparable_layers if layer not in stale_layers]
        question_result["current_layers"] = current_layers
        mismatches: list[dict[str, Any]] = []
        for layer in comparable_layers:
            equal, detail = _rows_equal(key_rows, normalized_rows_by_layer[layer])
            if layer in stale_layers:
                stale_checks[layer]["matched" if equal else "mismatched"].append(question_id)
                question_result.setdefault("stale_captures", {})[layer] = (
                    "matched" if equal else "mismatched"
                )
            elif not equal:
                mismatches.append({"layer": layer, "detail": detail})
        if not current_layers:
            question_result["comparison_status"] = "not_comparable"
            question_result["reason"] = "No layer executed this question on the current dataset."
            summary_counts["not_comparable"] += 1
        elif mismatches:
            question_result["comparison_status"] = "mismatched"
            question_result["mismatches"] = mismatches
            question_result["agreement_groups"] = _agreement_groups(
                {layer: normalized_rows_by_layer[layer] for layer in [ANSWER_KEY, *current_layers]}
            )
            summary_counts["mismatched"] += 1
        else:
            question_result["comparison_status"] = "matched"
            summary_counts["matched"] += 1

        question_result["normalized_rows"] = {
            layer: _json_safe(rows) for layer, rows in normalized_rows_by_layer.items()
        }
        results.append(question_result)

    summary_by_slice: dict[str, dict[str, Any]] = {}
    for slice_name in SLICE_BY_SCOPE.values():
        members = [item for item in results if item["slice"] == slice_name]
        summary_by_slice[slice_name] = {
            "questions": len(members),
            **{
                status: sum(1 for item in members if item["comparison_status"] == status)
                for status in summary_counts
            },
            "mismatched_questions": [
                item["question_id"] for item in members if item["comparison_status"] == "mismatched"
            ],
        }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reference_layer": ANSWER_KEY,
        "dataset_fingerprint": current_dataset,
        "summary": summary_counts,
        "stale_layers": {
            layer: {"captured": summaries[layer].get("generated_at"), **checks}
            for layer, checks in stale_checks.items()
        },
        "summary_by_slice": summary_by_slice,
        "questions": results,
    }
    (OUTPUT_DIR / "output_consistency.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

    markdown_lines = [
        "# Output Consistency",
        "",
        f"Generated at `{report['generated_at']}` on dataset `{current_dataset[:12]}`. Every "
        "layer is compared with the independent answer key in `shared/oracle/`.",
        "",
        f"- Matched: `{summary_counts['matched']}`",
        f"- Mismatched: `{summary_counts['mismatched']}`",
        f"- Not comparable: `{summary_counts['not_comparable']}`",
        "",
    ]
    for slice_name, counts in summary_by_slice.items():
        mismatched = ", ".join(f"`{qid}`" for qid in counts["mismatched_questions"]) or "none"
        markdown_lines.append(
            f"- `{slice_name}`: {counts['matched']} of {counts['questions']} matched; "
            f"mismatched: {mismatched}"
        )
    markdown_lines.append("")
    for layer, checks in report["stale_layers"].items():
        mismatched = ", ".join(f"`{qid}`" for qid in checks["mismatched"]) or "none"
        markdown_lines.append(
            f"- Stale capture, excluded from the counts above: `{layer}` "
            f"(captured {checks['captured'] or 'on an earlier dataset'}) matches "
            f"{len(checks['matched'])} questions; mismatched: {mismatched}"
        )
    markdown_lines.append("")

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
                    f"- `{mismatch['layer']}` differs from the answer key: `{json.dumps(mismatch['detail'], sort_keys=True)}`"
                )
            groups = " | ".join(", ".join(group) for group in item["agreement_groups"])
            markdown_lines.append(f"- Groups of layers whose outputs match within 1e-6: `{groups}`")
        elif item["comparison_status"] == "not_comparable":
            markdown_lines.append(f"- Reason: {item['reason']}")
        else:
            markdown_lines.append(f"- Layers compared: `{', '.join(item['current_layers'])}`")
        for layer, status in item.get("stale_captures", {}).items():
            markdown_lines.append(f"- Stale capture, not counted: `{layer}` {status}")
        markdown_lines.append("")

    (OUTPUT_DIR / "output_consistency.md").write_text("\n".join(markdown_lines), encoding="utf-8")
    print(f"Wrote consistency report to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
