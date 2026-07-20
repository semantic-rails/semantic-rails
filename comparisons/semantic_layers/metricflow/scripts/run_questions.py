from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "metricflow"
RESULTS_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results" / "metricflow"
MF_BIN = PROJECT_DIR / ".venv" / "bin" / "mf"
DBT_BIN = PROJECT_DIR / ".venv" / "bin" / "dbt"
ENV = {
    **os.environ,
    "DBT_PROFILES_DIR": str(PROJECT_DIR),
}


QUESTION_COMMANDS = {
    "q01_orders_by_month": [
        "query",
        "--metrics",
        "orders",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q02_revenue_by_store_by_month": [
        "query",
        "--metrics",
        "revenue_usd",
        "--group-by",
        "metric_time__month,store__store_name",
        "--order",
        "metric_time__month",
    ],
    "q03_item_revenue_by_product_type_by_month": [
        "query",
        "--metrics",
        "item_revenue_usd",
        "--group-by",
        "metric_time__month,item__product_type",
        "--order",
        "metric_time__month",
    ],
    "q04_aov_by_store": [
        "query",
        "--metrics",
        "aov_usd",
        "--group-by",
        "store__store_name",
        "--order",
        "-aov_usd",
    ],
    "q05_orders_and_item_revenue_by_store_by_month": [
        "query",
        "--metrics",
        "orders,item_revenue_usd",
        "--group-by",
        "metric_time__month,store__store_name",
        "--order",
        "metric_time__month",
    ],
    "q06_new_customer_orders_by_month": [
        "query",
        "--metrics",
        "new_customer_orders",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q07_delivered_revenue_by_month": [
        "query",
        "--metrics",
        "delivered_revenue",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q08_revenue_by_customer_segment_as_of_order_time": [
        "query",
        "--metrics",
        "revenue_usd",
        "--group-by",
        "metric_time__month,customer__customer_segment",
        "--order",
        "metric_time__month",
    ],
    "q09_session_to_order_conversion_7d": [
        "query",
        "--metrics",
        "session_to_order_conversion_rate_7d",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q10_orders_from_customers_with_10plus_orders_in_month": [
        "query",
        "--metrics",
        "qualifying_orders",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q11_repeat_customer_orders_by_store_by_month": [
        "query",
        "--metrics",
        "repeat_customer_orders",
        "--group-by",
        "metric_time__month,store__store_name",
        "--order",
        "metric_time__month",
    ],
    "q12_orders_by_month_with_lifetime_spend_500_filter": [
        "query",
        "--metrics",
        "filtered_orders_lifetime_spend_500",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": [
        "query",
        "--metrics",
        "qualifying_orders",
        "--group-by",
        "metric_time__day",
        "--order",
        "metric_time__day",
    ],
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": [
        "query",
        "--metrics",
        "qualifying_revenue_usd_same_store",
        "--group-by",
        "metric_time__month,store__store_name",
        "--order",
        "metric_time__month",
    ],
    "q15_same_store_session_to_order_conversion_7d": [
        "query",
        "--metrics",
        "same_store_session_to_order_conversion_rate_7d",
        "--group-by",
        "metric_time__month",
        "--order",
        "metric_time__month",
    ],
    "q16_revenue_by_customer_segment_as_of_delivered_time": [
        "query",
        "--metrics",
        "delivered_revenue",
        "--group-by",
        "metric_time__month,customer__customer_segment",
        "--order",
        "metric_time__month",
    ],
}

