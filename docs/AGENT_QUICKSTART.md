# Agent Quickstart

This guide is for agents and agent applications that need governed analytics without giving the
model direct warehouse access. The core contract is:

```text
discover -> inspect -> plan/build-options -> valid-values -> validate -> compile -> execute
```

Use the earliest tool that can answer the next question. Do not skip straight to `execute` unless
the query has already passed validation or was produced by `plan` with an `ok` status.

## Local MCP

For a UV-installed project, start MCP against the package you created with
`semantic-rails setup --interactive` or `semantic-rails init`. Use an absolute
`--path` because MCP clients may start outside your project directory:

```bash
PACKAGE_PATH="$(pwd)/my_package"
semantic-rails mcp setup --path "$PACKAGE_PATH"
```

On POSIX systems, start a terminal-managed HTTP smoke in the background:

```bash
semantic-rails mcp start --path "$PACKAGE_PATH" --port 8091
semantic-rails mcp status --path "$PACKAGE_PATH"
```

Managed `start/status/stop` is POSIX-only because it verifies process identity
before signaling a background PID. On Windows, prefer
`semantic-rails mcp setup --install --yes` so the client launches stdio, or run
`semantic-rails mcp http ...` as a foreground process in a separate terminal.

For desktop MCP clients, prefer `semantic-rails mcp setup --install --yes`
or the lower-level `client-config` commands below. The raw
`semantic-rails mcp stdio --path "$PACKAGE_PATH"` and
`semantic-rails mcp http --path "$PACKAGE_PATH" --host 127.0.0.1 --port 8091`
commands are foreground servers; they stay open until the client disconnects or
you stop the process.

From a source checkout, prefix commands with `uv run`, or point at the bundled
synthetic Jaffle Shop package:

```bash
uv run semantic-rails mcp stdio --package jaffle_shop
uv run semantic-rails mcp http --package jaffle_shop --host 127.0.0.1 --port 8091
```

Then list tool names:

```bash
curl http://127.0.0.1:8091/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python -c 'import json, sys; tools=json.load(sys.stdin)["result"]["tools"]; print(f"{len(tools)} tools"); print("\n".join("- " + tool["name"] for tool in tools))'
```

The response should include tools such as `capabilities`, `catalog`, `discover`, `plan`,
`validate`, `compile`, and `execute`.

On POSIX, stop the managed server when you are done with the terminal smoke:

```bash
semantic-rails mcp stop --path "$PACKAGE_PATH"
```

`mcp setup` is the quick local workflow path before wiring a client: it loads the
package, checks that the MCP tools are available, and previews the Claude/Codex
config it would write. Run it from inside the package directory, pass
`--path "$PACKAGE_PATH"`, or set a local profile with
`semantic-rails profile init --package-path ./my_package`.

```bash
semantic-rails mcp setup --path "$PACKAGE_PATH"
semantic-rails mcp setup --path "$PACKAGE_PATH" --client both --mcp both --install --yes
```

For lower-level client config control:

```bash
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client both --mcp both
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client claude --mcp both --install --yes
semantic-rails mcp client-config --path "$PACKAGE_PATH" --client codex --mcp both --install --yes
```

`--mcp query` exposes the runtime/query MCP. `--mcp architect` exposes the
package-authoring MCP. `--mcp both` installs both entries.

## HTTP API Path

Semantic Rails is designed for human-defined semantics and agent-first query building. Humans own
entities, measures, metrics, policies, examples, and tests in the package. Agents use the runtime
API to discover that governed surface, assemble Query IR, validate it, and compile SQL before
local/customer-side execution.

Start the active package:

```bash
uv run semantic-rails serve --package jaffle_shop --port 8081
```

Routes are available under stable `/api/v1/*` paths.

### Recommended Loop

```text
discover -> inspect -> plan/build-options -> valid-values -> validate -> compile -> execute
```

- `discover` maps business terms to governed semantic objects.
- `inspect` opens an object card with usage, provenance, comparison metadata, and starter patches.
- `plan` returns a validated best Query IR draft for natural-language intents. Use `detail="full"` only when you need alternatives or blocked drafts.
- `build-options` returns legal next query choices for guided builders.
- `valid-values` searches categorical values for selected dimensions.
- `validate` returns diagnostics, repair hints, output columns, and risk metadata.
- `compile` returns SQL and plan metadata without executing. Its response includes an `explain`
  payload with the semantic and physical plan plus a `chosen_paths` map keyed by target entity
  ID — each entry carries `selected` (the chosen relationship path), `candidates` (every
  considered path), and `contracts` (the relationship contracts along the selected path).
  Review these to confirm join paths and safety before execution.
- `execute` is the MCP tool name (HTTP path `/api/v1/query`, CLI verb `semantic-rails query`) and
  executes the compiled request in the local or customer-operated runtime.

### Plan Status And Detail

Use `plan` when the user gives a natural-language analysis intent. Use `build-options` when the
user is interactively editing Query IR one step at a time.

`plan` runs validation inline. When `status="ok"`, agents can forward `best.query_ir` directly
to `compile` or `/api/v1/query`; call `validate` again only for hand-authored or edited Query IR,
or when full diagnostics are needed after `low_confidence`.

Statuses are:

- `ok`: the best draft validated.
- `low_confidence`: a draft exists, but validation failed or a validating fallback would drift from
  the requested target, grouping, qualification, filters, or time scope.
- `unrealizable`: the intent parsed, but no pattern or fallback produced Query IR.
- `out_of_scope`: the classifier or relevance gate rejected the request as outside the package.

