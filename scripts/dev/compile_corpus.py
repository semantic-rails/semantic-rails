"""Compile every committed query for every supported dialect and record the result.

A zero-change proof for refactors: record before, record after, compare. Nothing connects to a
warehouse; each query compiles through ``Runtime.compile`` (``compile_internal`` for the
engine-form rendered-SQL snapshot inputs) with the package's warehouse swapped per dialect.

The corpus:
- every package's ``examples/`` and ``tests/`` entries under ``configs/semantic_rails/`` and
  ``tests/integration/correctness/`` (the answers ledger included, each case on its package
  variant), and the comparison package's ``queries/*.json``;
- ``examples/*.json``;
- the calls in ``scripts/blind_agent_corpus.json`` (the blocked call, each builder step's
  partial call, and that call with the step's expected patch applied);
- the engine-form inputs of ``tests/semantic_rails/test_rendered_sql_snapshots.py``;
- the payloads of ``scripts/dev/capture_sql_baseline.py``.

Per query and dialect the record holds the rendered SQL, the output columns and the warning and
assumption codes, or the refusal's code and path (a refusal is a result, not a skip), or the exception type
of a crash. An entry's own ``now`` wins; every other query compiles at a pinned ``now``. The
record is sorted JSON with no timings or request ids, so two runs on one commit are
byte-identical, whatever the hash seed or worker count.

Usage (keep records outside the repository):
    uv run python scripts/dev/compile_corpus.py record /tmp/before.json
    # ... change the engine ...
    uv run python scripts/dev/compile_corpus.py record /tmp/after.json
    uv run python scripts/dev/compile_corpus.py compare /tmp/before.json /tmp/after.json

``record`` takes ``--jobs N`` (worker processes, default 4), and ``--dialects a,b`` and
``--only ID_OR_GLOB`` (repeatable) to record a slice; the whole corpus takes about ten seconds
at four workers. ``compare`` prints each changed result as query id, dialect and field, with a
diff (``--brief`` leaves the diffs out), then counts by dialect and field; it exits 1 when
anything differs.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import difflib
import fnmatch
import functools
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from semantic_rails.config import load_package_config
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.request_context import TrustedAttributes, context_from_policy_context
from semantic_rails.runtime import Runtime
from semantic_rails.yaml_loader import load_named_entries

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-10-01T00:00:00Z"
FORMAT = 1
STATUSES = ("compiled", "refused", "crashed")
PACKAGE_GLOBS = (
    "configs/semantic_rails/*/package.yml",
    "tests/integration/correctness/*/package.yml",
)
LEDGER_PACKAGE = "tests/integration/correctness/shop"
LEDGER_DEFAULT_VARIANT = "utc_authored"
JAFFLE = "configs/semantic_rails/jaffle_shop"
COMPARISON = "comparisons/semantic_layers/semantic_rails"
BLIND_CORPUS = "scripts/blind_agent_corpus.json"
SNAPSHOTS = "tests/semantic_rails/test_rendered_sql_snapshots.py"
BASELINE = "scripts/dev/capture_sql_baseline.py"


@dataclasses.dataclass(frozen=True)
class Query:
    id: str
    package: str  # a package directory relative to the repo, ``@variant`` for a ledger variant
    call: dict[str, Any]
    now: str = NOW
    context: dict[str, Any] | None = None
    engine_form: bool = False  # already in the engine's form: compile_internal, not compile

    def fingerprint(self) -> str:
        """The input, so a compare can tell a changed query from a changed result."""
        data = [self.call, self.now, self.context, self.engine_form]
        text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class Unit:
    package_path: str
    dialect: str
    queries: tuple[Query, ...]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _literal(path: Path, name: str) -> Any:
    """A module-level literal, read without importing the module."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(value)
    raise LookupError(f"{_rel(path)} has no literal {name}")


