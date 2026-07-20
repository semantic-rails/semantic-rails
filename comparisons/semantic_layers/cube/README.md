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

Notes:

- The original capture used the local Node/Cube Core path rather than Docker.
- `customer_history` and `storefront_sessions` are modeled with explicit SQL joins / calculated measures, which is intentional evidence for the “workaround-heavy but possible” side of the comparison.
