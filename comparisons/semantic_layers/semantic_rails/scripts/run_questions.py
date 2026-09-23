from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import duckdb
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
DB_PATH = (
    REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "data" / "jaffle_comparison.duckdb"
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _provenance() -> dict[str, object]:
    """What produced this evidence, recorded by content where possible.

    The tree hashes are content-addressed, so they survive squash merges and can be checked
    against a release tag, e.g. `git rev-parse v0.2.1:semantic_rails`. Without git, the fields
    are null rather than guessed.
    """
    package = PACKAGE_DIR.relative_to(REPO_ROOT).as_posix()

    def git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args], text=True, capture_output=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return completed.stdout.strip()

    status = git(
        "status", "--porcelain", "--", "semantic_rails", "pyproject.toml", "uv.lock", package
    )
    return {
        "semantic_rails_commit": git("rev-parse", "HEAD"),
        "semantic_rails_tree": git("rev-parse", "HEAD:semantic_rails"),
        "package_tree": git("rev-parse", f"HEAD:{package}"),
        # True if the engine or the comparison package differs from the recorded commit.
        "engine_or_package_modified": None if status is None else bool(status),
    }


def main() -> None:
    config_module.list_package_paths.cache_clear()
    config_module.list_package_paths = lambda: {PACKAGE_ID: str(PACKAGE_DIR)}  # type: ignore[assignment]

    provenance = _provenance()
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        (fingerprint,) = con.execute("SELECT fingerprint FROM comparison_dataset").fetchone()
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
            {
                "layer": "semantic_rails",
                "package_id": PACKAGE_ID,
                # The published comparison states which engine produced this evidence.
                "semantic_rails_version": version("semantic-rails"),
                **provenance,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "dataset_fingerprint": fingerprint,
                "questions": summary,
            },
        )
        print(f"Wrote Semantic Rails comparison artifacts to {RESULTS_DIR}")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