def _package_entries() -> Iterator[Query]:
    packages = sorted(path.parent for pattern in PACKAGE_GLOBS for path in REPO_ROOT.glob(pattern))
    for package in packages:
        rel = _rel(package)
        for kind in ("examples", "tests"):
            entries = load_named_entries(
                package / kind, plural_key=kind, singular_key=kind[:-1], defaults=("now",)
            )
            for entry_id, spec in entries:
                where = rel
                if rel == LEDGER_PACKAGE:
                    where = f"{rel}@{spec.get('variant', LEDGER_DEFAULT_VARIANT)}"
                calls = {"": spec.get("query")}
                if spec.get("kind") == "metric_equals_query":
                    calls = {
                        "#metric_query": spec.get("metric_query", spec.get("query")),
                        "#expected_query": spec.get("expected_query"),
                    }
                for suffix, call in calls.items():
                    if isinstance(call, dict) and call:
                        yield Query(
                            f"{rel}/{kind}::{entry_id}{suffix}",
                            where,
                            call,
                            now=str(spec.get("now") or NOW),
                            context=spec.get("context"),
                        )


def _json_files(directory: str, package: str) -> Iterator[Query]:
    for path in sorted((REPO_ROOT / directory).glob("*.json")):
        yield Query(_rel(path), package, json.loads(path.read_text(encoding="utf-8")))


def _blind_corpus() -> Iterator[Query]:
    corpus = json.loads((REPO_ROOT / BLIND_CORPUS).read_text(encoding="utf-8"))
    blocked = corpus.get("blocked_case") or {}
    if isinstance(blocked.get("query"), dict):
        yield Query(f"{BLIND_CORPUS}::blocked_case.query", JAFFLE, blocked["query"])
    for case in corpus.get("builder_cases") or []:
        partial = case.get("partial_query")
        if not isinstance(partial, dict):
            continue
        label = f"{BLIND_CORPUS}::builder_cases[{case.get('name', '?')}]"
        yield Query(f"{label}.partial_query", JAFFLE, partial)
        patch = case.get("expected_query_patch")
        if isinstance(patch, dict) and patch:
            yield Query(f"{label}.completed", JAFFLE, {**partial, **patch})


