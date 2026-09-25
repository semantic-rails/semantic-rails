# Agent Quickstart

This guide is for agents and agent applications that need governed analytics without giving the
model direct warehouse access. The core contract is:

```text
discover -> plan -> execute
```

Use the earliest tool that can answer the next question. `inspect`, `build-options` and
`valid-values` help choose objects and values. `execute` validates and compiles the query before it
runs it, so `validate` and `compile` are optional dry runs. Run a `plan` draft only when its status
is `ok` and it has no warnings.

## Local MCP

Copy-paste setup for Claude Code, Codex, Claude Desktop, Cursor and the hosted demo endpoint
is in the README under [Connect your agent](../README.md#connect-your-agent).

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
before signaling a background PID. It uses `ps` for that, so it also refuses (and
starts nothing) where `ps` is missing, as in minimal container images such as
`python:3.12-slim`; install `procps` there. On Windows, prefer
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
API to discover that governed surface, assemble Query IR, and execute it locally or customer-side;
the runtime validates and compiles every query before it runs.

Start the active package:

```bash
uv run semantic-rails serve --package jaffle_shop --port 8081
```

Routes are available under stable `/api/v1/*` paths.

### Recommended Loop

```text
discover -> plan -> execute
```

- `discover` maps business terms to governed semantic objects.
- `inspect` (optional) opens an object card with usage, provenance, comparison metadata, and
  starter patches.
- `plan` returns a validated best Query IR draft for natural-language intents. Use `detail="full"` only when you need alternatives or blocked drafts.
- `build-options` returns legal next query choices for guided builders.
- `valid-values` searches categorical values for selected dimensions.
- `execute` is the MCP tool name (HTTP path `/api/v1/query`, CLI verb `semantic-rails query`). It
  validates and compiles the request, then executes it in the local or customer-operated runtime.
  An invalid query fails with a structured error and, where possible, recovery hints instead of
  running.
- `validate` (optional dry run) returns diagnostics, repair hints, output columns, and risk
  metadata without executing.
- `compile` (optional dry run) returns SQL and plan metadata without executing. At `compact` or `full` verbosity,
  its response also includes an `explain` payload with the semantic and physical plan plus a
  `chosen_paths` map keyed by target entity ID — each entry carries `selected` (the chosen
  relationship path), `candidates` (every considered path), and `contracts` (the relationship
  contracts along the selected path). The CLI defaults to `compact`; the MCP tool defaults to
  `minimal`, which leaves `explain` out, so pass `verbosity: "compact"` to review join paths
  and safety before execution.

### Plan Status And Detail

Use `plan` when the user gives a natural-language analysis intent. Use `build-options` when the
user is interactively editing Query IR one step at a time.

`plan` runs validation inline and checks the draft against the question. When `status="ok"` and
there are no `warnings`, agents can forward `best.query_ir` directly to `execute`
(`/api/v1/query`). Call `validate` only when you want diagnostics without running the query, for
example after editing Query IR or after `low_confidence`.

The checks cover time windows, rankings, named filter values, and exclusions, not every phrasing:
a draft can still misread a question and report `ok`, sometimes with only a `PLAN_UNMATCHED_TERMS`
warning (see the README's known limitations). Compare `best.query_ir` with the question before
executing it.

Statuses are:

- `ok`: the best draft validated, and no check found part of the question it leaves out.
  `warnings` can still name question words the draft doesn't use (`PLAN_UNMATCHED_TERMS`).
- `low_confidence`: a draft exists, but validation failed, the draft leaves out part of the
  question (`why` names it, for example `PLAN_INTENT_COVERAGE_GAP` or `TIME_WINDOW_UNRESOLVED`),
  or a validating fallback would drift from the requested target, grouping, qualification,
  filters, or time scope.
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
3. Call `inspect` on a candidate metric, measure, dimension, or segment when you need its
   aggregations, values, or time roles.
4. Use `plan` for natural-language questions, or `build-options` plus `valid-values` when the
   agent is interactively assembling Query IR.
5. Run a `plan` draft only when its status is `ok` and it has no warnings; otherwise fix the Query
   IR or ask the user.
6. Prefer `compile` when the user wants SQL, lineage, path selection, or explain output.
7. Call `execute` when the user wants rows. It validates and compiles first, so it needs no
   separate `validate` or `compile` call; use `validate` for diagnostics without running the query.

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
discover terms -> plan intent -> execute query
```

Expose `inspect`, `validate`, and `compile` as optional tools for object details and dry runs.

Keep warehouse credentials outside the model context. The model should receive structured results
and recovery hints, not raw credentials or arbitrary SQL execution privileges.

If a developer has configured `~/.semantic_rails/profiles.yml`, treat it as
local CLI state only. Agent and MCP host configs should pass explicit package
paths, and service deployments should rely on their own config/vault boundary.

### Graph-Style Agents

Model the loop as explicit nodes:

```text
orient -> discover -> draft -> execute
```

Branch on structured status fields: a `plan` draft that isn't `ok`, or has warnings, goes to a
repair node before `execute`. `INVALID_QUERY`, `PATH_JOIN_CONFLICT`,
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
