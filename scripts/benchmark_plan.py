"""Benchmark the public ``plan`` intent surface.

Measures:

* intent coverage and status breakdown on the blind-agent corpus
* default ``detail="best"`` aggregate response size versus ``detail="full"``
* warm ``plan -> compile`` latency and cache-hit behavior

Run:

    uv run python scripts/benchmark_plan.py
    uv run python scripts/benchmark_plan.py --gate
    uv run python scripts/benchmark_plan.py --gate --markdown-output docs/AGENTIC_GOVERNANCE_SCORECARD.md

In ``--gate`` mode, the script exits non-zero if any of these regress:

* an expected-answer corpus case is not actionable
* ``detail="best"`` is not smaller than ``detail="full"`` overall
* expected semantic IDs from the blind corpus are missing from the best plan
* expected Query IR fields from the blind corpus are missing from the best plan
* forbidden Query IR paths appear in the best plan
* a clarification case is marked ready to execute
* forbidden hallucinated IDs appear in the best plan
* any ``status="ok"`` plan fails to produce a compile cache hit
* cache-hit compile p95 exceeds the configured threshold
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _build_runtime(package_id: str = "jaffle_shop"):
    """Build a Runtime against a fresh package copy.

    Mirrors ``tests/semantic_rails/conftest.py`` so benchmark results
    line up with regression tests and avoid mutating checked-in package
    state.
    """

    from semantic_rails import config as cfg
    from semantic_rails.config import resolve_repo_path
    from semantic_rails.runtime import Runtime

    tmp = Path(tempfile.mkdtemp())
    run_dir = tmp / package_id / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    src = Path(resolve_repo_path(f"configs/semantic_rails/{package_id}"))
    target = run_dir / package_id
    shutil.copytree(src, target)
    package_path = target / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text()) or {})
    pkg = dict(raw.get("package", {}) or {})
    default_db = target / f"{package_id}.duckdb"
    pkg["default_db"] = f"{package_id}.duckdb"
    raw["package"] = pkg
    package_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    seeded = Path(resolve_repo_path("data/jaffle_shop.duckdb"))
    if seeded.exists():
        shutil.copy2(seeded, default_db)
    cfg.list_package_paths = lambda: {package_id: str(target)}
    return Runtime(package_id)


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str, separators=(",", ":")))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ranked = sorted(values)
    index = round((len(ranked) - 1) * pct)
    return ranked[index]


def _load_cases() -> list[dict[str, Any]]:
    corpus = json.loads((REPO_ROOT / "scripts" / "blind_agent_corpus.json").read_text())
    cases = [
        {
            "name": case.get("name") or case.get("intent", "")[:40],
            "intent": str(case.get("intent", "")),
            "expected": "answer",
            "expected_resolved_ids": _expected_ids_for_case(case),
            "forbidden_ids": list(case.get("forbidden_ids", []) or []),
            "expected_query_patch": dict(case.get("expected_query_patch", {}) or {}),
            "forbidden_query_paths": list(case.get("forbidden_query_paths", []) or []),
        }
        for case in corpus.get("benchmark_cases", [])
    ]
    cases.extend(
        [
            {
                "name": "scope_reject_linux_kernel",
                "intent": "compile the linux kernel",
                "expected": "reject",
            },
            {
                "name": "simple_aov_by_store",
                "intent": "average order value by store",
                "expected": "answer",
            },
            {
                "name": "ambiguous_customer_status",
                "intent": "monthly orders by customer status",
                "expected": "answer",
            },
            {
                "name": "orders_by_store_without_time_bucket",
                "intent": "orders by store",
                "expected": "answer",
                "expected_query_patch": {
                    "group_by": ["dimension.jaffle_store_name"],
                },
                "forbidden_query_paths": ["time"],
            },
            {
                "name": "total_orders_without_time_bucket",
                "intent": "total orders",
                "expected": "answer",
                "expected_resolved_ids": ["measure.jaffle.order_count"],
                "forbidden_query_paths": ["time"],
            },
            {
                "name": "total_revenue_without_time_bucket",
                "intent": "total revenue",
                "expected": "answer",
                "expected_resolved_ids": ["measure.jaffle.revenue_usd"],
                "forbidden_query_paths": ["time"],
            },
            {
                "name": "aov_without_time_bucket",
                "intent": "average order value",
                "expected": "answer",
                "expected_resolved_ids": ["metric.sales.aov_usd"],
                "forbidden_query_paths": ["time"],
            },
            {
                "name": "unresolved_time_window_requires_clarification",
                "intent": "revenue by store over the last few weeks",
                "expected": "clarify",
                "expected_status": "low_confidence",
                "forbid_ready_for_execute": True,
            },
        ]
    )
    return cases


def _expected_ids_for_case(case: dict[str, Any]) -> list[str]:
    """Return every semantic object the corpus says must survive planning.

    Older corpus rows use ``expected_resolved_ids`` for one-query plans and
    ``expected_bundle_object_ids`` for comparison bundles. The benchmark gate
    should treat both as fidelity requirements, otherwise a comparison plan can
    be actionable while silently resolving the wrong governed objects.
    """

    ids: list[str] = []
    for key in ("expected_resolved_ids", "expected_bundle_object_ids"):
        for object_id in list(case.get(key, []) or []):
            if object_id and object_id not in ids:
                ids.append(str(object_id))
    return ids


def _is_actionable(payload: dict[str, Any]) -> bool:
    return payload.get("status") in {"ok", "low_confidence", "unrealizable"}


def _compile_after_plan(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "ok":
        return {"attempted": False}
    best = payload.get("best") or {}
    query = dict(best.get("query_ir") or {})
    if not query:
        return {"attempted": False}
    query["verbosity"] = "full"
    started = time.perf_counter()
    try:
        compiled = runtime.compile(query)
    except Exception as exc:  # noqa: BLE001 - benchmark records failures instead of aborting
        return {
            "attempted": True,
            "ok": False,
            "cache_hit": False,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "lookup_ms": 0.0,
            "error": str(exc),
        }
    elapsed_ms = (time.perf_counter() - started) * 1000
    stats = dict(compiled.get("compile_stats") or {})
    return {
        "attempted": True,
        "ok": bool(compiled.get("ok", True)),
        "cache_hit": bool(stats.get("cache_hit")),
        "elapsed_ms": elapsed_ms,
        "lookup_ms": float(stats.get("cache_lookup_ms") or 0.0),
    }


def _ids_in_plan(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    best = dict(payload.get("best") or {})
    for row in list(best.get("resolved", []) or []):
        if isinstance(row, dict) and row.get("id"):
            ids.add(str(row["id"]))
    query = dict(best.get("query_ir") or {})
    for item in list(query.get("select", []) or []):
        if not isinstance(item, dict):
            continue
        expression = item.get("expression")
        if not isinstance(expression, dict):
            continue
        for key in ("metric", "measure"):
            if expression.get(key):
                ids.add(str(expression[key]))
    for dim_id in list(query.get("group_by", []) or []):
        ids.add(str(dim_id))
    for item in list(query.get("where", []) or []):
        if isinstance(item, dict) and item.get("field"):
            ids.add(str(item["field"]))
    _collect_expression_ids(query.get("metric_filters"), ids)
    _collect_expression_ids(best.get("bundle"), ids)
    _collect_expression_ids(best.get("query_patches"), ids)
    _collect_expression_ids(payload.get("alternatives"), ids)
    return ids


def _collect_expression_ids(value: Any, ids: set[str]) -> None:
    if isinstance(value, dict):
        for key in ("metric", "measure", "entity", "field", "dimension", "temporal_role"):
            if value.get(key):
                ids.add(str(value[key]))
        for child in value.values():
            _collect_expression_ids(child, ids)
    elif isinstance(value, list):
        for child in value:
            _collect_expression_ids(child, ids)


def _query_patch_misses(expected: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    if not expected:
        return []
    query = dict((payload.get("best") or {}).get("query_ir") or {})
    return list(_missing_paths(expected, query))


def _unexpected_query_paths(paths: list[str], payload: dict[str, Any]) -> list[str]:
    query: Any = (payload.get("best") or {}).get("query_ir") or {}
    hits: list[str] = []
    for path in paths:
        current = query
        found = True
        for part in str(path).split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found:
            hits.append(str(path))
    return hits


def _missing_paths(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [path or "<root>"]
        misses: list[str] = []
        for key, value in expected.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in actual:
                misses.append(child_path)
            else:
                misses.extend(_missing_paths(value, actual[key], child_path))
        return misses
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [path or "<root>"]
        misses = []
        for value in expected:
            if not any(_value_contains(candidate, value) for candidate in actual):
                misses.append(f"{path} contains {value!r}")
        return misses
    if actual != expected:
        return [path or "<root>"]
    return []


def _value_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _value_contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return all(
            any(_value_contains(candidate, value) for candidate in actual) for value in expected
        )
    return actual == expected


def _run_case(runtime: Any, case: dict[str, Any]) -> dict[str, Any]:
    from semantic_rails.planner import plan_payload

    intent = case["intent"]
    plan_started = time.perf_counter()
    best_payload = plan_payload(runtime, intent=intent, detail="best")
    plan_ms = (time.perf_counter() - plan_started) * 1000
    full_payload = plan_payload(runtime, intent=intent, detail="full", limit=3)
    compile_probe = _compile_after_plan(runtime, best_payload)
    missing_expected_ids = [
        object_id
        for object_id in list(case.get("expected_resolved_ids", []) or [])
        if object_id not in _ids_in_plan(best_payload)
    ]
    forbidden_hits = [
        object_id
        for object_id in list(case.get("forbidden_ids", []) or [])
        if object_id in _ids_in_plan(best_payload)
    ]
    missing_expected_query_patch = _query_patch_misses(
        dict(case.get("expected_query_patch", {}) or {}), best_payload
    )
    unexpected_query_paths = _unexpected_query_paths(
        list(case.get("forbidden_query_paths", []) or []), best_payload
    )
    ready_for = list((best_payload.get("next") or {}).get("ready_for") or [])
    return {
        **case,
        "status": best_payload.get("status"),
        "full_status": full_payload.get("status"),
        "pattern": (best_payload.get("best") or {}).get("pattern", ""),
        "full_pattern": (full_payload.get("best") or {}).get("pattern", ""),
        "actionable": _is_actionable(best_payload),
        "best_bytes": _payload_size(best_payload),
        "full_bytes": _payload_size(full_payload),
        "plan_ms": plan_ms,
        "compile_cache_hit": compile_probe.get("cache_hit"),
        "compile_ms": compile_probe.get("elapsed_ms", 0.0),
        "compile_lookup_ms": compile_probe.get("lookup_ms", 0.0),
        "compile_attempted": compile_probe.get("attempted", False),
        "missing_expected_ids": missing_expected_ids,
        "forbidden_hits": forbidden_hits,
        "missing_expected_query_patch": missing_expected_query_patch,
        "unexpected_query_paths": unexpected_query_paths,
        "unexpected_execute_ready": bool(
            case.get("forbid_ready_for_execute") and "execute" in ready_for
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--gate", action="store_true", help="Exit non-zero on regression.")
    parser.add_argument(
        "--max-best-bytes",
        type=int,
        default=6_500,
        help="Maximum allowed bytes for any detail='best' payload.",
    )
    parser.add_argument(
        "--max-cache-hit-p95-ms",
        type=float,
        default=25.0,
        help="Maximum p95 latency for compile calls that should hit the plan-warmed cache.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for a machine-readable JSON scorecard.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Optional path for a Markdown scorecard suitable for release evidence.",
    )
    args = parser.parse_args(argv)

    runtime = _build_runtime()
    try:
        rows = [_run_case(runtime, case) for case in _load_cases()]
    finally:
        runtime.close()

    _print_report(rows)
    if args.output:
        _write_scorecard(
            args.output,
            rows,
            max_best_bytes=args.max_best_bytes,
            max_cache_hit_p95_ms=args.max_cache_hit_p95_ms,
        )
    if args.markdown_output:
        _write_markdown_scorecard(
            args.markdown_output,
            rows,
            max_best_bytes=args.max_best_bytes,
            max_cache_hit_p95_ms=args.max_cache_hit_p95_ms,
        )
    if args.gate:
        return _gate_exit_code(
            rows,
            max_best_bytes=args.max_best_bytes,
            max_cache_hit_p95_ms=args.max_cache_hit_p95_ms,
        )
    return 0


def _scorecard_summary(
    rows: list[dict[str, Any]],
    *,
    max_best_bytes: int,
    max_cache_hit_p95_ms: float,
) -> dict[str, Any]:
    expected_answer = [row for row in rows if row["expected"] == "answer"]
    ok_rows = [row for row in rows if row["status"] == "ok" and row["compile_attempted"]]
    hit_latencies = [float(row["compile_ms"]) for row in ok_rows if row["compile_cache_hit"]]
    return {
        "case_count": len(rows),
        "expected_answer_count": len(expected_answer),
        "actionable_expected_answers": sum(1 for row in expected_answer if row["actionable"]),
        "expected_semantic_id_hits": sum(
            1 for row in expected_answer if not row.get("missing_expected_ids")
        ),
        "expected_query_patch_hits": sum(
            1 for row in expected_answer if not row.get("missing_expected_query_patch")
        ),
        "status_breakdown": {
            status: sum(1 for row in rows if row["status"] == status)
            for status in sorted({str(row["status"]) for row in rows})
        },
        "detail_best_total_bytes": sum(int(row["best_bytes"]) for row in rows),
        "detail_full_total_bytes": sum(int(row["full_bytes"]) for row in rows),
        "max_detail_best_bytes": max(int(row["best_bytes"]) for row in rows),
        "plan_latency_p95_ms": _percentile([float(row["plan_ms"]) for row in rows], 0.95),
        "plan_warmed_compile_cache_hit_p95_ms": _percentile(hit_latencies, 0.95),
        "gate_thresholds": {
            "max_best_bytes": max_best_bytes,
            "max_cache_hit_p95_ms": max_cache_hit_p95_ms,
        },
    }


def _write_scorecard(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    max_best_bytes: int,
    max_cache_hit_p95_ms: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "benchmark": "plan",
                "corpus": "scripts/blind_agent_corpus.json",
                "summary": _scorecard_summary(
                    rows,
                    max_best_bytes=max_best_bytes,
                    max_cache_hit_p95_ms=max_cache_hit_p95_ms,
                ),
                "cases": rows,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    print()
    print(f"Wrote scorecard to {_display_path(path)}")


def _write_markdown_scorecard(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    max_best_bytes: int,
    max_cache_hit_p95_ms: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _scorecard_markdown(
            rows,
            max_best_bytes=max_best_bytes,
            max_cache_hit_p95_ms=max_cache_hit_p95_ms,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Wrote Markdown scorecard to {_display_path(path)}")


def _display_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        return resolved.relative_to(REPO_ROOT)
    return path


def _scorecard_markdown(
    rows: list[dict[str, Any]],
    *,
    max_best_bytes: int,
    max_cache_hit_p95_ms: float,
) -> str:
    summary = _scorecard_summary(
        rows,
        max_best_bytes=max_best_bytes,
        max_cache_hit_p95_ms=max_cache_hit_p95_ms,
    )
    status_breakdown = ", ".join(
        f"{status}={count}" for status, count in summary["status_breakdown"].items()
    )
    lines = [
        "# Agentic Governance Benchmark Scorecard",
        "",
        "Generated by `scripts/benchmark_plan.py` from the checked-in blind-agent corpus and",
        "adversarial probes. This measures the Semantic Rails governed planning loop. It is",
        "not a competitor benchmark unless external systems are run and recorded separately.",
        "",
        "## Summary",
        "",
        f"- cases: {summary['case_count']}",
        (
            "- actionable expected answers: "
            f"{summary['actionable_expected_answers']}/{summary['expected_answer_count']}"
        ),
        (
            "- expected semantic-id hits: "
            f"{summary['expected_semantic_id_hits']}/{summary['expected_answer_count']}"
        ),
        (
            "- expected query-patch hits: "
            f"{summary['expected_query_patch_hits']}/{summary['expected_answer_count']}"
        ),
        f"- status breakdown: {status_breakdown}",
        f"- detail='best' total bytes: {summary['detail_best_total_bytes']}",
        f"- detail='full' total bytes: {summary['detail_full_total_bytes']}",
        f"- max detail='best' bytes: {summary['max_detail_best_bytes']}",
        f"- plan latency p95: {summary['plan_latency_p95_ms']:.3f}ms",
        (
            "- plan-warmed compile cache-hit p95: "
            f"{summary['plan_warmed_compile_cache_hit_p95_ms']:.3f}ms"
        ),
        (
            "- gate thresholds: "
            f"max best bytes={max_best_bytes}, cache-hit p95={max_cache_hit_p95_ms:.3f}ms"
        ),
        "",
        "## Case Results",
        "",
        "| case | expected | status | pattern | best bytes | full bytes | semantic misses | patch misses | cache hit | compile ms |",
        "| --- | --- | --- | --- | ---: | ---: | --- | --- | --- | ---: |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(str(row.get("name", ""))),
                    _md_cell(str(row.get("expected", ""))),
                    _md_cell(str(row.get("status", ""))),
                    _md_cell(str(row.get("pattern", ""))),
                    str(row.get("best_bytes", "")),
                    str(row.get("full_bytes", "")),
                    _md_cell(str(row.get("missing_expected_ids", []))),
                    _md_cell(str(row.get("missing_expected_query_patch", []))),
                    _md_cell(str(row.get("compile_cache_hit", ""))),
                    f"{float(row.get('compile_ms') or 0.0):.3f}",
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _gate_exit_code(
    rows: list[dict[str, Any]],
    *,
    max_best_bytes: int,
    max_cache_hit_p95_ms: float,
) -> int:
    failures: list[str] = []
    expected_answer = [row for row in rows if row["expected"] == "answer"]
    missed = [row["name"] for row in expected_answer if not row["actionable"]]
    if missed:
        failures.append(f"coverage regression: non-actionable expected answers: {missed}")

    best_total = sum(int(row["best_bytes"]) for row in rows)
    full_total = sum(int(row["full_bytes"]) for row in rows)
    if best_total >= full_total:
        failures.append(f"size regression: best total={best_total} B >= full total={full_total} B")
    per_case_size_regressions = [
        f"{row['name']} best={row['best_bytes']} B full={row['full_bytes']} B"
        for row in rows
        if row["expected"] == "answer" and int(row["best_bytes"]) >= int(row["full_bytes"])
    ]
    if per_case_size_regressions:
        failures.append(
            f"size regression: expected-answer detail='best' payloads not smaller than full: {per_case_size_regressions}"
        )
    best_full_status_regressions = [
        f"{row['name']} best={row['status']} full={row['full_status']}"
        for row in expected_answer
        if row.get("full_status") == "ok" and row.get("status") != "ok"
    ]
    if best_full_status_regressions:
        failures.append(
            "status regression: detail='best' missed a validating full-detail plan: "
            f"{best_full_status_regressions}"
        )
    oversized = [
        f"{row['name']}={row['best_bytes']} B"
        for row in rows
        if int(row["best_bytes"]) > max_best_bytes
    ]
    if oversized:
        failures.append(
            f"size regression: detail='best' payloads exceed {max_best_bytes} B: {oversized}"
        )
    fidelity_misses = [
        f"{row['name']} missing={row['missing_expected_ids']}"
        for row in expected_answer
        if row.get("missing_expected_ids")
    ]
    if fidelity_misses:
        failures.append(f"fidelity regression: expected semantic IDs missing: {fidelity_misses}")
    forbidden_hits = [
        f"{row['name']} hit={row['forbidden_hits']}"
        for row in expected_answer
        if row.get("forbidden_hits")
    ]
    if forbidden_hits:
        failures.append(f"fidelity regression: forbidden semantic IDs appeared: {forbidden_hits}")
    query_patch_misses = [
        f"{row['name']} missing={row['missing_expected_query_patch']}"
        for row in expected_answer
        if row.get("missing_expected_query_patch")
    ]
    if query_patch_misses:
        failures.append(
            f"fidelity regression: expected Query IR patch missing: {query_patch_misses}"
        )
    forbidden_query_path_hits = [
        f"{row['name']} hit={row['unexpected_query_paths']}"
        for row in rows
        if row.get("unexpected_query_paths")
    ]
    if forbidden_query_path_hits:
        failures.append(
            f"fidelity regression: forbidden Query IR paths appeared: {forbidden_query_path_hits}"
        )
    status_misses = [
        f"{row['name']} expected={row['expected_status']} actual={row['status']}"
        for row in rows
        if row.get("expected_status") and row.get("status") != row.get("expected_status")
    ]
    if status_misses:
        failures.append(f"status regression: clarification contract changed: {status_misses}")
    unsafe_ready = [row["name"] for row in rows if row.get("unexpected_execute_ready")]
    if unsafe_ready:
        failures.append(f"safety regression: clarification plans marked executable: {unsafe_ready}")

    ok_rows = [row for row in rows if row["status"] == "ok" and row["compile_attempted"]]
    cache_misses = [row["name"] for row in ok_rows if not row["compile_cache_hit"]]
    if cache_misses:
        failures.append(f"cache regression: plan-warmed compile misses: {cache_misses}")
    hit_latencies = [float(row["compile_ms"]) for row in ok_rows if row["compile_cache_hit"]]
    p95 = _percentile(hit_latencies, 0.95)
    if hit_latencies and p95 > max_cache_hit_p95_ms:
        failures.append(
            f"latency regression: cache-hit compile p95={p95:.3f}ms > {max_cache_hit_p95_ms:.3f}ms"
        )

    if failures:
        print()
        print("### Gate failures")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print()
    print("### Gate: PASS")
    return 0


def _print_report(rows: list[dict[str, Any]]) -> None:
    print()
    print("# Benchmark: `plan`")
    print()
    print(f"Cases: {len(rows)} from `scripts/blind_agent_corpus.json` plus adversarial probes")
    print()
    _print_table(rows)
    _print_summary(rows)


def _print_table(rows: list[dict[str, Any]]) -> None:
    cols = [
        "name",
        "expected",
        "status",
        "pattern",
        "best_bytes",
        "full_bytes",
        "missing_expected_ids",
        "missing_expected_query_patch",
        "compile_cache_hit",
        "compile_ms",
    ]
    widths = {col: max(len(col), max(len(str(row.get(col, ""))) for row in rows)) for col in cols}
    header = " | ".join(col.ljust(widths[col]) for col in cols)
    sep = "-+-".join("-" * widths[col] for col in cols)
    print(header)
    print(sep)
    for row in rows:
        display = dict(row)
        display["compile_ms"] = f"{float(row.get('compile_ms') or 0.0):.3f}"
        print(" | ".join(str(display.get(col, "")).ljust(widths[col]) for col in cols))


def _print_summary(rows: list[dict[str, Any]]) -> None:
    statuses = sorted({str(row["status"]) for row in rows})
    best_sizes = [int(row["best_bytes"]) for row in rows]
    full_sizes = [int(row["full_bytes"]) for row in rows]
    plan_latencies = [float(row["plan_ms"]) for row in rows]
    cache_hit_compile_latencies = [
        float(row["compile_ms"])
        for row in rows
        if row["status"] == "ok" and row["compile_cache_hit"]
    ]
    print()
    print("### Summary")
    print(
        f"- actionable expected answers: {sum(1 for r in rows if r['expected'] == 'answer' and r['actionable'])}/{sum(1 for r in rows if r['expected'] == 'answer')}"
    )
    print(
        f"- expected semantic-id hits: "
        f"{sum(1 for r in rows if r['expected'] == 'answer' and not r.get('missing_expected_ids'))}/"
        f"{sum(1 for r in rows if r['expected'] == 'answer')}"
    )
    print(
        f"- expected query-patch hits: "
        f"{sum(1 for r in rows if r['expected'] == 'answer' and not r.get('missing_expected_query_patch'))}/"
        f"{sum(1 for r in rows if r['expected'] == 'answer')}"
    )
    print(
        "- status breakdown: "
        + ", ".join(
            f"{status}={sum(1 for row in rows if row['status'] == status)}" for status in statuses
        )
    )
    print(
        f"- detail='best' bytes: median={int(statistics.median(best_sizes))}, "
        f"max={max(best_sizes)}, total={sum(best_sizes)}"
    )
    print(
        f"- detail='full' bytes: median={int(statistics.median(full_sizes))}, "
        f"max={max(full_sizes)}, total={sum(full_sizes)}"
    )
    print(
        f"- plan latency: median={statistics.median(plan_latencies):.3f}ms, "
        f"p95={_percentile(plan_latencies, 0.95):.3f}ms"
    )
    if cache_hit_compile_latencies:
        print(
            f"- plan-warmed compile cache-hit latency: "
            f"median={statistics.median(cache_hit_compile_latencies):.3f}ms, "
            f"p95={_percentile(cache_hit_compile_latencies, 0.95):.3f}ms"
        )


if __name__ == "__main__":
    raise SystemExit(main())
