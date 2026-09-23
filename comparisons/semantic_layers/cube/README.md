# Cube Comparison Evidence

The captured run used:

- `@cubejs-backend/server@1.6.32`
- `@cubejs-backend/duckdb-driver@1.6.32`
- `cubejs-cli@1.6.32`

This project uses the shared `comparison_*` views inside `comparisons/semantic_layers/shared/data/jaffle_comparison.duckdb`.

The npm manifest and lockfile are intentionally not shipped under the
installable `package.json` / `package-lock.json` names. The captured Cube
dependency graph currently carries unresolved high and critical advisories in
upstream runtime packages, so the originals are preserved as
`original-package*.json.evidence` alongside the raw npm audit, normalized
CycloneDX SBOM, authored models, queries, runner source, and result hashes.
This keeps the capture independently reviewable without inviting a default
install of the vulnerable graph. See `runtime-snapshot.json`.

Verify the complete capture offline with Python's standard library:

```bash
python3 comparisons/semantic_layers/cube/scripts/verify_evidence.py
```

The evidence was produced with:

```bash
uv run python comparisons/semantic_layers/cube/scripts/run_questions.py
```

That command now stops with an explicit captured-evidence message unless a
reviewer intentionally supplies a separately audited installable Cube runtime
manifest. The offline verifier above never renames or installs the preserved
dependency graph.

Artifacts are written under `comparisons/semantic_layers/shared/results/cube/`.

## Re-executing the captured SQL

The SQL that Cube generated for each question is captured in
`shared/results/cube/q*/sql.json` and doesn't depend on the data. So that Cube answers on
the same dataset as every other layer, the output check uses a re-execution of that SQL:

```bash
uv run python comparisons/semantic_layers/cube/scripts/replay_sql.py
```

It runs each captured statement with its captured parameters against the shared DuckDB,
with the session time zone pinned to UTC (Cube's SQL casts through `timestamptz`), and
writes `shared/results/cube_sql_replay/`. On every question whose data didn't change, the
replay returns the rows Cube itself returned, with numbers equal to within 1e-9 (the largest
difference is 7.7e-10, on q08). Cube's own response also echoes each time dimension without its
granularity. The capture above stays pinned by the offline verifier.

Notes:

- The original capture used the local Node/Cube Core path rather than Docker.
- q01-q09 each also hold an `error.txt` from an earlier attempt in the capture session: q01-q03
  and q06-q09 ran before the `comparison_*` views existed, and q04 and q05 before their measures
  were fixed.
  The runner never deletes an earlier attempt's error. The capture's `summary.json` records
  every question as answered, and each has its `load.json` and `sql.json`. The files stay
  because the evidence manifest pins every captured file.
- `customer_history` and `storefront_sessions` are modeled with explicit SQL joins and calculated measures.
