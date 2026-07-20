# Agent API Path

Semantic Rails is designed for human-defined semantics and agent-first query building. Humans own
entities, measures, metrics, policies, examples, and tests in the package. Agents use the runtime
API to discover that governed surface, assemble Query IR, validate it, and compile SQL before
local/customer-side execution.

Start the active package:

```bash
uv run semantic-rails serve --package jaffle_shop --port 8081
```

Routes are available under stable `/api/v1/*` paths.

## Recommended Loop

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

## Plan Status And Detail

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

## Minimal Agent Calls

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

## Policy Context

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
