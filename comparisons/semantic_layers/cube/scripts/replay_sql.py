"""Re-execute Cube's captured SQL on the current shared DuckDB dataset.

Cube 1.6.32 can't be reinstalled until its dependency advisories are resolved (see
../README.md), so `run_questions.py` can't regenerate its results. The SQL that Cube generated
from these models is captured in `shared/results/cube/q*/sql.json` and doesn't depend on the
data, so re-executing it answers each question on the same data as every other layer. The
captured evidence stays untouched; the replay writes to `shared/results/cube_sql_replay/`.

Cube's SQL casts timestamps through `timestamptz`, so the session time zone is pinned to UTC.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
SHARED_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared"
CAPTURE_DIR = SHARED_DIR / "results" / "cube"
REPLAY_DIR = SHARED_DIR / "results" / "cube_sql_replay"
DB_PATH = SHARED_DIR / "data" / "jaffle_comparison.duckdb"


def _replay(con: duckdb.DuckDBPyConnection, sql_path: Path) -> list[dict[str, Any]]:
    captured = json.loads(sql_path.read_text(encoding="utf-8"))["sql"]
    statement, params = captured["sql"]
    members = captured["aliasNameToMember"]
    cursor = con.execute(statement, list(params or []))
    columns = [members.get(column[0], column[0]) for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def main() -> None:
    capture = json.loads((CAPTURE_DIR / "summary.json").read_text(encoding="utf-8"))
    if REPLAY_DIR.exists():
        shutil.rmtree(REPLAY_DIR)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        (fingerprint,) = con.execute("SELECT fingerprint FROM comparison_dataset").fetchone()
        questions = []
        for entry in capture["questions"]:
            question_id = entry["question_id"]
            rows = _replay(con, REPO_ROOT / entry["sql_path"])
            result_path = REPLAY_DIR / question_id / "result.json"
            result_path.parent.mkdir(parents=True)
            # Same shape as Cube's own load.json, so readers treat both alike.
            result_path.write_text(
                json.dumps({"data": rows}, indent=2, default=str), encoding="utf-8"
            )
            questions.append(
                {
                    "question_id": question_id,
                    "status": "executed",
                    "query_path": entry["query_path"],
                    "sql_path": entry["sql_path"],
                    "result_path": str(result_path.relative_to(REPO_ROOT)),
                    "row_count": len(rows),
                }
            )
    finally:
        con.close()
    summary = {
        "layer": "cube",
        "method": "Cube 1.6.32 SQL captured 2026-04-07, re-executed on the current dataset",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_fingerprint": fingerprint,
        "environment": {"duckdb": duckdb.__version__},
        "questions": questions,
    }
    (REPLAY_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"Replayed {len(questions)} captured Cube queries into {REPLAY_DIR}")


if __name__ == "__main__":
    main()
