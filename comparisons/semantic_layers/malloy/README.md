# Malloy Comparison Pack

This project pins `@malloydata/cli` and runs the shared Jaffle comparison dataset through a single Malloy model with named queries.

## Setup

1. Install dependencies:

   ```bash
   npm install
   ```

2. Validate the model:

   ```bash
   npm run validate
   ```

3. Execute the comparison suite:

   ```bash
   uv run python scripts/run_questions.py
   ```

If `node_modules` is absent, the runner will install the pinned CLI automatically before compiling queries.

## Comparison Notes

- Baseline and portable questions are modeled directly in Malloy using sources, joins, dimensions, and measures.
- Historical-segment, session-conversion, and aggregate-on-aggregate questions use explicit DuckDB SQL sources and are therefore treated as `workaround` coverage in this pack.
- Result snapshots are produced by compiling each named Malloy query to SQL and executing that SQL against the shared DuckDB database, because the CLI's direct `run` preview truncates output rows.
- Generated artifacts are written to `../shared/results/malloy/`.
