from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "ktx"
SOURCES_DIR = PROJECT_DIR / "sources"
QUERIES_DIR = PROJECT_DIR / "queries"
RESULTS_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results" / "ktx"
DB_PATH = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "data" / "jaffle_comparison.duckdb"
)

KTX_SL_PATH = Path(os.environ.get("KTX_SL_PATH", "/tmp/ktx-compare/python/ktx-sl"))
if str(KTX_SL_PATH) not in sys.path:
    sys.path.insert(0, str(KTX_SL_PATH))

try:
    from semantic_layer.engine import SemanticEngine
    from semantic_layer.loader import SourceLoader
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Could not import KtX ktx-sl. Clone https://github.com/Kaelio/ktx to "
        "/tmp/ktx-compare or set KTX_SL_PATH, then run with "
        "`uv run --with sqlglot --with pydantic --with pyyaml ...`."
    ) from exc


QUESTION_STATUSES = {
    "q01_orders_by_month": "native",
    "q02_revenue_by_store_by_month": "native",
    "q03_item_revenue_by_product_type_by_month": "native",
    "q04_aov_by_store": "native",
    "q05_orders_and_item_revenue_by_store_by_month": "native",
    "q06_new_customer_orders_by_month": "native",
    "q07_delivered_revenue_by_month": "native",
    "q08_revenue_by_customer_segment_as_of_order_time": "workaround",
    "q09_session_to_order_conversion_7d": "workaround",
    "q10_orders_from_customers_with_10plus_orders_in_month": "workaround",
    "q11_repeat_customer_orders_by_store_by_month": "workaround",
    "q12_orders_by_month_with_lifetime_spend_500_filter": "workaround",
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": "workaround",
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": "workaround",
    "q15_same_store_session_to_order_conversion_7d": "workaround",
    "q16_revenue_by_customer_segment_as_of_delivered_time": "workaround",
}