Use `detail="query"` for a compact MCP QA loop, `detail="best"` for normal clients,
`detail="full"` for alternatives and blocked drafts, and `detail="debug"` only when
troubleshooting composition hints.

### Minimal Agent Calls

Discover:

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/discover \
  -H 'Content-Type: application/json' \
  -d '{"terms":"orders","limit":5}'
```

Inspect:

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/inspect \
  -H 'Content-Type: application/json' \
  -d '{"object_id":"measure.jaffle.order_count"}'
```

Plan:

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/plan \
  -H 'Content-Type: application/json' \
  -d '{"intent":"new customer orders over time","detail":"best"}'
```

Compile without execution:

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/compile \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "version": 1,
      "select": [
        { "expression": { "measure": "measure.jaffle.revenue_usd" }, "as": "revenue_usd" }
      ],
      "group_by": ["dimension.jaffle_store_name"],
      "time": { "temporal_role": "temporal_role.jaffle_order_time", "grain": "month" },
      "limit": 5
    }
  }'
```

Review relationship paths and contracts for the same query (call `/compile` and inspect the
nested `explain.chosen_paths` map — one `{selected, candidates, contracts}` entry per
target entity):

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/compile \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "version": 1,
      "select": [
        { "expression": { "measure": "measure.jaffle.order_count", "aggregation": "count_distinct" }, "as": "orders" }
      ],
      "group_by": ["dimension.jaffle_store_name"]
    }
  }' \
  | jq '.explain.chosen_paths | map_values({selected, candidates, contracts})'
```

### Policy Context

Discovery and query routes accept optional policy context:

```json
{
  "policy_context": {
    "environment": "production",
    "audience": "finance",
    "roles": ["sales"]
  }
}
```

When provided, the runtime applies package visibility, access, and metric-constraint rules
consistently across discovery, metadata, validation, compile, and query calls.

## Recommended Agent Policy

1. Call `capabilities` once per package to learn expression shapes, limits, and unsupported
   capabilities.
2. Call `discover` with business terms before selecting IDs. Treat low-relevance or off-topic
   responses as a stop condition.
3. Call `inspect` on candidate metrics, measures, dimensions, or segments before composing a query.
4. Use `plan` for natural-language questions, or `build-options` plus `valid-values` when the
   agent is interactively assembling Query IR.
5. Call `validate` before `compile` or `execute` when the Query IR did not come from an `ok` plan.
6. Prefer `compile` when the user wants SQL, lineage, path selection, or explain output.
7. Call `execute` only when the user explicitly wants rows and the query has passed the governed
   path.

Use `summary`, `minimal`, or `compact` verbosity unless the user asks for debugging detail. Request
`full` only for explainability, test failure triage, or query review.

## Client Patterns

### MCP Desktop Clients

Point the client at the stdio command above for local work, or at a
self-hosted Streamable HTTP endpoint. Use absolute paths in client config
because the MCP host may not start in your project directory. The server
exposes the same tool names across transports, so the agent prompt should
reference tool semantics, not transport details.

### Tool-Calling Agents

If a framework does not speak MCP directly, wrap each `/api/v1/*` route as a tool with the same
loop policy:

```text
discover terms -> inspect object_id -> plan intent -> validate query -> compile query -> execute query
```

Keep warehouse credentials outside the model context. The model should receive structured results
and recovery hints, not raw credentials or arbitrary SQL execution privileges.

If a developer has configured `~/.semantic_rails/profiles.yml`, treat it as
local CLI state only. Agent and MCP host configs should pass explicit package
paths, and service deployments should rely on their own config/vault boundary.

### Graph-Style Agents

Model the loop as explicit nodes:

```text
orient -> discover -> inspect -> draft -> validate -> compile -> execute
```

Branch on structured status fields. `INVALID_QUERY`, `PATH_JOIN_CONFLICT`,
`MIXED_GRAIN_INVALID`, `POLICY_DENIED`, and low-relevance results should route to repair or refusal
nodes instead of being retried as raw SQL.

## Local Warehouse Defaults

DuckDB is the default zero-setup runtime and runs in every gate. Snowflake, Postgres, BigQuery,
Databricks, Athena, ClickHouse, MotherDuck, and DuckLake are optional connectors with dialect-level
coverage. Live cloud warehouse execution depends on local credentials and is exercised on demand,
not in every PR gate.

## Supported vs Experimental

Supported core:

- DuckDB local runtime, CLI, local MCP stdio, local MCP HTTP, and stable `/api/v1/*` routes.
- Package authoring, package checks, examples, tests, validation, compile, explain, and
  local/customer-side execution.
- Snowflake execution through Snow CLI or the optional native connector when a package connection
  is configured.

Supported with guardrails:

- Mixed-grain rewrites for supported shapes.
- Historical slicing through declared temporal-validity joins.
- `metric_predicate` for contextual and entity-only predicate shapes.
- Event-count conversion metrics for the supported execution model.
- Non-DuckDB warehouse connectors through optional drivers and conformance tests; live credentials
  are exercised on demand, not in every CI run.

Experimental or out of scope:

- Managed acceleration, billing, tenant administration, JWT/JWKS, and production observability.
- Arbitrary chained SQL pipelines, user-authored CTE orchestration, managed materialization refresh,
  or a general ELT runtime.
- Fully general nested predicate planning beyond the supported predicate shapes.

When a request falls outside the supported surface, the agent should surface the structured error and
recovery hints instead of inventing SQL.
