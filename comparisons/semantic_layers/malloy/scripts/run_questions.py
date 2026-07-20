from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "malloy"
MODEL_PATH = PROJECT_DIR / "models" / "jaffle.malloy"
RESULTS_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results" / "malloy"
DB_PATH = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "data" / "jaffle_comparison.duckdb"
)
MALLOY_BIN = PROJECT_DIR / "node_modules" / ".bin" / "malloy-cli"

ENV = {
    **os.environ,
    "HOME": str(PROJECT_DIR / ".home"),
    "XDG_CACHE_HOME": str(PROJECT_DIR / ".cache"),
}


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


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=PROJECT_DIR, env=ENV, text=True, capture_output=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ensure_cli() -> None:
    if MALLOY_BIN.exists():
        return
    install = _run(["npm", "install"])
    _write(RESULTS_DIR / "npm_install.stdout.txt", install.stdout)
    _write(RESULTS_DIR / "npm_install.stderr.txt", install.stderr)
    if install.returncode != 0 or not MALLOY_BIN.exists():
        raise SystemExit(
            "Malloy npm install failed; see shared/results/malloy/npm_install.stderr.txt"
        )


def _run_sql(sql: str) -> str:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        cursor = con.execute(sql)
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
    finally:
        con.close()
    return json.dumps(rows, indent=2, default=str)


def main() -> None:
    (PROJECT_DIR / ".home").mkdir(parents=True, exist_ok=True)
    (PROJECT_DIR / ".cache").mkdir(parents=True, exist_ok=True)
    if RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_cli()

    validate = _run(
        [
            str(MALLOY_BIN),
            "-c",
            str(PROJECT_DIR),
            "compile",
            str(MODEL_PATH),
            "-n",
            "q01_orders_by_month",
        ]
    )
    _write(RESULTS_DIR / "validate.stdout.txt", validate.stdout)
    _write(RESULTS_DIR / "validate.stderr.txt", validate.stderr)
    if validate.returncode != 0:
        raise SystemExit("Malloy validation failed; see shared/results/malloy/validate.stderr.txt")

    summary: list[dict[str, object]] = []
    for question_id, status in QUESTION_STATUSES.items():
        target_dir = RESULTS_DIR / question_id
        compile_cmd = [
            str(MALLOY_BIN),
            "-c",
            str(PROJECT_DIR),
            "compile",
            str(MODEL_PATH),
            "-n",
            question_id,
        ]
        compile_out = _run(compile_cmd)

        compiled_sql = compile_out.stdout.removeprefix("Compiled SQL:\n").lstrip()
        _write(target_dir / "sql.sql", compiled_sql)
        _write(target_dir / "compile.stderr.txt", compile_out.stderr)

        query_file = PROJECT_DIR / "queries" / f"{question_id}.txt"
        query_file.parent.mkdir(parents=True, exist_ok=True)
        query_file.write_text(
            f"compile -n {question_id}\nexecute compiled SQL in DuckDB\n", encoding="utf-8"
        )

        run_error = ""
        execution_status = compile_out.returncode == 0
        result_text = "[]"
        if execution_status:
            try:
                result_text = _run_sql(compiled_sql)
            except Exception as exc:  # noqa: BLE001
                execution_status = False
                run_error = f"{type(exc).__name__}: {exc}\n"
        _write(target_dir / "result.json", result_text)
        _write(target_dir / "run.stderr.txt", run_error)

        summary.append(
            {
                "question_id": question_id,
                "status": status if execution_status else "unsupported",
                "query_path": str(query_file.relative_to(REPO_ROOT)),
                "result_path": str((target_dir / "result.json").relative_to(REPO_ROOT)),
                "sql_path": str((target_dir / "sql.sql").relative_to(REPO_ROOT)),
                "compile_stderr_path": str(
                    (target_dir / "compile.stderr.txt").relative_to(REPO_ROOT)
                ),
                "run_stderr_path": str((target_dir / "run.stderr.txt").relative_to(REPO_ROOT)),
            }
        )

    (RESULTS_DIR / "summary.json").write_text(
        json.dumps({"layer": "malloy", "questions": summary}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"Wrote Malloy artifacts to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
