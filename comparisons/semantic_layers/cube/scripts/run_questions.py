from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "cube"
RESULTS_DIR = REPO_ROOT / "comparisons" / "semantic_layers" / "shared" / "results" / "cube"
API_SECRET = "comparison-secret"
BASE_URL = "http://127.0.0.1:4000/cubejs-api/v1"
QUESTION_STATUSES = {
    "q01_orders_by_month": "native",
    "q02_revenue_by_store_by_month": "native",
    "q03_item_revenue_by_product_type_by_month": "native",
    "q04_aov_by_store": "native",
    "q05_orders_and_item_revenue_by_store_by_month": "workaround",
    "q06_new_customer_orders_by_month": "native",
    "q07_delivered_revenue_by_month": "native",
    "q08_revenue_by_customer_segment_as_of_order_time": "workaround",
    "q09_session_to_order_conversion_7d": "precomputed",
    "q10_orders_from_customers_with_10plus_orders_in_month": "precomputed",
    "q11_repeat_customer_orders_by_store_by_month": "workaround",
    "q12_orders_by_month_with_lifetime_spend_500_filter": "workaround",
    "q13_daily_orders_from_customers_with_10plus_orders_in_month": "precomputed",
    "q14_revenue_from_customers_with_10plus_orders_same_store_month": "precomputed",
    "q15_same_store_session_to_order_conversion_7d": "precomputed",
    "q16_revenue_by_customer_segment_as_of_delivered_time": "precomputed",
}


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            env[key] = value
    return env


def _request(path: str, query: dict[str, object] | None = None) -> bytes:
    url = f"{BASE_URL}{path}"
    if query is not None:
        url += "?" + urllib.parse.urlencode({"query": json.dumps(query)})
    req = urllib.request.Request(url, headers={"Authorization": API_SECRET})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _wait_for_server() -> None:
    for _ in range(60):
        try:
            _request("/meta")
            return
        except Exception:
            time.sleep(1)
    raise TimeoutError("Cube server did not start in time.")


def main() -> None:
    if not (PROJECT_DIR / "package.json").is_file():
        raise SystemExit(
            "Cube comparison is captured evidence only: the recorded npm graph has "
            "unresolved high/critical advisories. Review runtime-snapshot.json, verify "
            "its lock/SBOM evidence, and provide a separately audited manifest before "
            "rerunning it."
        )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["npm_config_cache"] = "/tmp/codex-npm-cache"

    install = subprocess.run(
        ["npm", "install"],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
    )
    _write(RESULTS_DIR / "npm_install.stdout.txt", install.stdout)
    _write(RESULTS_DIR / "npm_install.stderr.txt", install.stderr)
    if install.returncode != 0:
        raise SystemExit("Cube npm install failed.")

    validate = subprocess.run(
        ["npm", "run", "validate"],
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        capture_output=True,
    )
    _write(RESULTS_DIR / "validate.stdout.txt", validate.stdout)
    _write(RESULTS_DIR / "validate.stderr.txt", validate.stderr)

    server = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_server()
        _write(RESULTS_DIR / "meta.json", _request("/meta").decode("utf-8"))

        summary: list[dict[str, object]] = []
        for query_file in sorted((PROJECT_DIR / "queries").glob("q*.json")):
            question_id = query_file.stem
            target_dir = RESULTS_DIR / question_id
            query = json.loads(query_file.read_text(encoding="utf-8"))

            try:
                sql_raw = _request("/sql", query).decode("utf-8")
                _write(target_dir / "sql.json", sql_raw)
                load_raw = _request("/load", query).decode("utf-8")
                _write(target_dir / "load.json", load_raw)
                status = QUESTION_STATUSES[question_id]
            except urllib.error.HTTPError as exc:
                _write(target_dir / "error.txt", exc.read().decode("utf-8"))
                status = "unsupported"

            summary.append(
                {
                    "question_id": question_id,
                    "status": status,
                    "query_path": str(query_file.relative_to(REPO_ROOT)),
                    "result_path": str((target_dir / "load.json").relative_to(REPO_ROOT)),
                    "sql_path": str((target_dir / "sql.json").relative_to(REPO_ROOT)),
                }
            )

        unsupported = {}
        _write(RESULTS_DIR / "unsupported.json", json.dumps(unsupported, indent=2, sort_keys=True))
        _write(
            RESULTS_DIR / "summary.json",
            json.dumps({"layer": "cube", "questions": summary}, indent=2, sort_keys=True),
        )
    finally:
        server.terminate()
        try:
            stdout, stderr = server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            stdout, stderr = server.communicate(timeout=10)
        _write(RESULTS_DIR / "server.stdout.txt", stdout)
        _write(RESULTS_DIR / "server.stderr.txt", stderr)

    print(f"Wrote Cube artifacts to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
