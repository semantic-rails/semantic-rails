# Contributing

## Scope

This repo accepts changes only against the public `semantic_rails` runtime and its supported docs/package surface:

- `semantic_rails/`
- `configs/semantic_rails/`
- `tests/semantic_rails/`
- `docs/`

## Active Source Of Truth

This repo is intentionally centered on one active runtime and one active authored package.

- Runtime code: `semantic_rails/`
- Active package: `configs/semantic_rails/jaffle_shop/`
- Active package examples: `configs/semantic_rails/jaffle_shop/examples/`
- Active package tests: `configs/semantic_rails/jaffle_shop/tests/`
- Runtime tests: `tests/semantic_rails/`
- Seed fixtures for the active package: `data/jaffle_csv/` and `data/seed_jaffle.sql`

Everything else is supporting material, generated output, or archival history unless the docs explicitly say otherwise.

## Contributor Paths

### Package authoring

Start here when changing entities, dimensions, measures, metrics, or graph semantics.

- `configs/semantic_rails/jaffle_shop/package.yml`
- `configs/semantic_rails/jaffle_shop/graph.yml`
- `configs/semantic_rails/jaffle_shop/models/**`
- `configs/semantic_rails/jaffle_shop/metrics/**`

The package compiles into the runtime's `PackageConfig` via `semantic_rails/config.py`. For package PRs, run `uv run semantic-rails check --package jaffle_shop --artifact dist/jaffle_shop.semantic-rails.tar.gz`.

### Metadata and guided builder

Start here when changing catalog, discovery, inspect cards, build-options, valid-values, or plan behavior. HTTP and CLI surfaces call into `semantic_rails/runtime.py`; metadata payload builders live under `semantic_rails/metadata.py` and `semantic_rails/metadata_parts/`. The preferred user flow is `discover -> inspect -> plan/build-options -> valid-values -> validate -> compile -> execute` (relevance/scope screening runs inline on `discover` and `plan`; `compile`'s response includes the `explain` payload).

### Query compilation and execution

Start here when changing query planning, path selection, fanout rules, rewrites, SQL lowering, or explain output.

- Query normalization: `semantic_rails/ast.py`
- Planning and compile orchestration: `semantic_rails/compiler.py`
- Compiler subsystems: `semantic_rails/compiler_parts/`
- Fanout and path analysis: `semantic_rails/fanout.py`
- SQL rendering: `semantic_rails/renderer.py`
- Runtime execution and warehouse adapters: `semantic_rails/runtime.py` and `semantic_rails/db.py`

### Fixture and seed data

Start here when changing real runnable demo data.

- CSV fixtures: `data/jaffle_csv/*.csv`
- Post-load shaping SQL: `data/seed_jaffle.sql`
- Package seed configuration: `configs/semantic_rails/jaffle_shop/package.yml`

The active runtime reseeds the DuckDB file when the expected tables are missing.

## Expectations

1. Keep compiler boundaries explicit. Request payloads should normalize to AST, planning should emit semantic IR, lowering should emit SQL AST, and rendering should emit SQL text.
2. Do not reintroduce action-envelope or ontology-cockpit behavior into the public runtime.
3. Keep examples and docs aligned with the active runtime, not migration-era code.
4. Add or update focused tests for runtime, planner, or metadata behavior when changing those areas.
5. Treat generated distribution artifacts, cache directories, and local virtual environments as non-source material unless a task explicitly targets them.

## Validation

Run these before opening a PR:

```bash
uv run pytest -q tests/semantic_rails -n auto
uv run python scripts/verify_release_readiness.py
```

## Full Verification Matrix

For release-surface work or anything that touches the runtime, packages, or
distribution, run the extended matrix. Each command is independent — run the ones
that match the surface you changed.

```bash
# Package-quality gate (parse, validate, examples, tests, manifest):
uv run semantic-rails check --package jaffle_shop --artifact dist/jaffle_shop.semantic-rails.tar.gz

# Wheel smoke test (verifies the installable distribution from an isolated venv):
uv build --wheel
uv run python scripts/verify_package_distribution.py
```

For the Snowflake showcase package, see
[docs/SNOWFLAKE_SHOWCASE_RUNBOOK.md](docs/SNOWFLAKE_SHOWCASE_RUNBOOK.md).
