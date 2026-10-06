# Agent Quickstart

This guide is for agents and agent applications that need governed analytics without giving the
model direct warehouse access. The core contract is:

```text
discover -> plan -> execute
```

Use the earliest tool that can answer the next question. `inspect` and `valid-values` help choose
objects and values. `execute` validates and compiles the query before it runs it, so its `validate`
and `sql` modes (HTTP `/validate` and `/compile`) are optional dry runs. Run a `plan` draft only
when its status is `ok` and it has no warnings.

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
`python:3.12-slim`; install `procps` there. A `ps` that can't run or doesn't answer
within five seconds counts the same way, and `stop` never signals a process it
could not identify. If identity observation fails, `stop` reports
`ok: false` with `identity_unverifiable` and leaves the server registered so you
can retry. An observed identity mismatch removes the stale registration without
signaling the process. On Windows, prefer
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

The response should list six tools: `discover`, `inspect`, `valid-values`, `plan`, `execute` and
`segment`.

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
- `plan` returns the best Query IR draft for natural-language intents, with a `status` that says
  whether to run it. Use `detail="full"` only when you need alternatives or blocked drafts.
- `build-options` returns legal next query choices for guided builders.
- `valid-values` searches categorical values for selected dimensions.
- `execute` is the MCP tool name (HTTP path `/api/v1/query`, CLI verb `semantic-rails query`). It
  validates and compiles the request, then executes it in the local or customer-operated runtime.
  An invalid query fails with a structured error and, where possible, recovery hints instead of
  running. On MCP it returns at most `max_rows` rows (default 200); a capped result sets
  `truncated` and warns `EXECUTE_ROWS_TRUNCATED`.
  A window (`start` and/or `end`) without a `time.grain` returns one total over the window, with no
  time column, and says so in `assumptions`; set `time.grain` for one row per period. A time role
  with no window and no grain still returns one row per timestamp (`UNGRAINED_TIME_PROJECTION`).
  Every MCP tool and mode fits one 32,000-character response budget. Optional plans and metadata
  are trimmed first (`omitted_fields` names them); required data that still cannot fit is
  refused with `RESULT_TOO_LARGE`. Compact responses omit compiler plans.
- `validate` (optional dry run) returns diagnostics, repair hints, output columns, and risk
  metadata without executing.
- `compile` (optional dry run) returns SQL and plan metadata without executing. At `compact` or `full` verbosity,
  its response also includes an `explain` payload with the semantic and physical plan plus a
  `chosen_paths` map keyed by target entity ID — each entry carries `selected` (the chosen
  relationship path), `candidates` (every considered path), and `contracts` (the relationship
  contracts along the selected path). The CLI defaults to `compact`; on MCP, `execute` with
  `mode: "sql"` defaults to `minimal`, which leaves `explain` out, so pass
  `verbosity: "compact"` to review join paths and safety before execution.

### Plan Status And Detail

Use `plan` when the user gives a natural-language analysis intent. Use `build-options` when the
user is interactively editing Query IR one step at a time.

`plan` runs validation inline and checks the draft against the question. When `status="ok"` and
there are no `warnings`, agents can forward `best.query_ir` directly to `execute`
(`/api/v1/query`). Call `validate` only when you want diagnostics without running the query, for
example after editing Query IR or after `low_confidence`.

Catalog fallback ranking breaks equal intent-match scores by discovery score, then object id,
so candidate order and refusal diagnostics stay the same across Python hash seeds.

The checks cover time windows, rankings, named filter values, and exclusions, not every phrasing:
a draft can still misread a question and report `ok`, sometimes with only a `PLAN_UNMATCHED_TERMS`
warning (see the README's known limitations). Compare `best.query_ir` with the question before
executing it.

Statuses are:

- `ok`: the best draft validated, no check found part of the question it leaves out, and every
  number, clock or zone word, and word that names a catalog object (in a label or alias, or the
  last dotted part of an id or name outside its namespaces; never only a description) in the
  question is used by the draft: by an object it selects, a filter value, a time grain or a time
  phrase, never a synonym, a typo or a framing word. Every grouping the draft adds, each
  `group_by` dimension and a time grain that splits the rows, traces to the question too
  (otherwise `PLAN_UNASKED_GROUPING`), and a ranking keeps the top N of the entity it ranks
  (otherwise `PLAN_RANKING_PERIOD_AMBIGUOUS`, with no runnable option: ask the user which
  ranking they mean, such as the top N overall or the top N in each period).
  `warnings` can still name other question words the draft doesn't use (`PLAN_UNMATCHED_TERMS`).
  Parsed qualification drafts are held with `PLAN_INTENT_COVERAGE_GAP` until their cohort and
  time scope can be proven; entity keys or key counts alone do not prove that scope.
  Store groupings resolve through the catalog;
  a bare "by store" may need clarification about the intended dimension.
  Request words such as "show" still count when they are exact catalog names. A time grain
  consumes its own unit and its "-ly" form; a grouping that names the query's clock at the
  planned grain ("by order date") is consumed. Other time words must occur inside a recorded
  time phrase, and a prior-period shift consumes only its comparison phrase. Regular plurals are recognized
  and consumed using the same forms. "Number of" is consumed by a selected count-valued
  measure, including a snapshot count whose aggregation is `last_value`.
  An unknown word left in `intent_ir.unresolved` also blocks readiness when no object, filter
  value, grain or recorded span consumes it and it isn't a stopword or number word. The
  `PLAN_UNMATCHED_TERMS` reason names it with `kind="filter_values_unrealized"`; use
  `valid_values` to find the value, add the filter and validate, or ask again without the word.
- `low_confidence`: a draft exists, but validation failed, the draft leaves out part of the
  question (`why` names it, for example `PLAN_INTENT_COVERAGE_GAP`, or `TIME_WINDOW_UNRESOLVED`,
  which returns no `query_ir`: pass the window, temporal role and grain in `query.time` and
  plan again),
  or a validating fallback would drift from the requested target, grouping, qualification,
  filters, or time scope. For `TIME_WINDOW_UNRESOLVED`, follow `why.recovery_hints`; when
  `why.details.conflicting_phrases` names two windows that differ, as in "Q2 2017 (April 1 to
  June 29, 2017)", keep the one you mean and plan again. (One window stated twice the same way
  resolves.) On a package without time, every plan with a draft yields `low_confidence`,
  including plain catalogue questions. The Query IR is retained, with the same
  `INVALID_TEMPORAL_ROLE` warning: "This package has no time; check the question doesn't ask
  for a time breakdown or window."
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

