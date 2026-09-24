"""Capture compile-only golden SQL for every supported dialect.

Compiles each capture_sql_baseline.py payload against jaffle_shop once per
dialect in supported_warehouses(), swapping package.warehouse, and writes
{dialect: {metric_id: {"sql": ..., "column_mapping": [...]}}} as sorted JSON.
The column mapping is how the dialect's final preparation renamed result
columns (Postgres identifier shortening, BigQuery field names). A statement
that fails to compile is recorded as {"error": "<code>: <message>"}. Nothing
connects to a warehouse. A pure code move must leave every statement
byte-identical.

Exit status: a capture exits 1 when any statement failed to compile (the file
is still written); --compare exits 1 when a statement changed or either file
records a failure.

Usage:
    uv run python scripts/dev/capture_dialect_sql.py /tmp/dialect_sql_golden.json
    uv run python scripts/dev/capture_dialect_sql.py --compare GOLDEN.json AFTER.json
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
from pathlib import Path
from typing import Any

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry

Capture = dict[str, dict[str, dict[str, Any]]]


def _payloads() -> list[dict[str, Any]]:
    """The queries capture_sql_baseline.py captures, loaded from that file."""
    path = Path(__file__).with_name("capture_sql_baseline.py")
    spec = importlib.util.spec_from_file_location("capture_sql_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.PAYLOADS)


def capture() -> Capture:
    base = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    payloads = _payloads()
    golden: Capture = {}
    for warehouse in supported_warehouses():
        config = dataclasses.replace(
            base, package=dataclasses.replace(base.package, warehouse=warehouse)
        )
        registry = Registry(config)
        rows: dict[str, dict[str, Any]] = {}
        for entry in payloads:
            try:
                compiled = compile_query(config, registry, {"version": 1, **entry["payload"]})
            except Exception as exc:  # recorded, and it fails the capture
                code = exc.code if isinstance(exc, SemanticLayerError) else type(exc).__name__
                rows[entry["metric_id"]] = {"error": f"{code}: {exc}"}
                continue
            prepared = compiled["prepared_query"]
            rows[entry["metric_id"]] = {
                "sql": prepared.sql,
                "column_mapping": [list(pair) for pair in prepared.column_mapping],
            }
        golden[warehouse] = rows
    return golden


def failures(captured: Capture) -> list[str]:
    return sorted(
        f"{dialect}: {metric_id}: {row['error']}"
        for dialect, rows in captured.items()
        for metric_id, row in rows.items()
        if "error" in row
    )


def compare(golden_path: str, after_path: str) -> int:
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    failed = [f"golden {row}" for row in failures(golden)]
    failed += [f"after {row}" for row in failures(after)]
    changed = sorted(
        f"{dialect}: {metric_id}"
        for dialect in golden.keys() | after.keys()
        for metric_id in golden.get(dialect, {}).keys() | after.get(dialect, {}).keys()
        if golden.get(dialect, {}).get(metric_id) != after.get(dialect, {}).get(metric_id)
    )
    if failed:
        print(f"FAIL — {len(failed)} statement(s) did not compile:")
        print("\n".join(f"  - {row}" for row in failed))
    if changed:
        print(f"CHANGED — {len(changed)} statement(s) differ:")
        print("\n".join(f"  - {row}" for row in changed))
    if failed or changed:
        return 1
    total = sum(len(rows) for rows in golden.values())
    print(f"IDENTICAL — {len(golden)} dialects, {total} statements")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", metavar="PATH")
    parser.add_argument("--compare", action="store_true", help="compare GOLDEN.json AFTER.json")
    args = parser.parse_args(argv)
    if args.compare:
        if len(args.paths) != 2:
            parser.error("--compare takes GOLDEN.json AFTER.json")
        return compare(*args.paths)
    if len(args.paths) != 1:
        parser.error("capture takes one output path")
    golden = capture()
    text = json.dumps(golden, indent=2, sort_keys=True) + "\n"
    Path(args.paths[0]).write_text(text, encoding="utf-8")
    failed = failures(golden)
    total = sum(len(rows) for rows in golden.values())
    print(f"Captured {total} statements across {len(golden)} dialects ({len(failed)} failed).")
    for row in failed:
        print(f"  - {row}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
