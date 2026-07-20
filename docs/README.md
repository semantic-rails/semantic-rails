# Semantic Rails Docs

Start with these canonical docs:

- [Agent API path](AGENT_API_PATH.md) for the recommended `/api/v1/*` route sequence.
- [Agent quickstart](AGENT_QUICKSTART.md) for local MCP, local HTTP, and
  supported-vs-experimental guidance for agent clients.
- [Deployment](DEPLOYMENT.md) for Docker, ASGI, health/readiness, request context, and API-key shim setup.
- [Package authoring](PACKAGE_AUTHORING.md) for the schema.
- [Architect MCP](ARCHITECT_MCP.md) for revision-safe concurrent package mutations.
- [Query API](QUERY_API.md) for agent discovery, planning, validation, compile-only SQL, and execution surfaces.
- [MCP interface](MCP_INTERFACE.md) for self-hosted Streamable HTTP, the in-process adapter, and local compatibility transports.
- [Benchmark evidence](BENCHMARK_EVIDENCE.md) for measured plan/agent scorecard gates.
- [Capabilities](CAPABILITIES.md) for the supported semantic modeling feature set.
- [Architecture spec](ARCHITECTURE.md) for the runtime architecture and compiler-stage contract.
- [Public contracts](CONTRACTS.md) for version ownership, semantic validation export, compatibility rules, and cross-repository releases.
- [Embedding the engine](EMBEDDING.md) for the supported generic hosting facade.
- [Adding a warehouse dialect](ADDING_A_DIALECT.md) for the dialect/adapter/registry contract and the cross-warehouse conformance suite.
- [Snowflake showcase runbook](SNOWFLAKE_SHOWCASE_RUNBOOK.md) for the live Snowflake sample package.

For first install, first query, and contributor entrypoints see the top-level [README.md](../README.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).

## Active Runtime Surface

- Runtime package: `semantic_rails/`
- Active fixture package: `configs/semantic_rails/jaffle_shop`
- Canonical API contract: [QUERY_API.md](QUERY_API.md)
- Supported behavior and limits: [CAPABILITIES.md](CAPABILITIES.md)

## Package Verification

Use the parser and validator while authoring a package:

```bash
uv run semantic-rails parse-config --package jaffle_shop
uv run semantic-rails validate-config --package jaffle_shop
```

Use the one-command package gate before treating a config repo as deployable:

```bash
uv run semantic-rails check --package jaffle_shop --artifact dist/jaffle_shop.semantic-rails.tar.gz
```

The command runs parse, validation probes, package-local examples, and package-local tests, then writes a manifest-backed artifact when `--artifact` is provided.

Use the distribution verifier before publishing a Python package:

```bash
uv build --out-dir dist
uv run python scripts/verify_package_distribution.py --dist-dir dist --no-build
```

It verifies the exact wheel and sdist already built for release, installs the wheel into an isolated
virtual environment, and confirms the installed CLI can list packages, load the bundled
`jaffle_shop` catalog, and run a local query. Running the verifier without `--no-build` remains a
convenient standalone local smoke.

Check generated HTTP/MCP artifacts and compatibility against the previous
released bundle:

```bash
uv run python scripts/generate_contract_artifacts.py --check
uv run python scripts/check_contract_compatibility.py \
  --baseline path/to/previous-release/contracts
```

## Ergonomics V2

The active interface direction is:

- Humans author durable primitives in the `schema_version: 1` ergonomic YAML shape; the loader normalizes them into the internal `PackageConfig`.
- Agents use catalog/inspect/plan/validate/compile/query. `plan` returns a validated best Query IR draft, and `compile` returns SQL without execution. `plan` status and detail semantics live in [AGENT_API_PATH.md](AGENT_API_PATH.md) and [QUERY_API.md](QUERY_API.md#post-apiv1plan).
- SQL is rendered through per-warehouse dialect capabilities. DuckDB is the zero-setup local default; Snowflake (via `snowflake_cli` or `snowflake_native`), Postgres, BigQuery, Databricks, Athena, ClickHouse, MotherDuck, and DuckLake connect through `package.connection` blocks (see [ADDING_A_DIALECT.md](ADDING_A_DIALECT.md)). Response metadata includes `output_columns`, `sql_profile`, `warehouse`, `dialect`, physical plan, and performance risk.
- Runtime deployment is self-hostable: Docker/ASGI, health/readiness, request
  context, audit logs, and an optional API-key shim are shipped. Service
  control-plane concerns stay outside the engine repository.
- Relation-pipeline wording is evidence-gated. The shipped surface is package-authored relations,
  joins, metrics, segments, SQL AST/explain output, and package checks; arbitrary chained SQL
  pipelines and warehouse materialization orchestration are planned or out of scope unless a runnable
  package example proves otherwise.
