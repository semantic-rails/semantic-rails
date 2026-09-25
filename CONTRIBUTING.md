# Contributing

## Scope

This repo accepts changes only against the public `semantic_rails` runtime and its supported docs/package surface:

- `semantic_rails/`
- `mf2sr/` (MetricFlow import)
- `configs/semantic_rails/`
- `tests/semantic_rails/`
- `tests/mf2sr/`
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

This file is the sole contributor and agent guide. [docs/README.md](docs/README.md)
indexes the maintained product documentation; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
owns the engine design, and [docs/CONTRACTS.md](docs/CONTRACTS.md) owns public contract
versioning. The generated contract artifacts must agree with the engine's producers.
Keep Cloud identity, credential storage, billing, and tenant orchestration outside
this public repository; transports share request shaping and wrap the same runtime.

Update the owning document in the same PR as a behavior change. Put temporary audit
findings, run receipts, and implementation plans in issues or PRs rather than new
dated guidance files. Historical benchmark evidence records its tested version and
does not override current code or maintained docs. Keep `AGENTS.md` as a pointer
here instead of creating another set of contributor rules.

## Contributor Paths

### Package authoring

Start here when changing entities, dimensions, measures, metrics, or graph semantics.

- `configs/semantic_rails/jaffle_shop/package.yml`
- `configs/semantic_rails/jaffle_shop/graph.yml`
- `configs/semantic_rails/jaffle_shop/models/**`
- `configs/semantic_rails/jaffle_shop/metrics/**`

The package compiles into the runtime's `PackageConfig` via `semantic_rails/config.py`. For package PRs, run `uv run semantic-rails check --package jaffle_shop --artifact dist/jaffle_shop.semantic-rails.tar.gz`.

### Metadata and guided builder

Start here when changing catalog, discovery, inspect cards, build-options, valid-values, or plan behavior. HTTP and CLI surfaces call into `semantic_rails/runtime.py`; metadata payload builders live under `semantic_rails/metadata.py` and `semantic_rails/metadata_parts/`. The preferred user flow is `discover -> plan -> execute`; `inspect`, `build-options` and `valid-values` are optional helpers, and `validate` and `compile` are optional dry runs, because `execute` runs the same bind, policy and compile steps (relevance/scope screening runs inline on `discover` and `plan`; `compile`'s response includes the `explain` payload).

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

The runtime creates a missing DuckDB file from its seed, but never automatically replaces an existing file. If an existing file lacks configured relations, build them with its owner (for example dbt), or back up and explicitly remove a disposable seed database before restarting.

## Expectations

1. Keep compiler boundaries explicit. Request payloads should normalize to AST, planning should emit semantic IR, lowering should emit SQL AST, and rendering should emit SQL text.
2. Do not reintroduce action-envelope or ontology-cockpit behavior into the public runtime.
3. Keep examples and docs aligned with the active runtime, not migration-era code.
4. Add or update focused tests for runtime, planner, or metadata behavior when changing those areas.
5. Treat generated distribution artifacts, cache directories, and local virtual environments as non-source material unless a task explicitly targets them.
6. Record user-facing changes as a `changelog.d/` fragment (see [changelog.d/README.md](changelog.d/README.md)) instead of editing `CHANGELOG.md`.

## Validation

Run these before opening a PR (`make install lint typecheck test-backend contracts-check release-check changelog-check` runs the same commands):

```bash
uv sync --group dev --locked
uv run pytest -q tests/semantic_rails tests/mf2sr -n auto
uv run ruff check .
uv run ruff format --check .
uv run mypy semantic_rails
uv run python scripts/generate_contract_artifacts.py --check
uv run python scripts/verify_release_readiness.py
uv run python scripts/changelog_fragments.py check
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

# Refactors: complexity report (never fails), then golden SQL before and after
# (see scripts/dev/README.md):
make complexity
uv run python scripts/dev/capture_sql_baseline.py /tmp/sql_baseline_golden.json
uv run python scripts/dev/capture_dialect_sql.py /tmp/dialect_sql_golden.json
```

For the Snowflake showcase package, see
[docs/SNOWFLAKE_SHOWCASE_RUNBOOK.md](docs/SNOWFLAKE_SHOWCASE_RUNBOOK.md).
