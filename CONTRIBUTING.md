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

A change to what a query returns cites or adds a canonical case in
`tests/integration/correctness/shop/tests/answers.yml` and runs `make answers`.
Each frozen answer cites its declaration and is checked against independent
reference SQL; questions without a declared standard expect a clarification.

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
uv run pytest -q tests/semantic_rails -n auto
uv run ruff check .
uv run ruff format --check .
uv run mypy semantic_rails
uv run python scripts/generate_contract_artifacts.py --check
uv run python scripts/verify_release_readiness.py
uv run python scripts/changelog_fragments.py check
```

If `test_embedding_consumer_contract.py` fails, the change breaks a known embedder's use of
`semantic_rails.embedding`: follow "Changing the facade" in [docs/EMBEDDING.md](docs/EMBEDDING.md).

### ADBC tests

The Python 3.12 backend CI shards installs the `snowflake-adbc` and `postgres`
extras and requires both ADBC unit test modules to run without skips. These
tests use local Arrow batches and stub connections; warehouse credentials are
not needed. To run them locally with the same extras:

```bash
uv sync --group dev --extra snowflake-adbc --extra postgres --locked
uv run --no-sync pytest -q tests/semantic_rails/test_adbc_adapter.py tests/semantic_rails/test_adbc_snowflake.py -n 4
```

### Intermittent tests

Test subprocess helpers preserve the inherited `PYTHONPATH` so the DuckDB startup hook also applies to CLI children. Test DuckDB connections keep spill files under pytest's basetemp; `SR_TEST_DUCKDB_MAX_TEMP` (default `4GB`) caps spill per database instance and `SR_TEST_DUCKDB_MEMORY` (default `2GB`) caps memory.
Tests read one read-only seed per worker; tests that write request `copy_package_config(..., writable=True)`, and passing tests' temporary directories are deleted.
A DuckDB connection opened in a test body and still open after that test's teardown is closed there, so garbage collection never finalizes it during a later test; the run summary reports "DuckDB connections left open by N tests" with the worst node IDs. Connections opened while a fixture sets up, even one a test body requests, are left to that fixture; a connection shared across tests belongs in a fixture.

Each test has a five-minute timeout using `pytest-timeout`'s thread method,
which dumps all thread stacks before terminating the process. Under xdist,
the controller reports the crashed worker and test node ID; that stack dump
is not relayed, so `faulthandler_timeout` (240 s) first writes every thread's
stack to the worker's stderr, which reaches the CI log. Tests that legitimately
need longer must declare an explicit `@pytest.mark.timeout(...)` override. The
backend CI job's 30-minute timeout remains the backstop.

Tests bound every wait: subprocess calls, `urlopen`, `communicate`, and
`join`/`wait` take a timeout; `tests/semantic_rails/test_bounded_waits.py`
enforces it.

Backend CI partitions whole test files into four deterministic shards. Pull requests
run Python 3.12 only; merge groups and manual runs cover Python 3.11, 3.12, 3.13
and 3.14. The merge queue is the complete compatibility gate: a failure on another
Python version ejects the change before merging. Files are assigned longest first
to the lightest shard, with paths and shard indices breaking ties.
`tests/shard_durations.json` records summed
per-test seconds for each file; unknown files use the median cost. Ownership uses
the full on-disk test-file inventory so targeted runs and the flake guard retain
the full suite's assignment. Set `SR_SHARD_COUNT`
and zero-based `SR_SHARD_INDEX` to reproduce a shard locally; unset both to run
without partitioning. Quarantine IDs are validated against the full collection
before partitioning. Every Python/version shard uploads its JUnit report, and the
required "All checks pass" gate waits for the event's entire backend matrix. Backend
full-suite jobs skip pushes to main/master: pull requests and merge groups test
the suite before merging, while pushes retain lint, security, documentation and
Postgres checks. Direct pushes therefore rely on branch protection to require
pull requests and the merge queue.

Refresh the table when a backend shard exceeds about 15 minutes or test costs
change substantially. Download the `backend-results-py*` artifacts from a complete
CI run with `gh run download <run-id> --pattern 'backend-results-py*' --dir /tmp/backend-reports`,
then run `uv run python scripts/update_shard_durations.py /tmp/backend-reports/*/backend-results.xml`.
Supply reports from all Python versions (or multiple representative runs): the
script retains the slowest observed total for each file and prints shard estimates
at four workers, targeting about 12 minutes. These estimates omit startup, fixture
contention and auxiliary checks; use actual CI job times to confirm the count.
If one file alone exceeds the target, the script warns to split that file by hand.
Commit the refreshed table and adjust the workflow shard count if needed.

Merge-group CI on each Python 3.12 shard repeats affected unit test files three times with
random test ordering and an automatically chosen worker count.
`scripts/flake_guard.py` selects changed tests from the default test roots and tests
that directly import changed `semantic_rails`
modules, prioritizing changed tests and capping the set at 20 files. Seeds and any
omitted file count are logged. The guard uses the main run's `backend-results.xml`
test durations divided by its worker count to trim the lowest-priority files until
the first repetition fits its five-minute budget with 20% headroom; dropped files
produce a notice. Each repeated test runs under a 60-second limit, unless it declares
a longer `@pytest.mark.timeout`, and the first failure or test over its limit ends the
repetition. Any test failure, including a test over its limit,
fails the job with `intermittent: investigate`, without retrying the failure away,
even when slower tests are still running as the budget ends.
A repetition that cannot fit the remaining budget is skipped; one with no failure
still running when the budget ends is stopped with a notice naming its files. Both
are inconclusive and pass, since the main step already ran every test once. Warehouse
integration tests keep their separate CI.

`tests/quarantine.toml` starts empty. To temporarily quarantine a known failure,
replace `tests = []` with `[[tests]]` entries containing the exact pytest node `id`
(including parameter IDs), a nonempty `reason`, an HTTPS `upstream` issue link,
and a `review_by` date no more
than 30 days ahead. The collection hook applies `xfail(strict=False)` so unexpected
passes are visible. Expired entries fail collection; full-suite CI also uses
`--validate-quarantine` to fail on nonexistent IDs. Use
`uv run pytest --collect-only -q --validate-quarantine` to check entries locally;
targeted runs omit that flag because they collect only part of the suite. Remove
entries when fixed, or review the linked issue before extending the date.

## Dependency advisories

Run `uv run --no-sync python scripts/audit_dependencies.py` after syncing the dev
group. CI's Security audit checks the locked base install as a hard gate, then
audits every extra declared in `pyproject.toml` separately, except the aggregate
`all` extra. Only `snowflake`, `postgres`, `bigquery`, `databricks`, `athena`, and
`clickhouse` can receive exceptions; `server`, `repl`, and core-reachable packages
can never receive an exception.

When an upstream connector caps a dependency below its patched version, add one
`[[exceptions]]` entry per advisory to `security/audit-exceptions.toml`, with `id`,
`package`, the affected connector `extras`, `blocked_by` (`capping-package: specifier`),
`fixed_in`, an HTTPS tracking `issue`, `review_by` (a TOML date no more
than 30 days away), and `reason`. Every finding needs an entry for its own extra.
The checker rejects expired or unused entries and core-reachable packages.
The normalized `blocked_by` package must appear in the extra's audit report and
be named by an active direct requirement in `project.dependencies` or that extra's
`optional-dependencies`. A blocker reached only transitively cannot authorize an
exception. Its latest release must declare an active dependency requirement in
PyPI's JSON `requires_dist` metadata with an upper bound admitting no version at
or above the lowest patched version reported by the advisory, including backports.
`<V` qualifies when `V` is at most that patched version; `<=V`, `==V` and `===V`
qualify only when `V` is lower. For `~=V` and `==V.*`, the implied exclusive upper
bound must be at most the patched version. Lower bounds (`>=`, `>`) and exclusions
(`!=`) never establish a cap; unknown operators or unparseable versions fail the
check. `fixed_in` must appear among the reported patched versions. Markers are
evaluated for the current interpreter and platform; only active direct edges
select the blocker's requested extras.
The combined active requirements must also exclude every reported patched version
under packaging specifier rules, including versions with local labels.
A lifted cap fails with "cap lifted: upgrade now"; missing requirements, metadata
fetch or parse failures, and audit errors fail the check. Resolver explanations
cannot authorize an exception. Once the cap lifts,
upgrade the lockfile and remove the exception; if the advisory disappears, remove
the unused entry. Review the tracking issue before renewing a blocked exception.
The `all` extra must equal the union of the other extras, including `server` and
`repl`, with normalized package names and specifiers. It inherits their findings
and cannot have a separate exception.

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
