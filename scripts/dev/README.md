# Developer-only Scripts

Utility scripts used during refactors. Not part of the user-facing CLI.

| Script | Purpose |
|---|---|
| `capture_sql_baseline.py` | Compile + execute every governed jaffle metric, write rendered SQL + queried rows to a JSON file. Use before refactoring SQL lowering or rendering. |
| `diff_sql_baseline.py` | Diff a fresh baseline against a golden snapshot. Set-equivalent with FP tolerance (DuckDB row order is non-deterministic; ULP-level FP results vary). Fails on row diffs and on metrics that no longer compile or run; lists metrics whose SQL changed: a SQL-quality refactor changes SQL, a pure code move must not. |
| `capture_dialect_sql.py` | Compile (without running) the same queries once per supported dialect and write the SQL to a JSON file; `--compare GOLDEN AFTER` lists every statement that changed. A pure code move must leave all of them byte-identical. |
| `function_lengths.py` | List functions over `--min` lines (default 150), longest first, with totals over 150 and 300 lines. Report-only; `make complexity` runs it with ruff's complexity rules. To check that no function a change touches got longer, diff `--by-name --min 0` output from before and after. For per-function complexity scores, run `uv run ruff check semantic_rails mf2sr --select C901,PLR0912,PLR0915 --exit-zero --output-format concise`. |
| `regen_snapshot_cases.py` | Regenerate `SNAPSHOT_CASES` inside `tests/semantic_rails/test_rendered_sql_snapshots.py` after a deliberate renderer/compiler change. |

## Typical refactor workflow

```bash
# Before changing anything
uv run python scripts/dev/capture_sql_baseline.py /tmp/sql_baseline_golden.json
uv run python scripts/dev/capture_dialect_sql.py /tmp/dialect_sql_golden.json
uv run python scripts/dev/function_lengths.py --by-name --min 0 > /tmp/functions_before.txt

# Make refactor commits

# After
uv run python scripts/dev/capture_sql_baseline.py /tmp/sql_baseline_after.json
uv run python scripts/dev/diff_sql_baseline.py /tmp/sql_baseline_golden.json /tmp/sql_baseline_after.json
uv run python scripts/dev/capture_dialect_sql.py /tmp/dialect_sql_after.json
uv run python scripts/dev/capture_dialect_sql.py --compare /tmp/dialect_sql_golden.json /tmp/dialect_sql_after.json
uv run python scripts/dev/function_lengths.py --by-name --min 0 > /tmp/functions_after.txt
diff /tmp/functions_before.txt /tmp/functions_after.txt

# If snapshot tests need regenerating after a deliberate renderer change
uv run python scripts/dev/regen_snapshot_cases.py
```

`.tmp-baseline/` and `.tmp-snapshot/` are gitignored scratch dirs the scripts use.