QUESTION_STATUSES = {
    "q01_orders_by_month": "native",
    "q02_revenue_by_store_by_month": "native",
    "q03_item_revenue_by_product_type_by_month": "native",
    "q04_aov_by_store": "native",
    "q05_orders_and_item_revenue_by_store_by_month": "native",
    "q06_new_customer_orders_by_month": "native",
    "q07_delivered_revenue_by_month": "native",
    "q08_revenue_by_customer_segment_as_of_order_time": "native",
    "q09_session_to_order_conversion_7d": "precomputed",
    "q10_orders_from_customers_with_10plus_orders_in_month": "precomputed",
    "q11_repeat_customer_orders_by_store_by_month": "precomputed",
    "q12_orders_by_month_with_lifetime_spend_500_filter": "precomputed",
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": "precomputed",
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": "precomputed",
    "q15_same_store_session_to_order_conversion_7d": "precomputed",
    "q16_revenue_by_customer_segment_as_of_delivered_time": "native",
}


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=ENV, text=True, capture_output=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ensure_environment() -> None:
    if MF_BIN.exists() and DBT_BIN.exists():
        return

    venv = subprocess.run(["uv", "venv", ".venv"], cwd=PROJECT_DIR, text=True, capture_output=True)
    _write(RESULTS_DIR / "bootstrap_venv.stdout.txt", venv.stdout)
    _write(RESULTS_DIR / "bootstrap_venv.stderr.txt", venv.stderr)
    if venv.returncode != 0:
        raise SystemExit("MetricFlow venv bootstrap failed.")

    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(PROJECT_DIR / ".venv" / "bin" / "python"),
            "--prerelease=allow",
            "dbt-metricflow==0.11.0",
            "dbt-duckdb==1.10.1",
        ],
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
    )
    _write(RESULTS_DIR / "bootstrap_install.stdout.txt", install.stdout)
    _write(RESULTS_DIR / "bootstrap_install.stderr.txt", install.stderr)
    if install.returncode != 0:
        raise SystemExit("MetricFlow dependency bootstrap failed.")


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_environment()

    build = _run([str(DBT_BIN), "build"], cwd=PROJECT_DIR)
    _write(RESULTS_DIR / "dbt_build.stdout.txt", build.stdout)
    _write(RESULTS_DIR / "dbt_build.stderr.txt", build.stderr)
    if build.returncode != 0:
        raise SystemExit("dbt build failed; see results/metricflow/dbt_build.stderr.txt")

    validate = _run([str(MF_BIN), "validate-configs"], cwd=PROJECT_DIR)
    _write(RESULTS_DIR / "validate.stdout.txt", validate.stdout)
    _write(RESULTS_DIR / "validate.stderr.txt", validate.stderr)

    list_metrics = _run([str(MF_BIN), "list", "metrics"], cwd=PROJECT_DIR)
    _write(RESULTS_DIR / "list_metrics.stdout.txt", list_metrics.stdout)
    _write(RESULTS_DIR / "list_metrics.stderr.txt", list_metrics.stderr)

    summary: list[dict[str, object]] = []
    for question_id, args in QUESTION_COMMANDS.items():
        target_dir = RESULTS_DIR / question_id
        target_dir.mkdir(parents=True, exist_ok=True)
        explain_command = [str(MF_BIN), *args, "--explain"]
        result_csv = target_dir / "result.csv"
        data_command = [str(MF_BIN), *args, "--quiet", "--csv", str(result_csv)]
        query_file = PROJECT_DIR / "queries" / f"{question_id}.txt"
        query_file.parent.mkdir(parents=True, exist_ok=True)
        query_file.write_text(" ".join(args) + "\n", encoding="utf-8")

        explained = _run(explain_command, cwd=PROJECT_DIR)
        executed = _run(data_command, cwd=PROJECT_DIR)
        _write(target_dir / "sql.txt", explained.stdout)
        _write(target_dir / "explain.stderr.txt", explained.stderr)
        _write(target_dir / "stdout.txt", executed.stdout)
        _write(target_dir / "stderr.txt", executed.stderr)

        summary.append(
            {
                "question_id": question_id,
                "status": QUESTION_STATUSES[question_id]
                if explained.returncode == 0 and executed.returncode == 0
                else "unsupported",
                "query_path": str(query_file.relative_to(REPO_ROOT)),
                "result_path": str(result_csv.relative_to(REPO_ROOT)),
                "sql_path": str((target_dir / "sql.txt").relative_to(REPO_ROOT)),
                "stderr_path": str((target_dir / "stderr.txt").relative_to(REPO_ROOT)),
                "explain_stderr_path": str(
                    (target_dir / "explain.stderr.txt").relative_to(REPO_ROOT)
                ),
            }
        )

    (RESULTS_DIR / "summary.json").write_text(
        json.dumps({"layer": "metricflow", "questions": summary}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    unsupported = {}
    (RESULTS_DIR / "unsupported.json").write_text(
        json.dumps(unsupported, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"Wrote MetricFlow artifacts to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
