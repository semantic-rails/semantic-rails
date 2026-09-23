# Semantic Rails

[![CI](https://github.com/semantic-rails/semantic-rails/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/semantic-rails/semantic-rails/actions/workflows/ci.yml?query=branch%3Amain) [![PyPI](https://img.shields.io/pypi/v/semantic-rails.svg?label=PyPI)](https://pypi.org/project/semantic-rails/) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/semantic-rails/) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) [![Docs](https://img.shields.io/badge/docs-semantic--rails.com-0f766e.svg)](https://semantic-rails.com/docs/)

Semantic Rails is an open-source, agent-first semantic layer, licensed Apache-2.0.
You define metrics, dimensions and join paths once, in YAML. Agents then discover
those definitions, validate a query and compile governed SQL through an MCP server,
a CLI or an HTTP API, instead of writing joins and metric formulas by hand.

- Runs locally on DuckDB with no account or server. Optional connectors cover
  Snowflake, BigQuery, Databricks, Postgres, Athena, ClickHouse, MotherDuck and DuckLake.
- No telemetry and no update checks. The engine makes network connections only
  for what you configure; see [Telemetry and network access](#telemetry-and-network-access).
- Beta. See [Project status](#project-status) for what is supported and what isn't yet.

## Try it in one command

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches a
compatible Python (3.11 or newer) if your system Python is older.

```bash
uvx semantic-rails ask --package jaffle_shop "revenue by store" --run
```

This plans the question against the bundled synthetic Jaffle Shop package, validates
the plan, compiles SQL and runs it on DuckDB. It prints how it resolved the question
and the Query IR, then the rows.
To try the same loop without installing anything, use the
[browser demo](https://semantic-rails.com/try) or the hosted MCP endpoint below.

## Quickstart with your own package

```bash
uvx semantic-rails init my_package --yes
uvx semantic-rails project validate --path ./my_package
uvx semantic-rails ask --path ./my_package "total amount by event type" --run
```

`init` writes a runnable starter package (YAML models, metrics, examples, tests and
CSV data). Edit it to describe your own tables, then rerun `project validate`.

Pass `--path` (or `--package`) on every command for now. Without it, and without a
saved profile, commands fall back to the bundled `jaffle_shop` package: `ask` answers
from it and `project validate` validates it, both exiting 0.

To keep a `semantic-rails` command on your PATH instead of running it through `uvx`:

```bash
uv tool install semantic-rails
```

If uv warns that its tool directory isn't on your PATH, run `uv tool update-shell` and
open a new terminal.

Or install it into a project environment. Pin the Python version: on a machine without
a uv-managed Python, a bare `uv venv` can pick up the system Python (3.9 on stock macOS,
3.10 on Ubuntu 22.04), and then the install fails.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install semantic-rails
```

With pip, run `python -m pip install semantic-rails` inside a Python 3.11+
environment. On Windows, activate the environment with `.venv\Scripts\activate`; see the
[agent quickstart](docs/AGENT_QUICKSTART.md#local-mcp) for how MCP differs there. CI
doesn't cover Windows yet.

The interactive wizard, `semantic-rails setup --interactive`, walks through the same
steps and can register the MCP server with Claude Desktop or Codex. Inside
`semantic-rails repl`, type `author` to add models, dimensions, measures, metrics and
segments with previews and validation.

## Connect your agent

MCP clients may start in another directory, so give the package's absolute path.

**Claude Code**

```bash
claude mcp add semantic-rails -- uvx semantic-rails mcp stdio --path "$PWD/my_package"
```

**Codex CLI**

```bash
codex mcp add semantic-rails -- uvx semantic-rails mcp stdio --path "$PWD/my_package"
```

**Claude Desktop.** Install the command first (`uv tool install semantic-rails`), then
let Semantic Rails write the client config:

```bash
semantic-rails mcp setup --path "$PWD/my_package" --client claude --mcp query --install --yes
```

Run `mcp setup` without `--install --yes` to preview the change. `--client codex` and
`--client both` also work, and `--mcp both` adds the Architect MCP, which can edit
package files. Don't run `mcp setup --install` through `uvx`: the config would point
into uv's cache, which `uv cache clean` deletes.

**Cursor.** Add the server to `.cursor/mcp.json`, using the path that
`command -v semantic-rails` prints after `uv tool install semantic-rails`:

```json
{
  "mcpServers": {
    "semantic-rails": {
      "command": "/absolute/path/to/semantic-rails",
      "args": ["mcp", "stdio", "--path", "/absolute/path/to/my_package"]
    }
  }
}
```

**Hosted demo (no install).** `https://semantic-rails.com/mcp` is a public Streamable
HTTP endpoint over the same synthetic Jaffle Shop data. It is anonymous and
rate-limited, and it can't load your package.

```bash
claude mcp add --transport http semantic-rails-demo https://semantic-rails.com/mcp
codex mcp add semantic-rails-demo --url https://semantic-rails.com/mcp
```

The agent loop, tool policy and HTTP routes are in
[docs/AGENT_QUICKSTART.md](docs/AGENT_QUICKSTART.md). The full MCP contract, including
`semantic-rails mcp http` for a local Streamable HTTP server, is in
[docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).

## How it works

An agent works through separate, inspectable steps instead of one SQL string:

```text
discover -> inspect -> plan/build-options -> valid-values -> validate -> compile -> execute
```

- `discover` and `inspect` map business terms to governed metric, dimension and
  segment IDs.
- `plan` drafts Query IR from a natural-language question; `build-options` and
  `valid-values` guide step-by-step builders.
- `validate` rejects unknown fields, dimension mismatches, bad filters and policy
  failures with structured errors and, where possible, recovery hints.
- `compile` renders SQL for the target warehouse and returns an `explain` payload:
  the chosen join path to each entity, the candidate paths it considered and the
  relationship contracts along the chosen path.
- `execute` runs the compiled query where the package's connection lives.

The engine design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the
supported modeling surface is in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

## How it compares

dbt's Semantic Layer (MetricFlow), Cube, LookML and Malloy are more mature, and they
reach warehouses Semantic Rails doesn't support yet: Cube alone [connects
to](https://docs.cube.dev/admin/connect-to-data/data-sources) Redshift, SQL Server,
Microsoft Fabric, MySQL and Trino. dbt, Cube and Looker also connect to far more BI
tools and offer caching or pre-aggregation. Semantic Rails is narrower: an engine
built around the agent loop above, which you can run locally or embed.

The [comparison pack](comparisons/semantic_layers/) runs the same 16 questions
through six layers. It measures whether each layer can express a question as a
governed primitive, not performance. Read its methodology disclosure first: 9 of the
16 questions target features Semantic Rails ships natively, and Semantic Rails is
also the reference its answers are checked against. Its
[output consistency check](comparisons/semantic_layers/shared/results/validation/output_consistency.md)
currently reports 14 of the 16 answers matching across all six layers.

Coming from MetricFlow? Translate a MetricFlow YAML directory or a dbt
`semantic_manifest.json` into a package. Anything that doesn't translate is listed
as a warning.

```bash
uvx semantic-rails import --from metricflow --source target/semantic_manifest.json \
  --output . --package-id my_package
```

## Warehouses

DuckDB is included. Add a connector only when you need it:

```bash
uv tool install 'semantic-rails[postgres]'     # also: snowflake, bigquery, databricks, athena, clickhouse
uv pip install 'semantic-rails[all]'           # every connector, into the active environment
```

MotherDuck and DuckLake use the core `duckdb` dependency. Keep secrets in environment
variables or files, not in package YAML. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
and [docs/ADDING_A_DIALECT.md](docs/ADDING_A_DIALECT.md).

## Telemetry and network access

Semantic Rails collects no telemetry and has no update check. The engine opens
network connections only to:

- the warehouses configured in your package's `connection` block;
- DuckDB's extension repository (extensions.duckdb.org), when a MotherDuck or
  DuckLake package needs a DuckDB extension;
- its own local MCP server, when `semantic-rails mcp start` or `mcp status` checks
  that server's `/health` endpoint.

The hosted demo at semantic-rails.com is a separate deployment with its own
[privacy notice](https://semantic-rails.com/legal/2026-09-12/privacy.html).

## Project status

Semantic Rails is beta software. The supported core is the open-source runtime, the
CLI, the MCP stdio and HTTP servers, the `/api/v1/*` HTTP API, the DuckDB path and
Snowflake execution. The other connectors are supported with guardrails, and live
warehouse credentials are exercised on demand, not in every CI run. The
[agent quickstart](docs/AGENT_QUICKSTART.md#supported-vs-experimental) lists what is
experimental or out of scope.

Known limitations in the current release:

- Without `--path` and without a profile, commands fall back to the bundled
  `jaffle_shop` package.
- `plan` and `ask` can return a draft that validates but leaves out or misreads part
  of the question. For example, "revenue by store for 2017" drops the year and still
  reports `ok`. Check the Query IR before you rely on the numbers.
- Currency measures print binary floating-point noise: long decimal tails such as
  `…85000000062`.

### Roadmap

Work in progress, without dates:

- A guided terminal experience that creates and edits a project with pickers and
  live validation.
- A leaner MCP interface: smaller default responses, row caps with explicit
  truncation, and plans that report any part of a question they dropped.
- Architect MCP for real projects: warehouse introspection and dbt `manifest.json`
  import.
- One-step agent setup: Claude Code and Codex plugins, a Claude Desktop bundle and
  Cursor install links.
- A flagship example: a dbt project on an open dataset, modeled end to end.
- A fairer comparison pack, with an independent answer key and a published scoring
  rubric.
- Import and export for Apache Ossie, the incubating Open Semantic Interchange
  specification.

Questions and proposals are welcome in
[GitHub Discussions](https://github.com/semantic-rails/semantic-rails/discussions).

## Docs

- [Getting started](https://semantic-rails.com/docs/getting-started)
- [Agent quickstart](docs/AGENT_QUICKSTART.md)
- [Package authoring](docs/PACKAGE_AUTHORING.md)
- [MCP interface](docs/MCP_INTERFACE.md)
- [Architect MCP](docs/ARCHITECT_MCP.md)
- [Query API](docs/QUERY_API.md)
- [Capabilities](docs/CAPABILITIES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Benchmark evidence](docs/BENCHMARK_EVIDENCE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Comparison pack](comparisons/semantic_layers/)
- [Changelog](CHANGELOG.md)

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers scope, architecture ownership and the
validation commands. To work from a source checkout:

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
uv pip install --python .venv/bin/python --reinstall dist/semantic_rails-0.2.1-py3-none-any.whl
uv run python scripts/verify_package_distribution.py --dist-dir dist --no-build
```

## Package naming

Semantic Rails is the public product name. The PyPI distribution is
`semantic-rails`, the Python import package is `semantic_rails`, and the CLI is
`semantic-rails`. The published distribution also includes `mf2sr`, the
MetricFlow translator behind `semantic-rails import`.

## Support, security and license

Support, issue reporting, conduct and security reporting are documented in
[SUPPORT.md](SUPPORT.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and [SECURITY.md](SECURITY.md).

Semantic Rails is licensed under Apache 2.0; see [LICENSE](LICENSE). Everything in
this repository is open source, with no gated features. Semantic Rails, Inc., which
runs [semantic-rails.com](https://semantic-rails.com), also offers a hosted service;
nothing here requires it.
