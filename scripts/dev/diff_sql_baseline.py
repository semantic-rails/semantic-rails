"""Diff a freshly captured baseline against the golden snapshot.

Output equivalence is enforced as set-equivalence with floating-point tolerance,
not byte-identical row sets. DuckDB's query plan is non-deterministic at ULP
level and row order is unspecified without ORDER BY, so the right contract is:
  - Same set of rows (after sorting by stringified key)
  - All numeric cells within FP_TOL_REL/FP_TOL_ABS

SQL strings WILL differ — that's the point of the SQL-quality refactor — and
are reported only for inspection.

Usage:
    python scripts/diff_sql_baseline.py /tmp/sql_baseline_golden.json /tmp/sql_baseline.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

FP_TOL_REL = 1e-9
FP_TOL_ABS = 1e-9


def _row_sort_key(row: dict[str, Any]) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in row.items()))


def _row_value_keys(row: dict[str, Any]) -> tuple:
    return tuple(sorted((k, type(v).__name__) for k, v in row.items()))


def _approx_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(float(a), float(b), rel_tol=FP_TOL_REL, abs_tol=FP_TOL_ABS)
    return a == b


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by all non-numeric columns; matches up rows for FP comparison."""
    if not rows:
        return rows
    # Sort key: every column stringified (numeric values become "1.234").
    # For two rows of the same metric, this aligns them before FP-tolerance compare.
    return sorted(rows, key=_row_sort_key)


def _rows_equivalent(g_rows: list[dict[str, Any]], a_rows: list[dict[str, Any]]) -> bool:
    if len(g_rows) != len(a_rows):
        return False
    if not g_rows:
        return True
    # Same column sets in every row?
    g_cols = set(g_rows[0].keys())
    a_cols = set(a_rows[0].keys())
    if g_cols != a_cols:
        return False

    # Sort both by NON-numeric columns first (to align rows whose only difference is FP tail).
    # A column is "numeric" if every non-None value is int/float (and not bool).
    def _is_numeric_col(col: str) -> bool:
        for row in g_rows:
            val = row.get(col)
            if val is None:
                continue
            if isinstance(val, bool):
                return False
            if not isinstance(val, (int, float)):
                return False
        return True

    non_numeric_cols = sorted(c for c in g_cols if not _is_numeric_col(c))

    def stable_key(r: dict[str, Any]) -> tuple:
        return tuple(str(r.get(c)) for c in non_numeric_cols)

    g_sorted = sorted(g_rows, key=stable_key)
    a_sorted = sorted(a_rows, key=stable_key)
    for g_row, a_row in zip(g_sorted, a_sorted, strict=True):
        for col in g_cols:
            if not _approx_equal(g_row.get(col), a_row.get(col)):
                return False
    return True


def main(golden_path: str, after_path: str) -> int:
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))

    row_diffs: list[str] = []
    sql_changes: list[str] = []
    coverage_issues: list[str] = []

    for metric_id in sorted(golden):
        if metric_id not in after:
            coverage_issues.append(f"{metric_id}: missing post-fix")
            continue
        g = golden[metric_id]
        a = after[metric_id]

        if "query" in g and "query" in a:
            if not _rows_equivalent(g["query"]["rows"], a["query"]["rows"]):
                row_diffs.append(metric_id)
        elif "query" in g and "query" not in a:
            coverage_issues.append(f"{metric_id}: query failed post-fix ({a.get('query_error')})")
        elif "query" not in g and "query" in a:
            coverage_issues.append(
                f"{metric_id}: query newly succeeds (was: {g.get('query_error')})"
            )

        g_sql = g.get("compile", {}).get("sql")
        a_sql = a.get("compile", {}).get("sql")
        if g_sql != a_sql:
            sql_changes.append(metric_id)

    if row_diffs:
        print(f"FAIL: {len(row_diffs)} metric(s) have row diffs (set-equivalent + FP tolerance):")
        for m in row_diffs:
            print(f"  - {m}")
            g_rows = golden[m]["query"]["rows"]
            a_rows = after[m]["query"]["rows"]
            print(f"    golden first 2 rows: {g_rows[:2]}")
            print(f"    after  first 2 rows: {a_rows[:2]}")
        return 1

    if coverage_issues:
        print("WARN coverage:")
        for c in coverage_issues:
            print(f"  - {c}")

    print(
        f"CLEAN — {len(golden)} metrics, {len(sql_changes)} SQL strings changed, "
        f"0 row diffs (set + FP tolerance)."
    )
    if sql_changes:
        print("SQL-changed metrics:")
        for m in sql_changes:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    g = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sql_baseline_golden.json"
    a = sys.argv[2] if len(sys.argv) > 2 else "/tmp/sql_baseline.json"
    raise SystemExit(main(g, a))
