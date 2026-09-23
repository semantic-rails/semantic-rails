"""Answer every question with the independent answer key in `shared/oracle/`.

The answer key is hand-written SQL over the shared `comparison_*` views, written and reviewed
without looking at any layer's models or outputs (see `shared/oracle/SEMANTICS.md`). Every layer
is checked against these results; no layer is the reference.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SHARED_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared"
ORACLE_DIR = SHARED_DIR / "oracle"
RESULTS_DIR = SHARED_DIR / "results" / "oracle"
DB_PATH = SHARED_DIR / "data" / "jaffle_comparison.duckdb"


def main() -> None:
    questions = yaml.safe_load((SHARED_DIR / "questions.yml").read_text(encoding="utf-8"))
    if RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        (fingerprint,) = con.execute("SELECT fingerprint FROM comparison_dataset").fetchone()
        summary = []
        for question in questions["questions"]:
            question_id = question["id"]
            sql_path = ORACLE_DIR / f"{question_id}.sql"
            cursor = con.execute(sql_path.read_text(encoding="utf-8"))
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            result_path = RESULTS_DIR / question_id / "result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
            summary.append(
                {
                    "question_id": question_id,
                    "status": "executed",
                    "sql_path": str(sql_path.relative_to(REPO_ROOT)),
                    "result_path": str(result_path.relative_to(REPO_ROOT)),
                    "row_count": len(rows),
                }
            )
    finally:
        con.close()
    (RESULTS_DIR / "summary.json").write_text(
        json.dumps(
            {
                "layer": "answer_key",
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "dataset_fingerprint": fingerprint,
                "environment": {"duckdb": duckdb.__version__},
                "questions": summary,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"Answered {len(summary)} questions from the answer key into {RESULTS_DIR}")


if __name__ == "__main__":
    main()