def corpus() -> list[Query]:
    """Every query, sorted by id; ids are unique."""
    queries = [
        *_package_entries(),
        *_json_files(f"{COMPARISON}/queries", f"{COMPARISON}/package"),
        *_json_files("examples", JAFFLE),
        *_blind_corpus(),
    ]
    snapshots = _literal(REPO_ROOT / SNAPSHOTS, "SNAPSHOT_CASES")
    queries += [
        Query(f"{SNAPSHOTS}::SNAPSHOT_CASES[{name}]", JAFFLE, call, engine_form=True)
        for name, (call, _sql) in snapshots.items()
    ]
    queries += [
        Query(f"{BASELINE}::PAYLOADS[{entry['metric_id']}]", JAFFLE, entry["payload"])
        for entry in _literal(REPO_ROOT / BASELINE, "PAYLOADS")
    ]
    ids = Counter(query.id for query in queries)
    duplicates = sorted(name for name, count in ids.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate query ids: {duplicates}")
    return sorted(queries, key=lambda query: query.id)


def _package_paths(packages: Iterable[str], root: Path) -> dict[str, str]:
    """Each package key's directory; a ledger variant is written under ``root`` by the
    ledger's own helper, so the two never disagree on what a variant is."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.integration.correctness.conftest import _write_variant

    paths: dict[str, str] = {}
    for key in sorted(set(packages)):
        rel, _, variant = key.partition("@")
        if variant:
            paths[key] = str(_write_variant(root / variant, variant))
        else:
            paths[key] = str(REPO_ROOT / rel)
    return paths


@functools.cache
def _config(package_path: str) -> Any:
    return load_package_config(package_path)


def _trusted(context: Any) -> Any:
    """An entry's context as an embedding host sends it: its ``attributes`` are trusted, so an
    entry that names a row filter's attribute compiles under that filter."""
    if isinstance(context, dict) and "attributes" in context:
        attributes = TrustedAttributes(context["attributes"])
        return context_from_policy_context({**context, "attributes": attributes})
    return context


def _compile(runtime: Runtime, query: Query) -> dict[str, Any]:
    try:
        if query.engine_form:
            payload = {**query.call, "policy_context": {"now": query.now}, "verbosity": "full"}
            out = runtime.compile_internal(payload)
        else:
            out = runtime.compile(
                query.call, now=query.now, context=_trusted(query.context), verbosity="full"
            )
    except SemanticLayerError as exc:  # a path is the call's, or a refused route's relationships
        return {"refusal": {"code": exc.code, "path": (exc.details or {}).get("path") or ""}}
    except Exception as exc:  # recorded so a compare shows it; the run goes on
        return {"crash": type(exc).__name__}
    result = {
        "rendered_sql": out["rendered_sql"],
        "output_columns": out["output_columns"],
        "warnings": [str(issue.get("code", "")) for issue in out.get("warnings") or []],
    }
    for key in ("assumptions", "errors"):
        if out.get(key):
            result[key] = [str(issue.get("code", "")) for issue in out[key]]
    return result


def compile_unit(unit: Unit) -> list[tuple[str, str, dict[str, Any]]]:
    """Compile one package's queries for one dialect."""
    base = _config(unit.package_path)
    config = dataclasses.replace(
        base, package=dataclasses.replace(base.package, warehouse=unit.dialect)
    )
    runtime = Runtime.from_config(
        config, source_path=unit.package_path, package_id=config.package.package_id
    )
    try:
        return [(query.id, unit.dialect, _compile(runtime, query)) for query in unit.queries]
    finally:
        runtime.close()


def _status(result: dict[str, Any]) -> str:
    return "refused" if "refusal" in result else "crashed" if "crash" in result else "compiled"


def record(
    *, dialects: Iterable[str] | None = None, only: Iterable[str] = (), jobs: int = 4
) -> dict[str, Any]:
    """The record of every selected query (an ``only`` entry is an id or a glob over ids) for
    every selected dialect."""
    chosen = sorted(dialects or supported_warehouses())
    unknown = sorted(set(chosen) - set(supported_warehouses()))
    if unknown:
        raise ValueError(f"unknown dialects {unknown}; the dialects are {supported_warehouses()}")
    patterns = list(only)
    queries = [
        query
        for query in corpus()
        if not patterns or any(query.id == p or fnmatch.fnmatchcase(query.id, p) for p in patterns)
    ]
    by_package: dict[str, list[Query]] = {}
    for query in queries:
        by_package.setdefault(query.package, []).append(query)
    with TemporaryDirectory(prefix="compile-corpus-") as root:
        paths = _package_paths(by_package, Path(root))
        units = [
            Unit(paths[key], dialect, tuple(rows))
            for key, rows in sorted(by_package.items())
            for dialect in chosen
        ]
        if jobs > 1:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                batches = list(pool.map(compile_unit, units))
        else:
            batches = [compile_unit(unit) for unit in units]
    results: dict[str, dict[str, Any]] = {
        query.id: {"input": query.fingerprint(), "results": {}} for query in queries
    }
    for batch in batches:
        for query_id, dialect, result in batch:
            results[query_id]["results"][dialect] = result
    pairs = [
        (dialect, _status(result))
        for row in results.values()
        for dialect, result in row["results"].items()
    ]
    counts, totals = Counter(pairs), Counter(status for _, status in pairs)
    return {
        "format": FORMAT,
        "now": NOW,
        "dialects": chosen,
        "summary": {
            "queries": len(queries),
            "results": len(queries) * len(chosen),
            **{status: totals[status] for status in STATUSES},
            "by_dialect": {
                dialect: {status: counts[(dialect, status)] for status in STATUSES}
                for dialect in chosen
            },
        },
        "queries": results,
    }


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def _outcome(result: dict[str, Any] | None) -> Any:
    """``compiled``, the refusal or crash itself, or None for a result not recorded."""
    if result is None or _status(result) == "compiled":
        return None if result is None else "compiled"
    return {key: result[key] for key in ("refusal", "crash") if key in result}


def _detail(before: Any, after: Any) -> list[str]:
    """One line for short values, else a unified diff (SQL by line, JSON indented)."""
    short = [json.dumps(value, sort_keys=True, default=str) for value in (before, after)]
    if all(len(text) <= 120 and "\\n" not in text for text in short):
        return [f"    {short[0]} -> {short[1]}"]
    old, new = (
        value.splitlines()
        if isinstance(value, str)
        else json.dumps(value, indent=1, sort_keys=True, default=str).splitlines()
        for value in (before, after)
    )
    diff = difflib.unified_diff(old, new, "before", "after", n=2, lineterm="")
    return [f"    {line}" for line in diff]


def differences(
    golden: dict[str, Any], after: dict[str, Any]
) -> list[tuple[str, str, str, Any, Any]]:
    """Every (query id, dialect, field, before, after) that differs.

    Dialect and field are empty for an added or removed query; field is ``input`` for a query
    whose input changed, and ``outcome`` for a result that changed between compiled, refused
    and crashed (its fields are then not compared)."""
    rows: list[tuple[str, str, str, Any, Any]] = []
    old_queries, new_queries = golden["queries"], after["queries"]
    for query_id in sorted(old_queries.keys() | new_queries.keys()):
        old, new = old_queries.get(query_id), new_queries.get(query_id)
        if old is None or new is None:
            rows.append((query_id, "", "", old and "recorded", new and "recorded"))
            continue
        if old["input"] != new["input"]:
            rows.append((query_id, "", "input", old["input"], new["input"]))
        for dialect in sorted(old["results"].keys() | new["results"].keys()):
            before, now = old["results"].get(dialect), new["results"].get(dialect)
            if before is None or now is None or _status(before) != _status(now):
                rows.append((query_id, dialect, "outcome", _outcome(before), _outcome(now)))
                continue
            for field in sorted(before.keys() | now.keys()):
                if before.get(field) != now.get(field):
                    rows.append((query_id, dialect, field, before.get(field), now.get(field)))
    return rows


def compare(golden_path: str, after_path: str, *, brief: bool = False) -> int:
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    for key in ("format", "now", "dialects"):
        if golden.get(key) != after.get(key):
            print(f"NOTE: {key} differs: {golden.get(key)!r} -> {after.get(key)!r}")
    rows = differences(golden, after)
    queries: Counter[str] = Counter()
    by_dialect_field: Counter[tuple[str, str]] = Counter()
    for query_id, dialect, field, before, now in rows:
        if not dialect:
            label = "INPUT CHANGED" if field else "REMOVED" if now is None else "ADDED"
            queries[label.lower()] += 1
            print(f"{label} {query_id}")
            continue
        by_dialect_field[(dialect, field)] += 1
        print(f"CHANGED {query_id} [{dialect}] {field}")
        if not brief:
            print("\n".join(_detail(before, now)))
    if not rows:
        summary = golden["summary"]
        print(
            f"IDENTICAL: {summary['queries']} queries x {len(golden['dialects'])} dialects "
            f"({summary['compiled']} compiled, {summary['refused']} refused, "
            f"{summary['crashed']} crashed)"
        )
        return 0
    changed = len({(row[0], row[1]) for row in rows if row[1]})
    counts = ", ".join(f"{n} {label}" for label, n in sorted(queries.items()))
    print(f"DIFFERENT: {changed} results changed{f'; queries: {counts}' if counts else ''}")
    for (dialect, field), count in sorted(by_dialect_field.items()):
        print(f"  {dialect:<12} {field:<16} {count}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rec = commands.add_parser("record", help="write the record to OUT")
    rec.add_argument("out", metavar="OUT")
    rec.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    rec.add_argument("--dialects", default="", help="comma-separated; default every dialect")
    rec.add_argument("--only", action="append", default=[], metavar="ID_OR_GLOB")
    cmp = commands.add_parser("compare", help="compare two records")
    cmp.add_argument("golden", metavar="GOLDEN")
    cmp.add_argument("after", metavar="AFTER")
    cmp.add_argument("--brief", action="store_true", help="list changes without diffs")
    args = parser.parse_args(argv)
    if args.command == "compare":
        return compare(args.golden, args.after, brief=args.brief)
    started = time.perf_counter()
    dialects = [name.strip() for name in args.dialects.split(",") if name.strip()]
    data = record(dialects=dialects or None, only=args.only, jobs=args.jobs)
    Path(args.out).write_text(dumps(data), encoding="utf-8")
    summary = data["summary"]
    print(
        f"Recorded {summary['queries']} queries x {len(data['dialects'])} dialects = "
        f"{summary['results']} results: {summary['compiled']} compiled, "
        f"{summary['refused']} refused, {summary['crashed']} crashed "
        f"({time.perf_counter() - started:.1f}s) -> {args.out}"
    )
    for dialect, counts in summary["by_dialect"].items():
        print(f"  {dialect:<12} {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