QUERIES: dict[str, dict[str, Any]] = {
    "q01_orders_by_month": {
        "measures": ["orders.orders"],
        "dimensions": [{"field": "orders.ordered_at", "granularity": "month"}],
    },
    "q02_revenue_by_store_by_month": {
        "measures": ["orders.revenue_usd"],
        "dimensions": [
            {"field": "orders.ordered_at", "granularity": "month"},
            "stores.store_name",
        ],
    },
    "q03_item_revenue_by_product_type_by_month": {
        "measures": ["order_items.item_revenue_usd"],
        "dimensions": [
            {"field": "order_items.ordered_at", "granularity": "month"},
            "order_items.product_type",
        ],
    },
    "q04_aov_by_store": {
        "measures": ["orders.aov_usd"],
        "dimensions": ["stores.store_name"],
    },
    "q05_orders_and_item_revenue_by_store_by_month": {
        "measures": ["orders.orders", "order_items.item_revenue_usd"],
        "dimensions": [
            {"field": "orders.ordered_at", "granularity": "month"},
            "stores.store_name",
        ],
    },
    "q06_new_customer_orders_by_month": {
        "measures": ["orders.new_customer_orders"],
        "dimensions": [{"field": "orders.ordered_at", "granularity": "month"}],
    },
    "q07_delivered_revenue_by_month": {
        "measures": ["order_lifecycle.delivered_revenue"],
        "dimensions": [{"field": "order_lifecycle.delivered_at", "granularity": "month"}],
    },
    "q08_revenue_by_customer_segment_as_of_order_time": {
        "measures": ["revenue_by_customer_segment_order_time.revenue_usd"],
        "dimensions": [
            {
                "field": "revenue_by_customer_segment_order_time.ordered_month",
                "granularity": "month",
            },
            "revenue_by_customer_segment_order_time.customer_segment",
        ],
    },
    "q09_session_to_order_conversion_7d": {
        "measures": ["session_conversion_7d.session_to_order_conversion_rate_7d"],
        "dimensions": [{"field": "session_conversion_7d.session_month", "granularity": "month"}],
    },
    "q10_orders_from_customers_with_10plus_orders_in_month": {
        "measures": ["orders_from_high_frequency_customers_month.qualifying_orders"],
        "dimensions": [
            {
                "field": "orders_from_high_frequency_customers_month.ordered_month",
                "granularity": "month",
            }
        ],
    },
    "q11_repeat_customer_orders_by_store_by_month": {
        "measures": ["orders.orders"],
        "dimensions": [
            {"field": "orders.ordered_at", "granularity": "month"},
            "stores.store_name",
        ],
        "filters": ["customers.lifetime_order_count > 1"],
    },
    "q12_orders_by_month_with_lifetime_spend_500_filter": {
        "measures": ["orders.orders"],
        "dimensions": [{"field": "orders.ordered_at", "granularity": "month"}],
        "filters": ["customers.lifetime_spend_cents >= 50000"],
    },
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": {
        "measures": ["daily_orders_from_high_frequency_customers_month.qualifying_orders"],
        "dimensions": [
            {
                "field": "daily_orders_from_high_frequency_customers_month.ordered_day",
                "granularity": "day",
            }
        ],
    },
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": {
        "measures": ["revenue_from_high_frequency_store_customers.qualifying_revenue_usd"],
        "dimensions": [
            {
                "field": "revenue_from_high_frequency_store_customers.ordered_month",
                "granularity": "month",
            },
            "revenue_from_high_frequency_store_customers.store_name",
        ],
    },
    "q15_same_store_session_to_order_conversion_7d": {
        "measures": ["session_conversion_7d_same_store.same_store_conversion_rate_7d"],
        "dimensions": [
            {"field": "session_conversion_7d_same_store.session_month", "granularity": "month"}
        ],
    },
    "q16_revenue_by_customer_segment_as_of_delivered_time": {
        "measures": ["delivered_revenue_by_customer_segment.delivered_revenue"],
        "dimensions": [
            {
                "field": "delivered_revenue_by_customer_segment.delivered_month",
                "granularity": "month",
            },
            "delivered_revenue_by_customer_segment.customer_segment",
        ],
    },
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_sql(sql: str) -> list[dict[str, Any]]:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        cursor = con.execute(sql)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
    finally:
        con.close()


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(
            "Shared DuckDB file is missing. Run "
            "`PYTHONPATH=. uv run python comparisons/semantic_layers/shared/scripts/bootstrap_shared_duckdb.py`."
        )

    if RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    QUERIES_DIR.mkdir(parents=True, exist_ok=True)

    sources = SourceLoader(SOURCES_DIR).load_all()
    validation = SemanticEngine.from_sources(sources, dialect="duckdb").validate()
    _write(
        RESULTS_DIR / "validate.json",
        json.dumps(validation.model_dump(mode="json"), indent=2, sort_keys=True),
    )
    if validation.errors:
        raise SystemExit(f"KtX source validation failed: {validation.errors}")

    engine = SemanticEngine.from_sources(sources, dialect="duckdb")
    summary: list[dict[str, Any]] = []

    for question_id, query in QUERIES.items():
        query_file = QUERIES_DIR / f"{question_id}.json"
        target_dir = RESULTS_DIR / question_id
        query_payload = {**query, "include_empty": False, "limit": 5000}
        _write(query_file, json.dumps(query_payload, indent=2, sort_keys=True))

        result_rows: list[dict[str, Any]] = []
        compile_error = ""
        run_error = ""
        execution_status = True
        compiled_sql = ""
        try:
            compiled = engine.query(query_payload)
            compiled_sql = compiled.sql
        except Exception as exc:  # noqa: BLE001
            execution_status = False
            compile_error = f"{type(exc).__name__}: {exc}\n"

        if execution_status:
            try:
                result_rows = _run_sql(compiled_sql)
            except Exception as exc:  # noqa: BLE001
                execution_status = False
                run_error = f"{type(exc).__name__}: {exc}\n"

        _write(target_dir / "sql.sql", compiled_sql)
        _write(target_dir / "result.json", json.dumps(result_rows, indent=2, default=str))
        _write(target_dir / "compile.stderr.txt", compile_error)
        _write(target_dir / "run.stderr.txt", run_error)

        summary.append(
            {
                "question_id": question_id,
                "status": QUESTION_STATUSES[question_id] if execution_status else "unsupported",
                "query_path": str(query_file.relative_to(REPO_ROOT)),
                "result_path": str((target_dir / "result.json").relative_to(REPO_ROOT)),
                "sql_path": str((target_dir / "sql.sql").relative_to(REPO_ROOT)),
                "compile_stderr_path": str(
                    (target_dir / "compile.stderr.txt").relative_to(REPO_ROOT)
                ),
                "run_stderr_path": str((target_dir / "run.stderr.txt").relative_to(REPO_ROOT)),
            }
        )

    _write(
        RESULTS_DIR / "summary.json",
        json.dumps(
            {"layer": "ktx", "ktx_sl_path": str(KTX_SL_PATH), "questions": summary}, indent=2
        ),
    )
    print(f"Wrote KtX artifacts to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
