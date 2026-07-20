from __future__ import annotations

import json
from pathlib import Path

import yaml

from semantic_rails import config as config_module
from semantic_rails.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "semantic_rails" / "package"
QUESTIONS_PATH = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "questions.yml"
QUERY_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "semantic_rails" / "queries"
RESULTS_DIR = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results" / "semantic_rails"
)
PACKAGE_ID = "comparison_semantic_rails"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def main() -> None:
    config_module.list_package_paths.cache_clear()
    config_module.list_package_paths = lambda: {PACKAGE_ID: str(PACKAGE_DIR)}  # type: ignore[assignment]

    questions = list(
        (yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8")) or {}).get("questions", [])
        or []
    )
    runtime = Runtime(PACKAGE_ID)
    try:
        summary = []
        for question in questions:
            question_id = str(question["id"])
            query_path = QUERY_DIR / f"{question_id}.json"
            query = json.loads(query_path.read_text(encoding="utf-8"))
            target_dir = RESULTS_DIR / question_id

            validated = runtime.validate(query)
            explained = runtime.compile(query)
            result = runtime.query(query) if validated.get("ok") else {"query": query}

            _write_json(target_dir / "query.json", query)
            _write_json(target_dir / "validate.json", validated)
            _write_json(target_dir / "explain.json", explained)
            _write_json(target_dir / "result.json", result)
            if "rendered_sql" in result:
                _write_text(target_dir / "sql.sql", str(result["rendered_sql"]))

            summary.append(
                {
                    "question_id": question_id,
                    "status": "native" if validated.get("ok") else "unsupported",
                    "row_count": result.get("row_count", 0),
                    "query_path": str(query_path.relative_to(REPO_ROOT)),
                    "result_path": str((target_dir / "result.json").relative_to(REPO_ROOT)),
                    "sql_path": str((target_dir / "sql.sql").relative_to(REPO_ROOT))
                    if "rendered_sql" in result
                    else "",
                }
            )

        _write_json(
            RESULTS_DIR / "summary.json",
            {"layer": "semantic_rails", "package_id": PACKAGE_ID, "questions": summary},
        )
        print(f"Wrote Semantic Rails comparison artifacts to {RESULTS_DIR}")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
