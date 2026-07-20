from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
LAYER_ROOT = REPO_ROOT / "comparisons" / "semantic_layers" / "snowflake_semantic_views"
SHARED_RESULTS_ROOT = (
    REPO_ROOT
    / "comparisons"
    / "semantic_layers"
    / "shared"
    / "results"
    / "snowflake_semantic_views"
)
QUERY_EXAMPLES_PATH = LAYER_ROOT / "query_examples.sql"
VERIFY_TRIAL_LOAD_PATH = LAYER_ROOT / "verify_trial_load.sql"
CREATE_SQL_PATH = LAYER_ROOT / "generated" / "create_semantic_view.sql"
RENDER_SCRIPT_PATH = LAYER_ROOT / "scripts" / "render_create_semantic_view_sql.py"
CONNECTION_NAME = "semantic_views_trial"
SEMANTIC_VIEW_FQN = "ANALYTICS.SEMANTIC_COMPARISON.JAFFLE_SEMANTIC_COMPARISON"

QUESTION_MARKER = re.compile(r"^--\s*(q\d{2}_[a-z0-9_]+)\s*$")
QUESTION_STATUS = {
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

SMOKE_TEST_SQL = f"""
SELECT *
FROM SEMANTIC_VIEW(
  {SEMANTIC_VIEW_FQN}
  DIMENSIONS stores.store_name, DATE_TRUNC('month', orders.ordered_at) AS ordered_month
  METRICS orders.revenue_usd
)
ORDER BY ordered_month, store_name
LIMIT 12;
""".strip()

INTROSPECTION_SQL = f"""
SHOW SEMANTIC VIEWS IN SCHEMA ANALYTICS.SEMANTIC_COMPARISON;
DESCRIBE SEMANTIC VIEW {SEMANTIC_VIEW_FQN};
""".strip()


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=isinstance(payload, dict)))


def run_command(args: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def run_snow_sql(
    *, query: str | None = None, file_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    args = ["snow", "sql", "-c", CONNECTION_NAME, "--format", "JSON_EXT"]
    if query is not None:
        args.extend(["-q", query])
    elif file_path is not None:
        args.extend(["-f", str(file_path)])
    else:
        raise ValueError("Expected either query or file_path")
    return run_command(args)


def parse_json_stdout(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return None
    return json.loads(text)


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        if not payload:
            return []
        if all(isinstance(item, dict) for item in payload):
            return payload
        if all(isinstance(item, list) for item in payload):
            for item in reversed(payload):
                if isinstance(item, list) and (
                    not item or all(isinstance(row, dict) for row in item)
                ):
                    return item
    raise ValueError(f"Unsupported Snowflake JSON payload shape: {type(payload)!r}")


def parse_query_blocks(path: Path) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current_qid: str | None = None
    current_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        marker_match = QUESTION_MARKER.match(line)
        if marker_match:
            if current_qid is not None:
                blocks[current_qid] = "\n".join(current_lines).strip() + "\n"
            current_qid = marker_match.group(1)
            current_lines = []
            continue
        if current_qid is not None:
            current_lines.append(line)
    if current_qid is not None:
        blocks[current_qid] = "\n".join(current_lines).strip() + "\n"
    return blocks


def ensure_success(result: subprocess.CompletedProcess[str], *, context: str) -> Any:
    if result.returncode != 0:
        raise RuntimeError(
            f"{context} failed with exit code {result.returncode}.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return parse_json_stdout(result.stdout)


def main() -> None:
    if SHARED_RESULTS_ROOT.exists():
        shutil.rmtree(SHARED_RESULTS_ROOT)
    SHARED_RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

    render = run_command([sys.executable, str(RENDER_SCRIPT_PATH)])
    write_text(SHARED_RESULTS_ROOT / "render_create_semantic_view.stdout.txt", render.stdout)
    write_text(SHARED_RESULTS_ROOT / "render_create_semantic_view.stderr.txt", render.stderr)
    if render.returncode != 0:
        raise RuntimeError(render.stderr or "render_create_semantic_view_sql.py failed")

    verify = run_snow_sql(file_path=VERIFY_TRIAL_LOAD_PATH)
    write_text(SHARED_RESULTS_ROOT / "verify_trial_load.stderr.txt", verify.stderr)
    write_json(
        SHARED_RESULTS_ROOT / "verify_trial_load.json",
        ensure_success(verify, context="verify_trial_load.sql"),
    )

    create = run_snow_sql(file_path=CREATE_SQL_PATH)
    write_text(SHARED_RESULTS_ROOT / "create_semantic_view.stderr.txt", create.stderr)
    write_json(
        SHARED_RESULTS_ROOT / "create_semantic_view.json",
        ensure_success(create, context="create_semantic_view.sql"),
    )

    introspection = run_snow_sql(query=INTROSPECTION_SQL)
    write_text(SHARED_RESULTS_ROOT / "describe_semantic_view.stderr.txt", introspection.stderr)
    write_json(
        SHARED_RESULTS_ROOT / "describe_semantic_view.json",
        ensure_success(introspection, context="semantic view introspection"),
    )

    smoke_test = run_snow_sql(query=SMOKE_TEST_SQL)
    write_text(SHARED_RESULTS_ROOT / "smoke_test.stderr.txt", smoke_test.stderr)
    smoke_payload = ensure_success(smoke_test, context="semantic view smoke test")
    write_json(SHARED_RESULTS_ROOT / "smoke_test.json", smoke_payload)
    write_text(SHARED_RESULTS_ROOT / "smoke_test.sql", SMOKE_TEST_SQL + "\n")

    query_blocks = parse_query_blocks(QUERY_EXAMPLES_PATH)
    summary_entries: list[dict[str, Any]] = []
    unsupported_entries: dict[str, dict[str, str]] = {}

    for question_id, sql in query_blocks.items():
        question_dir = SHARED_RESULTS_ROOT / question_id
        question_dir.mkdir(parents=True, exist_ok=True)
        write_text(question_dir / "sql.sql", sql)

        result = run_snow_sql(query=sql)
        write_text(question_dir / "stderr.txt", result.stderr)

        if result.returncode != 0:
            write_text(question_dir / "stdout.txt", result.stdout)
            unsupported_entries[question_id] = {
                "status": "unsupported",
                "reason": (result.stderr or result.stdout or "Snowflake execution failed").strip(),
            }
            continue

        payload = parse_json_stdout(result.stdout)
        rows = extract_rows(payload)
        write_json(question_dir / "stdout.json", payload)
        write_json(question_dir / "result.json", rows)

        summary_entries.append(
            {
                "query_path": rel(QUERY_EXAMPLES_PATH),
                "question_id": question_id,
                "result_path": rel(question_dir / "result.json"),
                "row_count": len(rows),
                "sql_path": rel(question_dir / "sql.sql"),
                "status": QUESTION_STATUS[question_id],
                "stderr_path": rel(question_dir / "stderr.txt"),
                "stdout_path": rel(question_dir / "stdout.json"),
            }
        )

    write_json(SHARED_RESULTS_ROOT / "unsupported.json", unsupported_entries)
    write_json(
        SHARED_RESULTS_ROOT / "summary.json",
        {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "layer": "snowflake_semantic_views",
            "connection": CONNECTION_NAME,
            "semantic_view": SEMANTIC_VIEW_FQN,
            "questions": summary_entries,
        },
    )
    print(f"Wrote Snowflake results to {SHARED_RESULTS_ROOT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
