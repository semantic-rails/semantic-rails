"""Run the Cube pack live: start Cube Core, then save /meta and each query's /sql and /load."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
PACK = REPO_ROOT / "comparisons" / "semantic_layers"
PROJECT_DIR = PACK / "cube"
RESULTS_DIR = PACK / "shared" / "results" / "cube"
DATABASE = PACK / "shared" / "data" / "jaffle_comparison.duckdb"
BASE_URL = "http://127.0.0.1:4000/cubejs-api/v1"  # index.js runs in dev mode: no token needed
PACKAGES = ["@cubejs-backend/server", "@cubejs-backend/duckdb-driver", "@duckdb/node-api"]


def _request(path: str, query: str | None = None) -> str:
    url = BASE_URL + path + (f"?{urllib.parse.urlencode({'query': query})}" if query else "")
    while True:
        with urllib.request.urlopen(url, timeout=120) as response:
            body = response.read().decode("utf-8")
        if json.loads(body).get("error") != "Continue wait":  # a long query: ask again
            return body


def _wait_for_meta(server: subprocess.Popen[bytes], log: IO[bytes]) -> str:
    for _ in range(90):
        if server.poll() is not None:
            break
        try:
            return _request("/meta")
        except OSError:
            time.sleep(1)
    log.seek(0)
    raise SystemExit("Cube didn't start:\n" + log.read().decode("utf-8", "replace")[-4000:])


def _environment() -> dict[str, str]:
    modules = PROJECT_DIR / "node_modules"
    if not modules.is_dir():
        raise SystemExit("Install Cube first: see comparisons/semantic_layers/cube/README.md")
    versions = {
        name: json.loads((modules / name / "package.json").read_text(encoding="utf-8"))["version"]
        for name in PACKAGES
    }
    node = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True)
    return versions | {"node": node.stdout.strip()}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    environment = _environment()
    with duckdb.connect(str(DATABASE), read_only=True) as con:
        (fingerprint,) = con.execute("SELECT fingerprint FROM comparison_dataset").fetchone()
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    questions, unsupported = [], {}
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen(["node", "index.js"], cwd=PROJECT_DIR, stdout=log, stderr=log)
        try:
            _write(RESULTS_DIR / "meta.json", _wait_for_meta(server, log))
            for query_file in sorted((PROJECT_DIR / "queries").glob("q*.json")):
                target = RESULTS_DIR / query_file.stem
                query = query_file.read_text(encoding="utf-8")
                try:
                    _write(target / "sql.json", _request("/sql", query))
                    _write(target / "load.json", _request("/load", query))
                    status = "executed"
                except urllib.error.HTTPError as exc:
                    _write(target / "error.txt", exc.read().decode("utf-8"))
                    unsupported[query_file.stem] = f"HTTP {exc.code}"
                    status = "unsupported"
                questions.append(
                    {
                        "question_id": query_file.stem,
                        "status": status,
                        "query_path": str(query_file.relative_to(REPO_ROOT)),
                        "result_path": str((target / "load.json").relative_to(REPO_ROOT)),
                        "sql_path": str((target / "sql.json").relative_to(REPO_ROOT)),
                    }
                )
        finally:
            server.terminate()
            server.wait(timeout=30)
    summary = {
        "layer": "cube",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_fingerprint": fingerprint,
        "environment": environment,
        "questions": questions,
    }
    _write(RESULTS_DIR / "unsupported.json", json.dumps(unsupported, indent=2, sort_keys=True))
    _write(RESULTS_DIR / "summary.json", json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote Cube results to {RESULTS_DIR.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
