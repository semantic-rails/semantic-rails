"""Regenerate SNAPSHOT_CASES inside test_rendered_sql_snapshots.py.

Reads the current `SNAPSHOT_CASES` dict (as data — the queries are still
the source of truth), runs each through `compile_query` against jaffle_shop,
and writes a fresh dict literal with the new rendered SQL.

Use after a deliberate renderer / compiler change that updates the SQL
shape but preserves correctness.

Usage:
    UV_INDEX_URL=https://pypi.org/simple uv run python scripts/regen_snapshot_cases.py
"""

from __future__ import annotations

import importlib
import pprint
import shutil
import uuid
from pathlib import Path

import yaml

from semantic_rails import config as config_module
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.registry import Registry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = REPO_ROOT / "tests/semantic_rails/test_rendered_sql_snapshots.py"


def _load_jaffle_config():
    package_id = "jaffle_shop"
    src = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    tmp = REPO_ROOT / ".tmp-snapshot" / uuid.uuid4().hex
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, tmp)
    package_path = tmp / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    default_db = tmp / f"{package_id}.duckdb"
    package["default_db"] = str(default_db)
    raw["package"] = package
    package_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    seeded = Path(resolve_repo_path("data/jaffle_shop.duckdb"))
    if seeded.exists():
        shutil.copy2(seeded, default_db)
    config_module.list_package_paths = lambda: {package_id: str(tmp)}  # type: ignore[assignment]
    return load_package_config(str(tmp))


def main() -> int:
    # Import the existing SNAPSHOT_CASES to get the queries.
    spec = importlib.util.spec_from_file_location("_snap", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases: dict = dict(module.SNAPSHOT_CASES)

    config = _load_jaffle_config()
    registry = Registry(config)

    new_cases: dict = {}
    for name, (query, _old_sql) in cases.items():
        compiled = compile_query(config, registry, query)
        new_cases[name] = (query, compiled["sql"])

    # Format as a Python literal. Use pprint for the dict, but render multiline
    # strings as triple-quoted to keep them readable in the test file.
    body_lines: list[str] = []
    for name in sorted(new_cases):
        query, sql = new_cases[name]
        query_repr = pprint.pformat(query, indent=2, width=120)
        sql_lit = repr(sql)  # single-line repr
        body_lines.append(f"    {name!r}: (\n        {query_repr},\n        {sql_lit},\n    ),")
    body = "\n".join(body_lines)

    text = TARGET.read_text(encoding="utf-8")
    # Find SNAPSHOT_CASES = { ... }
    start_marker = "SNAPSHOT_CASES = "
    start = text.index(start_marker)
    # Walk past the literal until the matching brace.
    open_brace = text.index("{", start)
    depth = 0
    end = open_brace
    in_string: str | None = None
    i = open_brace
    while i < len(text):
        ch = text[i]
        if in_string is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        else:
            if ch in ("'", '"'):
                in_string = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        i += 1

    new_literal = "SNAPSHOT_CASES = {\n" + body + "\n}"
    new_text = text[:start] + new_literal + text[end:]
    TARGET.write_text(new_text, encoding="utf-8")
    print(f"Rewrote SNAPSHOT_CASES in {TARGET} ({len(new_cases)} cases).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
