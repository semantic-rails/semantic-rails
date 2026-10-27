"""Answer the frozen-model questions with an installed Semantic Rails release.

Run it in an environment that holds the release and not this checkout, for example:

    uv run --isolated --no-project --with semantic-rails==0.3.2 --with pyyaml \\
      python -I comparisons/semantic_layers/semantic_rails/scripts/check_release.py

It copies this repo's comparison package and queries to a temporary directory, checks the copy
against the package digest pinned in `shared/frozen_model.yml`, answers each frozen-model
question (`scope_level: variant`) on the shared DuckDB that `bootstrap_shared_duckdb.py` built,
compares each answer with the independent answer key, and writes the compact capture to
`shared/results/semantic_rails/latest_release_frozen_model.json`. A release that can't load the
package is recorded with its load error and no questions.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
PACK = REPO_ROOT / "comparisons" / "semantic_layers"
SCRIPTS = PACK / "shared" / "scripts"
OUTPUT = PACK / "shared" / "results" / "semantic_rails" / "latest_release_frozen_model.json"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"release_check_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    import semantic_rails

    if Path(semantic_rails.__file__).resolve().is_relative_to(REPO_ROOT):
        raise SystemExit(
            f"semantic_rails is imported from this checkout ({semantic_rails.__file__}), not "
            "from an installed release: run this in an isolated environment (see the docstring)."
        )
    sys.path.insert(0, str(SCRIPTS))
    runner = _load("runner", PACK / "semantic_rails" / "scripts" / "run_questions.py")
    validator = _load("validator", SCRIPTS / "validate_output_consistency.py")
    rubric = _load("rubric", SCRIPTS / "apply_rubric.py")

    import duckdb
    import yaml
    from semantic_rails import config as config_module
    from semantic_rails.runtime import Runtime

    model_sha = rubric.model_digest(["semantic_rails/package"])
    if model_sha != rubric.load_frozen_models()["semantic_rails"]["sha256"]:
        raise SystemExit("The comparison package differs from its pin in shared/frozen_model.yml")
    release_version = version("semantic-rails")
    capture: dict[str, Any] = {
        "distribution": f"PyPI semantic-rails=={release_version}",
        "engine_release": f"v{release_version}",
        "semantic_rails_version": release_version,
        "model_sha256": model_sha,
        "queries_sha256": rubric.model_digest(["semantic_rails/queries"]),
    }
    with duckdb.connect(str(runner.DB_PATH), read_only=True) as con:
        (capture["dataset_fingerprint"],) = con.execute(
            "SELECT fingerprint FROM comparison_dataset"
        ).fetchone()
    questions = yaml.safe_load(runner.QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]
    variants = [str(q["id"]) for q in questions if q.get("scope_level") == "variant"]
    maps = validator._load_column_maps()["semantic_rails"]

    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch)
        shutil.copytree(runner.PACKAGE_DIR, copy / "package")
        shutil.copytree(runner.QUERY_DIR, copy / "queries")
        config_module.list_package_paths.cache_clear()
        config_module.list_package_paths = lambda: {runner.PACKAGE_ID: str(copy / "package")}
        resolve = Runtime._resolve_asset_path
        # Read the shared database the bootstrap built (checked by its fingerprint above).
        Runtime._resolve_asset_path = lambda self, value, *, kind: (  # type: ignore[method-assign]
            str(runner.DB_PATH) if kind == "default_db" else resolve(self, value, kind=kind)
        )
        try:
            runtime = Runtime(runner.PACKAGE_ID)
        except Exception as exc:  # the release refuses this package version
            capture["load_error"] = {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            }
            capture["questions"] = []
        else:
            answers = []
            try:
                for qid in variants:
                    query = json.loads((copy / "queries" / f"{qid}.json").read_text())
                    validated = runtime.validate(query)
                    if not validated.get("ok"):
                        answers.append(
                            {
                                "question_id": qid,
                                "status": "unsupported",
                                "errors": validated.get("errors"),
                            }
                        )
                        continue
                    result = runtime.query(query)
                    key = json.loads(
                        (SCRIPTS.parent / "results" / "oracle" / qid / "result.json").read_text()
                    )
                    equal, _ = validator._rows_equal(
                        validator._normalize_rows(qid, key),
                        validator._normalize_rows(qid, result["rows"], maps[qid]),
                    )
                    answers.append(
                        {
                            "question_id": qid,
                            "status": "executed",
                            "row_count": result.get("row_count", 0),
                            "answer_key_matches": bool(equal),
                        }
                    )
            finally:
                runtime.close()
            capture["questions"] = answers
    capture["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    OUTPUT.write_text(json.dumps(capture, indent=2, sort_keys=True), encoding="utf-8")
    answered = sum(item["status"] == "executed" for item in capture["questions"])
    print(f"{capture['engine_release']}: {answered} of {len(variants)} answered; wrote {OUTPUT}")


if __name__ == "__main__":
    main()
