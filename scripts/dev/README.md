# Developer-only Scripts

Utility scripts used during refactors. Not part of the user-facing CLI.

| Script | Purpose |
|---|---|
| `capture_sql_baseline.py` | Compile + execute every governed jaffle metric, write rendered SQL + queried rows to a JSON file. Use before refactoring SQL lowering or rendering. |
| `diff_sql_baseline.py` | Diff a fresh baseline against a golden snapshot. Set-equivalent with FP tolerance (DuckDB row order is non-deterministic; ULP-level FP results vary). Reports row diffs and lists metrics whose SQL changed: a SQL-quality refactor changes SQL, a pure code move must not. |
| `function_lengths.py` | List functions over `--min` lines (default 150), longest first, with totals over 150 and 300 lines. Report-only; `make complexity` runs it with ruff's complexity rules. Compare before and after a split: no function a change touches should get longer. |
| `regen_snapshot_cases.py` | Regenerate `SNAPSHOT_CASES` inside `tests/semantic_rails/test_rendered_sql_snapshots.py` after a deliberate renderer/compiler change. |

## Typical refactor workflow

```bash
# Before changing anything
uv run python scripts/dev/capture_sql_baseline.py /tmp/sql_baseline_golden.json

# Make refactor commits

# After
uv run python scripts/dev/capture_sql_baseline.py /tmp/sql_baseline_after.json
python scripts/dev/diff_sql_baseline.py /tmp/sql_baseline_golden.json /tmp/sql_baseline_after.json

# If snapshot tests need regenerating after a deliberate renderer change
uv run python scripts/dev/regen_snapshot_cases.py
```

`.tmp-baseline/` and `.tmp-snapshot/` are gitignored scratch dirs the scripts use.