1. Call `discover` with business terms before selecting IDs. Treat low-relevance or off-topic
   responses as a stop condition.
2. Call `inspect` on a candidate metric, measure, dimension, or segment when you need its
   aggregations, values, or time roles.
3. Use `plan` for natural-language questions, and `valid-values` for filter values.
4. Run a `plan` draft only when its status is `ok` and it has no warnings; otherwise fix the Query
   IR or ask the user.
5. Use `execute` with `mode: "sql"` when the user wants SQL, lineage, path selection, or explain
   output.
6. Call `execute` when the user wants rows. It validates and compiles first, so it needs no
   separate dry run; use `mode: "validate"` for diagnostics without running the query.
7. On `AMBIGUOUS_CHILD_SCOPE`, the conditions on a child entity can mean the same child row or
   separate ones (or, negated, "has a row that is not X" or "has no row that is X"). Answer
   `details.clarification.question` from the user's question, or ask the user, then resend the
   chosen option's `where` unchanged. Write a child group (`{child, match, where}`) up front when
   the question already says it.
   A `PLAN_UNMATCHED_TERMS` grouping option also carries `group_by` and `order_by` to
   apply with `where` for one unclear term. With several, each option names its
   `term` and `replaces` IDs: in `best.query_ir`, remove those IDs from `group_by`
   and their `order_by` entries, add the chosen `id`, keep `group_by` sorted, then validate.
   When two terms could replace the same grouping, plan offers no options; ask the user.

Use `minimal` or `compact` verbosity unless the user asks for debugging detail. Request
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
nodes instead of being retried as raw SQL. `AMBIGUOUS_CHILD_SCOPE` routes to a clarification
node: each of its `details.clarification.options` is a complete `where` to resend.
A `PLAN_UNMATCHED_TERMS` grouping option also carries `group_by` and `order_by` to apply
with `where` for one unclear term. With several, in `best.query_ir` remove each chosen
option's `replaces` IDs from `group_by` and their `order_by` entries, add its `id`,
keep `group_by` sorted, then validate. When two terms could replace the same grouping,
plan offers no options; ask the user. `AMBIGUOUS_PATH` (`details.reason:
route_decision_required`) means two join routes can answer the question differently (an account's
branch district or its owner's home district) and the package hasn't recorded which one it means.
The agent never picks one; it asks:

1. Refusal: `details.clarification.question` ("Which District does the question mean for an
   Account?") and one option per route, each with a `meaning` in business words.
2. Ask the person, reading each option's `meaning`.
3. Resend the same query with the chosen option's `decision` in
   [`route_decisions`](QUERY_IR_SCHEMA.md#route_decisions). The answer is for this person and this
   query only, and carries an `info` note `ROUTE_CHOSEN_BY_QUERY` with the row and `replaced` (how
   the package resolves the pair without it: `undecided` when it refuses). State the meaning
   used (`details.meaning`) and mention the `label` of each ready decision row in
   `details.route_alternatives` as a one-step switch: resend with that row in
   `route_decisions`. Only an undecided pair gets these alternatives, taken from the
   package's refusal options and excluding routes through hidden objects. At most three
   are listed; `details.more_alternatives` counts any remaining visible alternatives.
   To see every option, validate the query without `route_decisions`; that does not run
   a warehouse query. The warehouse executes only the chosen route.
4. To make it the default for everyone, a maintainer calls Architect
   [`record_route_decision`](ARCHITECT_MCP.md) with the same `decision`. That is a reviewed package
   change; from then on the question answers without asking. An option with `conflicts_with`
   names the package rows to change first; its `decision` still answers per query.

One row also decides every route that walks its pair. An `info` note `ROUTE_COLOCATED_KEY` or
`ROUTE_RECORDED` (compact and full responses) names the route the answer used, the start entity's
own key or the package's recorded routes; it needs no follow-up, and `ROUTE_COLOCATED_KEY` lists
the row that would make each other route the default (or, in `details.conflicts_with`, the rows
that row would disagree with). `PATH_NOT_FOUND` with `details.reason:
excluded_by_decision` means the package's rows rule out every route; it is a package fix, not a
query fix.

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
