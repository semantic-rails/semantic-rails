"""Run the Cube pack live: start Cube Core, then save /meta and each query's /sql and /load.

A `queries/*.json` file is a REST query. A `queries/*.sql` file is an SQL API query, sent to
/cubesql; its /load result is saved in the same shape as a REST query's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
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
BASE_URL = "http://127.0.0.1:4000/cubejs-api/v1"
PACKAGES = ["@cubejs-backend/server", "@cubejs-backend/duckdb-driver", "@duckdb/node-api"]
# A fresh API secret per run: Cube accepts only requests carrying a JWT signed with it.
API_SECRET = secrets.token_hex(32)


def token(secret: str) -> str:
    """An HS256 JWT signed with the API secret, which Cube requires on every request."""

    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signed = encode(b'{"alg":"HS256","typ":"JWT"}') + "." + encode(b"{}")
    signature = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{encode(signature)}"


def _request(path: str, query: str | None = None, **params: str) -> str:
    params = {"query": query, **params} if query else params
    url = BASE_URL + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    request = urllib.request.Request(url, headers={"Authorization": token(API_SECRET)})
    for _ in range(60):
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
        if json.loads(body).get("error") != "Continue wait":  # a long query: ask again
            return body
    raise SystemExit(f"Cube still answered 'Continue wait' to {path} after 60 tries")


class SqlApiError(Exception):
    """Cube refused an SQL API query."""


def _cubesql(sql: str) -> str:
    """Run an SQL API query through /cubesql and return its rows the way /load returns them."""
    request = urllib.request.Request(
        BASE_URL + "/cubesql",
        data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": token(API_SECRET), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        chunks = [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line]
    errors = [chunk["error"] for chunk in chunks if "error" in chunk]
    if errors or not chunks or "schema" not in chunks[0]:
        raise SqlApiError("; ".join(errors) or "no schema in the response")
    names = [column["name"] for column in chunks[0]["schema"]]
    rows = [dict(zip(names, row, strict=True)) for chunk in chunks[1:] for row in chunk["data"]]
    return json.dumps({"schema": chunks[0]["schema"], "data": rows}, indent=2)


def _wait_for_meta(server: subprocess.Popen[bytes], log: IO[bytes]) -> str:
    for _ in range(90):
        if server.poll() is not None:
            break
        try:
            return _request("/meta")
        except urllib.error.HTTPError as exc:  # Cube is up but refused: don't wait it out
            raise SystemExit(f"Cube answered /meta with HTTP {exc.code}") from exc
        except OSError:
            time.sleep(1)
    log.seek(0)
    raise SystemExit("Cube didn't start:\n" + log.read().decode("utf-8", "replace")[-4000:])


def _server_environment() -> dict[str, str]:
    """What Node needs, plus the API secret. An inherited CUBEJS_* setting, CUBE_DUCKDB_PATH or
    PORT would change what Cube runs or where it listens, apart from what this runner records."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    return env | {"CUBEJS_API_SECRET": API_SECRET}


def _stop(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:  # don't leave Cube listening
        server.kill()
        server.wait()


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
        server = subprocess.Popen(
            ["node", "index.js"], cwd=PROJECT_DIR, env=_server_environment(), stdout=log, stderr=log
        )
        try:
            _write(RESULTS_DIR / "meta.json", _wait_for_meta(server, log))
            queries = sorted((PROJECT_DIR / "queries").glob("q*.*"), key=lambda path: path.stem)
            for query_file in queries:
                target = RESULTS_DIR / query_file.stem
                query = query_file.read_text(encoding="utf-8")
                sql_api = query_file.suffix == ".sql"
                try:
                    if sql_api:
                        _write(target / "sql.json", _request("/sql", query, format="sql"))
                        _write(target / "load.json", _cubesql(query))
                    else:
                        _write(target / "sql.json", _request("/sql", query))
                        _write(target / "load.json", _request("/load", query))
                    status = "executed"
                except (urllib.error.HTTPError, SqlApiError) as exc:
                    is_http = isinstance(exc, urllib.error.HTTPError)
                    _write(
                        target / "error.txt", exc.read().decode("utf-8") if is_http else str(exc)
                    )
                    reason = f"HTTP {exc.code}" if is_http else "SQL API error"
                    unsupported[query_file.stem] = {"status": "unsupported", "reason": reason}
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
            _stop(server)
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
