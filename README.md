# Semantic Rails

[![CI](https://github.com/semantic-rails/semantic-rails/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/semantic-rails/semantic-rails/actions/workflows/ci.yml?query=branch%3Amain) [![PyPI](https://img.shields.io/pypi/v/semantic-rails.svg?label=PyPI)](https://pypi.org/project/semantic-rails/) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/semantic-rails/) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/semantic-rails/semantic-rails/blob/main/LICENSE) [![Docs](https://img.shields.io/badge/docs-semantic--rails.com-0f766e.svg)](https://semantic-rails.com/docs/)

Semantic Rails is an open-source, agent-first semantic layer, licensed Apache-2.0.
You define metrics, dimensions and join paths once, in YAML. Agents then discover
those definitions, plan a query and run it as governed SQL through an MCP server,
a CLI or an HTTP API, instead of writing joins and metric formulas by hand.

- Runs locally on DuckDB with no account or server. Optional connectors cover
  Snowflake, BigQuery, Databricks, Postgres, Athena, ClickHouse, MotherDuck and DuckLake.
- No telemetry and no update checks. The engine makes network connections only
  for what you configure; see
  [Telemetry and network access](https://github.com/semantic-rails/semantic-rails#telemetry-and-network-access).
- Beta. See [Project status](https://github.com/semantic-rails/semantic-rails#project-status) for what is supported
  and what isn't yet.

## Try it in one command

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches a
compatible Python (3.11 or newer) if your system Python is older.

```bash
uvx semantic-rails ask --package jaffle_shop "revenue by store" --run
```

`uvx` runs Semantic Rails without installing it; the next section shows how to install it.
`--package` names a bundled sample package; pass your own package with `--path`.

This plans the question against the synthetic Jaffle Shop sample, then validates,
compiles and runs the plan on DuckDB. It prints how it interpreted the question, the
rows and any warnings, then the Query IR. Check that interpretation before you rely on
the numbers; see
[Known limitations](https://github.com/semantic-rails/semantic-rails#known-limitations).
Without uv, try the [browser demo](https://semantic-rails.com/try) or the hosted MCP
endpoint below.

## Install

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
[agent quickstart](https://github.com/semantic-rails/semantic-rails/blob/main/docs/AGENT_QUICKSTART.md#local-mcp)
for how MCP differs there. CI doesn't cover Windows yet.

## Quickstart with your own package

Pass `--path` on commands that use your own package, or run them inside the package
directory. Without either, and without a saved profile, commands stop with
`no_package_selected` and list the ways to choose a package; at an interactive terminal,
`ask` first offers the bundled sample package.

```bash
uvx semantic-rails init my_package --yes
uvx semantic-rails project validate --path ./my_package
uvx semantic-rails ask --path ./my_package "total amount by event type" --run
```

`init` writes a runnable starter package (YAML models, metrics, examples, tests and
CSV data). Edit it to describe your own tables, then rerun `project validate`.

The interactive wizard, `semantic-rails setup --interactive`, walks through the same
steps and can register the MCP server with Claude Desktop or Codex. Run it from an
installed `semantic-rails`, not through `uvx`, for the reason given under
[Claude Desktop](https://github.com/semantic-rails/semantic-rails#connect-your-agent) below.
Inside `semantic-rails repl`, type `author` to add models, dimensions, measures, metrics and
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

**Claude Desktop.** Install the command, then let Semantic Rails write the client
config:

```bash
uv tool install semantic-rails
"$(uv tool dir --bin)/semantic-rails" mcp setup --path "$PWD/my_package" --client claude --mcp query --install --yes
```

`uv tool dir --bin` finds the command even when uv's tool directory isn't on your
`PATH` yet. Run `mcp setup` without `--install --yes` to preview the change.
`--client codex`, `--client claude-code` (registers the server at user scope),
`--client cursor` (writes `~/.cursor/mcp.json`, so the manual Cursor config below is
optional) and `--client both` (Claude Desktop and Codex) also work, and `--mcp both`
adds the Architect MCP, which can edit package files. Don't run `mcp setup --install`
through `uvx`: the config would point into uv's cache, which `uv cache clean` deletes.

**Cursor.** Install the command and print the two absolute paths the config needs:

```bash
uv tool install semantic-rails
echo "$(uv tool dir --bin)/semantic-rails"
echo "$PWD/my_package"
```

Then add the server to `.cursor/mcp.json`:

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
[docs/AGENT_QUICKSTART.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/AGENT_QUICKSTART.md).
The full MCP contract, including `semantic-rails mcp http` for a local HTTP server, is in
[docs/MCP_INTERFACE.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/MCP_INTERFACE.md).

## How it works

An agent works through separate, inspectable steps instead of one SQL string:

```text
discover -> plan -> execute
```

- `discover` maps business terms to governed metric, dimension and segment IDs.
  `inspect` opens one object's card when the agent needs its aggregations, values or
  time roles.
- `plan` drafts Query IR from a natural-language question and checks the draft against
  the question. Run the draft when its status is `ok` and it has no warnings; otherwise
  the response says what the draft misses. `valid-values` (and, over HTTP and the CLI,
  `build-options`) guide step-by-step builders instead.
- `execute` (the CLI's `query`, HTTP `/api/v1/query`) validates, compiles and runs the
  Query IR where the package's connection lives. It rejects unknown fields, dimension
  mismatches, bad filters and policy failures with structured errors and, where
  possible, recovery hints.
- `validate` and `compile` are optional dry runs of the same checks. `validate` returns
  the diagnostics without running anything; `compile` also renders SQL for the target
  warehouse. At `compact` verbosity (the CLI's default) or `full`, `compile` returns an
  `explain` payload: the chosen join path to each entity, the candidate paths it
  considered and the relationship contracts along the chosen path. On MCP they are
  `execute` modes `validate` and `sql`, which default to `minimal` and leave `explain` out.

The engine design is in
[docs/ARCHITECTURE.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/ARCHITECTURE.md),
and the supported modeling surface is in
[docs/CAPABILITIES.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/CAPABILITIES.md).

## How it compares

dbt's Semantic Layer (MetricFlow), Cube, LookML and Malloy are more mature, and they
reach warehouses Semantic Rails doesn't support yet: Cube alone [connects
to](https://docs.cube.dev/admin/connect-to-data/data-sources) Redshift, SQL Server,
Microsoft Fabric, MySQL and Trino. dbt, Cube and Looker also connect to far more BI
tools and offer caching or pre-aggregation. Semantic Rails is narrower: an engine
built around the agent loop above, which you can run locally or embed.

The [comparison pack](https://github.com/semantic-rails/semantic-rails/tree/main/comparisons/semantic_layers/)
asks the same 16 questions of six modeled layers: Semantic Rails, MetricFlow, Cube, Malloy, Snowflake Semantic Views
and KtX. It compares capability, not performance; its support labels describe the
authored models, not each layer's limits. Nine questions were chosen to exercise
primitives Semantic Rails ships. The
[output consistency check](https://github.com/semantic-rails/semantic-rails/blob/main/comparisons/semantic_layers/shared/results/validation/output_consistency.md)
compares five layers on the current shared dataset, including Semantic Rails,
against an independent SQL answer key: all 16 match. Cube's captured SQL was
replayed on current data, but Cube itself was not rerun. Snowflake Semantic Views
is a stale capture on an older dataset and is excluded from that count; it matches
14 questions and differs on q07 and q16. The authored models and shared data leave
some intended semantics weakly tested, so matching outputs are not a ranking.

Coming from MetricFlow? Translate a MetricFlow YAML directory or a dbt
`semantic_manifest.json` into a new package. The importer is partial: models and
measures it can't translate are listed as warnings, but a metric that used them can
still be written out. `project validate --mode parse` then fails and names that
metric; remove it, or define the measure it names, before you rely on the import.

```bash
uvx semantic-rails import --from metricflow --source target/semantic_manifest.json \
  --output . --package-id my_dbt_package
```

The imported package targets DuckDB and names a seed script,
`data/seed_my_dbt_package.sql`, that the import doesn't create. Before
`project validate`, replace `seed` with your warehouse's `connection` (see
[`package.yml`](https://github.com/semantic-rails/semantic-rails/blob/main/docs/PACKAGE_AUTHORING.md#packageyml))
or add that script.

## Warehouses

DuckDB is included. Add a connector only when you need it, for example Postgres:

```bash
uv tool install 'semantic-rails[postgres]'
```

The other connector extras are `snowflake`, `bigquery`, `databricks`, `athena` and
`clickhouse`, and `all` installs every connector. In a project environment, use
`uv pip install 'semantic-rails[all]'`. MotherDuck and DuckLake use the core `duckdb`
dependency. Keep secrets in environment
variables or files, not in package YAML. See
[docs/DEPLOYMENT.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/DEPLOYMENT.md) and
[docs/ADDING_A_DIALECT.md](https://github.com/semantic-rails/semantic-rails/blob/main/docs/ADDING_A_DIALECT.md).

## Telemetry and network access

Semantic Rails collects no telemetry and has no update check. The engine opens
network connections only to:

- the warehouses configured in your package's `connection` block;
- DuckDB's extension repository (extensions.duckdb.org): DuckDB downloads an
  extension it doesn't bundle the first time a query needs one, for example for
  MotherDuck, DuckLake or remote files;
- its own local MCP server, when `semantic-rails mcp start` or `mcp status` checks
  that server's `/health` endpoint.

The hosted demo at semantic-rails.com is a separate deployment with its own
[privacy notice](https://semantic-rails.com/legal/2026-09-12/privacy.html).

## Project status

Semantic Rails is beta software. The supported core is the open-source runtime, the
CLI, the MCP stdio and HTTP servers, the `/api/v1/*` HTTP API, the DuckDB path and
Snowflake execution. The other connectors are supported with guardrails, and live
warehouse credentials are exercised on demand, not in every CI run. The
[agent quickstart](https://github.com/semantic-rails/semantic-rails/blob/main/docs/AGENT_QUICKSTART.md#supported-vs-experimental)
lists what is experimental or out of scope.

### Known limitations

In the current release:

- `plan` reports the parts of a question its draft doesn't honor, as `low_confidence`
  or a `PLAN_UNMATCHED_TERMS` warning, but its checks don't cover every phrasing. For
  "revenue by store before today" it plans today alone and reports `ok` with only that
  warning, and `ask` runs it. Check the Query IR, or `ask`'s "Interpreted as" line,
  before you rely on the numbers.
- `ask` rounds its tables, but JSON results (`query`, `ask --json`, MCP `execute` and the
  HTTP API) return the warehouse's floating-point values as they are, for example
  `486468.17999985756` for a currency total.
- The MetricFlow importer is partial. It can keep a metric whose model or measure it
  dropped, and package validation then fails, naming that metric.

### Roadmap

Work in progress, without dates:

- Packaged agent integrations: Claude Code and Codex plugins and a Claude Desktop
  bundle. `mcp setup` already writes the client configs.
- A flagship example: a dbt project on an open dataset, modeled end to end.
- Broader native-model coverage in the comparison pack and a refreshed Snowflake
  capture on the current dataset.
- Import and export for Apache Ossie, the incubating Open Semantic Interchange
  specification.

Questions and proposals are welcome in
[GitHub Discussions](https://github.com/semantic-rails/semantic-rails/discussions).

## Docs

- [Getting started](https://semantic-rails.com/docs/getting-started)
- [Agent quickstart](https://github.com/semantic-rails/semantic-rails/blob/main/docs/AGENT_QUICKSTART.md)
- [Package authoring](https://github.com/semantic-rails/semantic-rails/blob/main/docs/PACKAGE_AUTHORING.md)
- [MCP interface](https://github.com/semantic-rails/semantic-rails/blob/main/docs/MCP_INTERFACE.md)
- [Architect MCP](https://github.com/semantic-rails/semantic-rails/blob/main/docs/ARCHITECT_MCP.md)
- [Query API](https://github.com/semantic-rails/semantic-rails/blob/main/docs/QUERY_API.md)
- [Capabilities](https://github.com/semantic-rails/semantic-rails/blob/main/docs/CAPABILITIES.md)
- [Architecture](https://github.com/semantic-rails/semantic-rails/blob/main/docs/ARCHITECTURE.md)
- [Benchmark evidence](https://github.com/semantic-rails/semantic-rails/blob/main/docs/BENCHMARK_EVIDENCE.md)
- [Deployment](https://github.com/semantic-rails/semantic-rails/blob/main/docs/DEPLOYMENT.md)
- [Comparison pack](https://github.com/semantic-rails/semantic-rails/tree/main/comparisons/semantic_layers/)
- [Changelog](https://github.com/semantic-rails/semantic-rails/blob/main/CHANGELOG.md)

## Contributing

[CONTRIBUTING.md](https://github.com/semantic-rails/semantic-rails/blob/main/CONTRIBUTING.md)
covers scope, architecture ownership and the validation commands. To work from a source checkout:

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
uv pip install --python .venv/bin/python --reinstall dist/semantic_rails-0.3.1-py3-none-any.whl
uv run python scripts/verify_package_distribution.py --dist-dir dist --no-build
```

## Package naming

Semantic Rails is the public product name. The PyPI distribution is
`semantic-rails`, the Python import package is `semantic_rails`, and the CLI is
`semantic-rails`. The published distribution also includes `mf2sr`, the
MetricFlow translator behind `semantic-rails import`.

## Support, security and license

Support, issue reporting, conduct and security reporting are documented in
[SUPPORT.md](https://github.com/semantic-rails/semantic-rails/blob/main/SUPPORT.md),
[CONTRIBUTING.md](https://github.com/semantic-rails/semantic-rails/blob/main/CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](https://github.com/semantic-rails/semantic-rails/blob/main/CODE_OF_CONDUCT.md) and
[SECURITY.md](https://github.com/semantic-rails/semantic-rails/blob/main/SECURITY.md).

Semantic Rails is licensed under Apache 2.0; see
[LICENSE](https://github.com/semantic-rails/semantic-rails/blob/main/LICENSE). Everything in this
repository is open source, with no gated features. Semantic Rails, Inc., which
runs [semantic-rails.com](https://semantic-rails.com), also offers a hosted service;
nothing here requires it.
