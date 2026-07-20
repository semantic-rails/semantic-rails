# Semantic Rails

[![PyPI](https://img.shields.io/pypi/v/semantic-rails.svg?label=PyPI)](https://pypi.org/project/semantic-rails/) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/semantic-rails/) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) [![Docs](https://img.shields.io/badge/docs-semantic--rails.com-0f766e.svg)](https://semantic-rails.com/docs/)

> Agents need more than a SQL string. They need a way to see what exists,
> understand the allowed next move, validate the draft, and only then run it.
> Semantic Rails packages that loop for local projects, HTTP clients, and MCP
> hosts.

Semantic Rails is an Apache-2.0 open-source, agent-first semantic layer. It ships a Python
package, a CLI, an MCP server, and an HTTP API. DuckDB works out of the box for
local development; Snowflake, Postgres, BigQuery, Databricks, Athena,
ClickHouse, MotherDuck, and DuckLake are available through optional connectors.

Project documentation is available in [`docs/`](docs/) and at
[semantic-rails.com/docs](https://semantic-rails.com/docs/).

## Quickstart

Start from any empty project folder. This path uses the published PyPI package;
no repo checkout is required.

```bash
mkdir semantic-rails-demo
cd semantic-rails-demo
uv venv .venv
source .venv/bin/activate
uv pip install 'semantic-rails>=0.2.0'

semantic-rails setup --interactive
semantic-rails repl --path ./my_package
```

If you use another Python environment manager, install Semantic Rails into that
environment and then run the same setup command:

```bash
mkdir semantic-rails-demo
cd semantic-rails-demo
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'semantic-rails>=0.2.0'
semantic-rails setup --interactive
semantic-rails repl --path ./my_package
```

These onboarding commands require Semantic Rails 0.2.0 or newer. The setup
wizard can create a starter package, set a local profile, validate the
package, and add Query MCP or Architect MCP to Claude Desktop or Codex. On POSIX
systems it can also start a managed local MCP server; on Windows it skips that
offer and uses client-launched stdio or a foreground HTTP terminal instead.
Inside the REPL, type `author` to create or update models, dimensions, times,
measures, metrics, and segments with guided choices, pre-write previews,
similar-definition warnings, parse validation, and session-local `undo`.
After the wizard sets a local profile, most commands can omit `--path`:

```bash
semantic-rails ask "total amount by event type" --run
semantic-rails mcp setup
```

For a non-interactive setup path:

```bash
semantic-rails init my_package --yes
semantic-rails project validate --path ./my_package
semantic-rails ask --path ./my_package "total amount by event type" --run
semantic-rails mcp setup --path ./my_package
```

`init` and the wizard both create a runnable package with `package.yml`,
`graph.yml`, `models/`, `metrics/`, `examples/`, `tests/`, and starter CSV data.
Commands can pass `--path ./my_package`, run from inside the package directory,
or use the local profile created by the wizard.

To turn the starter into your project, edit the package files in place:

- `package.yml`: package id, namespace, warehouse, and connection settings.
- `graph.yml`: entities and relationships between modeled tables.
- `models/`: source relations, dimensions, time roles, and measures.
- `metrics/`: governed metrics built from those measures.
- `examples/` and `tests/`: known-good queries for validation and CI.

Then rerun:

```bash
semantic-rails project validate --path ./my_package
semantic-rails ask --path ./my_package "your business question" --run
```

For a guided authoring walkthrough, see
[docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md). For MCP-assisted
authoring, see [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).

## Local MCP

For your own package, prefer an absolute path because MCP clients may start from
a different working directory than your shell.

```bash
PACKAGE_PATH="$(pwd)/my_package"
semantic-rails mcp setup --path "$PACKAGE_PATH"
```

`mcp setup` checks the package, confirms the MCP tools load, and previews the
Claude/Codex config it would write. From inside the package directory, or after
`semantic-rails profile init --package-path ./my_package`, you can omit
`--path`.

To install local MCP config:

```bash
semantic-rails mcp setup --path "$PACKAGE_PATH" --client both --mcp both --install --yes
```

On POSIX systems, use the managed background lifecycle for an HTTP smoke:

```bash
semantic-rails mcp status --path "$PACKAGE_PATH"
semantic-rails mcp start --path "$PACKAGE_PATH" --port 8091
curl -s http://127.0.0.1:8091/health
semantic-rails mcp stop --path "$PACKAGE_PATH"
```

The wizard uses the default managed-server name. If it already started the
server, `mcp status` will show it and `mcp start` returns `already_running`.
For a stale registration, run `semantic-rails mcp stop --name default`; the
interactive wizard also offers to remove a dead registration and retry.

Managed `mcp start/status/stop` requires POSIX process identity. On Windows,
install the generated client config so Claude/Codex launches stdio, or keep the
foreground HTTP process open in its own terminal:

```powershell
$PACKAGE_PATH = (Resolve-Path .\my_package).Path
semantic-rails mcp setup --path "$PACKAGE_PATH" --install --yes
semantic-rails mcp http --path "$PACKAGE_PATH" --host 127.0.0.1 --port 8091
```

`--mcp query` installs the runtime/query MCP. `--mcp architect` installs the
package-authoring MCP. `--mcp both` installs both. Raw `mcp stdio` and
`mcp http` commands are foreground server processes; most users should let their
MCP host launch stdio from generated config. `mcp client-config` remains
available when you want to preview or write client config directly.

See [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md) and
[docs/AGENT_QUICKSTART.md](docs/AGENT_QUICKSTART.md) for the full MCP contract.

## Source Checkout

Use the GitHub source path when you are contributing to this repository or want
the checked-in examples under `examples/`.

```bash
git clone https://github.com/semantic-rails/semantic-rails.git
cd semantic-rails
uv sync --group dev
uv run semantic-rails packages
uv run semantic-rails query --package jaffle_shop --query-json '@examples/jaffle_shop_revenue_by_store.json' --verbosity minimal --sql-profile off
```

Expected package output includes the bundled synthetic fixture:

```text
jaffle_shop
```

Contributor release smoke for the bundled package:

```bash
uv run semantic-rails parse-config --path configs/semantic_rails/jaffle_shop
uv run semantic-rails validate-config --path configs/semantic_rails/jaffle_shop --quiet
```

Build and verify the exact wheel and sdist before publishing:

```bash
uv build --out-dir dist
uv pip install --python .venv/bin/python --reinstall dist/semantic_rails-0.2.0-py3-none-any.whl
uv run python scripts/verify_package_distribution.py --dist-dir dist --no-build
```

## Package Naming

Semantic Rails is the public product name. The PyPI distribution is
`semantic-rails`, the Python import package is `semantic_rails`, and the CLI is
`semantic-rails`. The published distribution also includes `mf2sr`, the
MetricFlow translator behind `semantic-rails import`.

## What It Provides

- A governed Query IR that can be discovered, planned, validated, compiled, and
  executed as separate steps.
- MCP tools for the agent loop: `capabilities`, `catalog`, `discover`,
  `inspect`, `build-options`, `valid-values`, `plan`, `validate`, `compile`,
  `execute`, and segment helpers.
- Structured errors for invalid metrics, dimension mismatches, bad filters, and
  policy failures, with recovery hints where possible.
- Local DuckDB by default, with optional warehouse connectors for production
  runtimes.

The bundled `jaffle_shop` package is useful for smoke tests and examples. It is
not the onboarding path for modeling your own data; use `semantic-rails init`
for that.

## Warehouses

DuckDB is included. Install optional connectors only when you need them:

```bash
pip install 'semantic-rails[postgres]'    # also: snowflake, bigquery, databricks, athena, clickhouse
pip install 'semantic-rails[all]'         # every connector
```

MotherDuck and DuckLake use the core `duckdb` dependency. Package connection
settings should keep secrets in environment variables or files, not YAML
literals. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/ADDING_A_DIALECT.md](docs/ADDING_A_DIALECT.md).

## Docs

- [Getting started](https://semantic-rails.com/docs/getting-started)
- [Package authoring](docs/PACKAGE_AUTHORING.md)
- [MCP interface](docs/MCP_INTERFACE.md)
- [Agent quickstart](docs/AGENT_QUICKSTART.md)
- [Architect MCP](docs/ARCHITECT_MCP.md)
- [Query API](docs/QUERY_API.md)
- [Capabilities](docs/CAPABILITIES.md)
- [Benchmark evidence](docs/BENCHMARK_EVIDENCE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [OSS release checklist](docs/RELEASE_CHECKLIST.md)
- [Contributing](CONTRIBUTING.md)
- [Comparative capability evidence](comparisons/semantic_layers/)

## Project Status

Semantic Rails is beta software. The open-source runtime, CLI, MCP stdio server,
HTTP API, and DuckDB path are the supported core.

Support, issue reporting, conduct, and security reporting are documented in
[SUPPORT.md](SUPPORT.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md).

## License

Apache 2.0; see [LICENSE](LICENSE).
