"""Answer every question with the independent answer key in `shared/oracle/`.

The answer key is hand-written SQL over the shared `comparison_*` views, written and reviewed
without looking at any layer's models or outputs (see `shared/oracle/SEMANTICS.md`). Every layer
is checked against these results; no layer is the reference.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SHARED_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared"
ORACLE_DIR = SHARED_DIR / "oracle"
QUESTIONS_PATH = SHARED_DIR / "questions.yml"
RESULTS_DIR = SHARED_DIR / "results" / "oracle"
DB_PATH = SHARED_DIR / "data" / "jaffle_comparison.duckdb"


def answer_key_fingerprint(oracle_dir: Path, questions_path: Path) -> str:
    """Identify what the answer key computes: its queries and the question definitions.

    The dataset fingerprint covers the data only. Recording this too means answers cached from
    an older query or question can't pass as current.
    """
    digest = hashlib.sha256()
    for path in [questions_path, *sorted(oracle_dir.glob("*.sql"))]:
        data = path.read_bytes().replace(b"\r\n", b"\n")  # the same text on a CRLF checkout
        digest.update(f"{path.name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def answer(con: duckdb.DuckDBPyConnection, question_id: str) -> list[dict[str, Any]]:
    cursor = con.execute((ORACLE_DIR / f"{question_id}.sql").read_text(encoding="utf-8"))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def main() -> None:
    questions = yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))
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
            rows = answer(con, question_id)
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
                "answer_key_fingerprint": answer_key_fingerprint(ORACLE_DIR, QUESTIONS_PATH),
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
