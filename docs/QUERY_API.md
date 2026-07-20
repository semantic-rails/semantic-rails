# Query API

This guide covers the active CLI, HTTP routes, query AST, and response shapes.

The API is served by the public `semantic-rails` runtime and CLI.

> **Machine-readable Query IR contract:** [`schemas/query_ir.v1.json`](../schemas/query_ir.v1.json) (Draft 2020-12). Narrative reference and worked examples per expression position: [QUERY_IR_SCHEMA.md](QUERY_IR_SCHEMA.md).

The stable HTTP contract is served under `/api/v1/*`.

## Package Loading

The query API runs against the normalized runtime package contract. All packages use
`schema_version: 1`.

Directory packages are loaded from a package root containing `package.yml`. The loader merges:

- `package.yml`
- optional top-level `defaults.yml`, `graph.yml`, `metrics.yml`, `policies.yml`, and `caveats.yml`
- every YAML file under `models/**`
- every YAML file under `metrics/**`

Use [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md) for the authoring contract. The query payload shape below is unchanged by how the package is split across files.

## CLI

```bash
uv run semantic-rails packages
uv run semantic-rails catalog --package jaffle_shop
uv run semantic-rails catalog --package jaffle_shop --verbosity compact
uv run semantic-rails discover --package jaffle_shop --terms drinks --stage initial
uv run semantic-rails inspect --package jaffle_shop --object-id measure.jaffle.order_count --verbosity compact
uv run semantic-rails build-options --package jaffle_shop --query-json '@examples/jaffle_shop_revenue_by_store.json' --focus-terms "orders by store" --step group_by
uv run semantic-rails valid-values --package jaffle_shop --dimension dimension.jaffle_store_name
uv run semantic-rails plan --package jaffle_shop --intent "new customer orders over time"
uv run semantic-rails validate --package jaffle_shop --query-json '@examples/jaffle_shop_revenue_by_store.json'
uv run semantic-rails compile --package jaffle_shop --query-json '@examples/jaffle_shop_revenue_by_store.json'
uv run semantic-rails query --package jaffle_shop --query-json '@examples/jaffle_shop_revenue_by_store.json' --verbosity minimal --sql-profile off
uv run semantic-rails check --package jaffle_shop
uv run semantic-rails build-package --package jaffle_shop --output dist/jaffle_shop.semantic-rails.tar.gz
uv run semantic-rails run-examples --package jaffle_shop
uv run semantic-rails test-package --package jaffle_shop
uv run semantic-rails impact-report --package jaffle_shop --base-ref main
uv run semantic-rails doctor --package jaffle_shop
uv run semantic-rails init my_semantic_package
uv run semantic-rails project validate --path ./my_semantic_package
uv run semantic-rails serve --package jaffle_shop --port 8081
uv run semantic-rails mcp stdio --package jaffle_shop
uv run semantic-rails mcp http --package jaffle_shop --host 127.0.0.1 --port 8091
```

## HTTP Routes

Preferred stable routes:

- `GET /api/v1/health`
- `GET /api/v1/ready`
- `GET /api/v1/capabilities`
- `GET /api/v1/catalog`
- `POST /api/v1/catalog`
- `POST /api/v1/discover`
- `POST /api/v1/inspect`
- `POST /api/v1/build-options`
- `POST /api/v1/valid-values`
- `POST /api/v1/plan`
- `POST /api/v1/validate`
- `POST /api/v1/compile`
- `POST /api/v1/query`
- `POST /api/v1/segment-validate`
- `POST /api/v1/segment-explain`
- `POST /api/v1/segment-preview`

Root paths and unversioned `/api/*` paths are not public routes; use `/api/v1/*`.

## HTTP Envelope

All JSON HTTP responses include the operation payload plus additive contract fields:

- `ok`
- `status`
- `api_version`
- `request_id`
- `package_id`
- `warnings`
- `errors`
- `recovery_hints`
- `timing_ms`

Clients may send `X-Request-ID`, `X-Correlation-ID`, a `request_id` query parameter, or a
top-level JSON `request_id`. The server echoes the selected value in the response body and
the `X-Request-ID` response header. If none is provided, the server generates one.

JSON responses are emitted compact by default. Append `?pretty=true` to any HTTP route to get
indented output for ad-hoc inspection. Compact-by-default keeps large `/api/v1/catalog` and
`/api/v1/discover` payloads bandwidth-cheap for hosted deployments.

`GET /api/v1/health` returns service, package, schema, and warehouse metadata without warehouse I/O.
`GET /api/v1/ready` returns package-load readiness and can run a protected warehouse check when
`X-Semantic-Check-Warehouse: true` is sent.
`GET /api/v1/capabilities` returns the public v1 route index (append-only from 0.1.0 on:
route removals or breaking request/response changes require a new `/api/v2` prefix, even
before 1.0), supported API versions,
route alias prefixes, warehouse capabilities, package semantic capabilities, and
`expression_shapes` — a structured list of the accepted `select[*].expression`
shapes (`aggregate`, `metric`, `prior_period`, `rolling`, `cumulative`, `ratio`,
`conversion`, `distribution`, `aggregate_if`, `between`). Each entry is a
`{name, description, example}` dict so agents can introspect the IR contract at
runtime without parsing `schemas/query_ir.v1.json` out of band.

## Request Context And API Key Shim

The runtime accepts trusted request context from headers and merges it into `policy_context` for
metadata, validate, compile, and query calls. Header context wins over body-provided
context when both are present:

- `X-Request-ID` or `X-Correlation-ID`
- `X-Semantic-Actor`
- `X-Semantic-Tenant`
- `X-Semantic-Project`
- `X-Semantic-Roles`
- `X-Semantic-Environment`
- `X-Semantic-Audience`

Responses include additive `request_context` when context is present. This is a runtime shim, not
hosted enterprise auth.

### Trusted Upstream Context

`policy_context.audience`, `policy_context.environment`, `policy_context.tenant`, and
`policy_context.roles` are **trusted upstream context**. The policy engine matches policies
against these fields (for example, a policy with `audiences: [finance]` only fires when the
resolved context carries `audience=finance`). The default runtime reads them from request
headers (and falls back to the body's `policy_context` block).

For local development, single-tenant on-prem, and any deployment behind a trusted edge,
the header-based default is correct. **For multi-tenant hosted deployments it is unsafe** —
any caller can self-assert `audience: "finance"` and bypass MNPI-style policy gating.

Hosted operators must replace the resolver with one that derives these fields from an
authenticated identity (JWT, mTLS cert, signed session, etc.) before reaching the runtime:

```python
from semantic_rails.request_context import (
    PolicyContextResolver,
    RequestContext,
    set_policy_context_resolver,
)

class IdentityDerivedResolver:
    def resolve(self, headers, *, payload=None, request_id=""):
        identity = verify_jwt(headers.get("Authorization", ""))
        return RequestContext(
            request_id=request_id,
            actor=identity.subject,
            tenant=identity.tenant_id,        # trust comes from the token, not headers
            audience=identity.audience,        # ditto
            environment=identity.environment,
            roles=tuple(identity.roles),
        )

set_policy_context_resolver(IdentityDerivedResolver())
```

The Query IR, policy engine, and HTTP envelope do not change — the resolver is the only
integration seam. The default `HeaderPolicyContextResolver` remains the OSS behavior so a
single-user local install needs no additional configuration.

### Raw SQL In Error Envelopes

`QUERY_EXECUTION_ERROR` envelopes redact the rendered SQL by default (sha256 + outline +
char count). Raw SQL is only included in `details.sql` when **all three** of these hold:

1. The request payload sets `debug: true`.
2. The operator sets the env var `SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1`.
3. The resolved request context carries the `debug` role.

The env var (#2) is the operator opt-in: with the OSS default resolver, any client can
self-assert `roles: ["debug"]` in the body, so role membership alone is not sufficient
authority. Operators flip the env var only after replacing the resolver with one bound to
authenticated identity.

Authentication is disabled by default. Set `SEMANTIC_RAILS_API_KEYS` or
`SEMANTIC_RAILS_API_KEY_FILE` to require `Authorization: Bearer ...`, `X-API-Key`, or
`X-Semantic-API-Key` on protected routes. `GET /api/v1/health` and lightweight
`GET /api/v1/ready` remain public; deep warehouse readiness requires auth when API keys are
configured.

Local/customer-side runtime supports `/api/v1/query`. A future hosted v0 service should expose
compile/validate/discovery first and keep warehouse execution customer-side until execution
is explicitly productized.

The endpoint sections below use unprefixed route names as shorthand; production clients should
prefer the `/api/v1` prefix.

## Guided Discovery Flow

The preferred human-like query-building cascade is:

1. `discover`
   Start with a business term like `orders`, `drinks`, or `inventory`.
2. `inspect`
   Open the most relevant measure, metric, dimension, or entity card.
3. `plan` or `build-options`
   Use `plan` for a natural-language intent; use `build-options` for the next legal choices in an interactive builder. In MCP QA loops, `plan(detail: "query")` is the compact planning shape.
4. `valid-values`
   Page or search categorical values for a selected dimension.
5. `validate`
   Check the Query IR and get repair hints, output columns, and performance risk.
6. `compile`
   Generate SQL without execution. The response includes an `explain` payload with
   chosen relationship paths, candidates, contracts, and the full plan.
7. `query`
   Execute once the query is assembled.

`plan` is the agent planning entrypoint. It returns `status`, `intent_ir`, and a validated best Query IR draft by default. In MCP, use `detail: "query"` when the next step is direct execution; it keeps the same planning and validation behavior but omits trace, alternatives, `next`, and compose hints. For advanced runtime-composition requests, drafts may use Query IR nodes such as `ratio`, `scoped_aggregate`, `aggregate_if`, `metric_predicate`, `entity_value`, `distribution`, `between`, and `arithmetic`.

## Compile-Only SQL

`POST /compile` accepts the same payload as `validate` and `query`, but never executes SQL. It returns:

- `rendered_sql`
- `sql_profile`
- `warehouse`
- `dialect`
- `warehouse_capabilities`
- `output_columns`
- `physical_plan` (top-level at `verbosity: "full"`; the `compact` HTTP default keeps it nested under `explain`)
- `performance_plan` (same gating as `physical_plan`)

When package authors declare physical rollups with `model.variants:` or explicit
`aggregate_relations:`, compile output also exposes routing decisions:

- `MeasurePlan.aggregate_relation_id` is set when a measure leaf was routed.
- `logical_plan.measure_plans[].rewrite_strategy` is `aggregate_relation` for
  routed measure leaves.
- `physical_plan.nodes[].details.selected_relation_type` is `aggregate_relation`
  or `raw`.
- `physical_plan.nodes[].details.aggregate_relation_id` names the selected
  rollup relation when routing succeeds.
- `performance_plan.aggregate_routing.selected` lists the aggregate relation IDs
  used by the compiled query.

Routing is exact-first. If the rollup misses a grouped or filtered dimension,
uses a non-default source, has non-exact equivalence, lacks the requested time
grain, or cannot serve the requested measure aggregation, the compiler falls
back to the raw relation.

`validate`, `compile`, and `query` accept a `verbosity` field (`minimal` | `compact` |
`full`). The HTTP default is `compact`. The MCP adapter defaults the same three tools to
`minimal` instead, which keeps only `{ok, status, errors, warnings, recovery_hints}` plus
`rendered_sql` on `compile` and `rows` + `row_count` on `execute` — see
[MCP_INTERFACE.md](MCP_INTERFACE.md). An explicit `verbosity` always wins on both surfaces.

`sql_profile` currently defaults to `audit`. Callers may pass `sql_profile: "compact"` or `sql_profile: "debug"`; unsupported profile-specific rewrites fall back to audit-safe SQL while preserving the requested profile in metadata.

`output_columns` maps result fields back to semantic metadata:

```json
{
  "field": "revenue",
  "semantic_id": "metric.demo.revenue",
  "display_label": "Revenue",
  "sql_alias": "revenue",
  "type": "currency",
  "lineage": ["metric.demo.revenue"]
}
```

## Query Shape

All runtime queries use stable IDs.

`select` is optional when the query is a distinct-values query driven by `group_by` and/or `time`.
At least one of `select`, `group_by`, or `time` must be present.

```json
{
  "version": 1,
  "select": [
    {
      "expression": {
        "measure": "measure.jaffle.order_count"
      },
      "as": "orders"
    },
    {
      "expression": {
        "metric": "metric.sales.aov_usd"
      },
      "as": "aov_usd"
    }
  ],
  "group_by": ["dimension.jaffle_store_name"],
  "where": [
    {
      "field": "dimension.jaffle_order_ordered_at",
      "op": ">=",
      "value": "2016-09-01 00:00:00"
    }
  ],
  "metric_filters": [
    {
      "expression": {
        "measure": "measure.jaffle.order_count"
      },
      "op": ">",
      "value": 0
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2016-09-01 00:00:00",
    "end": "2016-12-01 00:00:00",
    "fill": false,
    "calendar_id": "default"
  },
  "temporal_role_overrides": {},
  "path_policy": {
    "preference": "fewest_hops",
    "ask_if_ambiguous": true
  },
  "order_by": [
    {
      "field": "orders",
      "direction": "DESC"
    }
  ],
  "limit": 25,
  "policy_context": {
    "environment": "production",
    "audience": "finance",
    "roles": ["sales"]
  },
  "debug": false,
  "explain": false
}
```

`time.start` is **inclusive** (`>=`) and `time.end` is **exclusive** (`<`) — the window is
`[start, end)`. The example above covers September through November 2016; to include December,
set `end` to `"2017-01-01 00:00:00"`. Half-open bounds let adjacent windows compose without
overlap. See [QUERY_IR_SCHEMA.md](QUERY_IR_SCHEMA.md#timeblock) for the full `TimeBlock`
contract, including the object-only `range.last` relative window
(`unit` ∈ `day | week | month | quarter | year`).

`policy_context` is optional and scopes visibility, access, and metric-constraint policies for metadata, validation, and query routes.

### Request `limits` block (optional)

The query envelope accepts an optional `limits` block with per-request
resource hints:

```json
{
  "version": 1,
  "select": [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}],
  "limits": {
    "statement_timeout_ms": 30000,
    "max_rows": 10000
  }
}
```

- `statement_timeout_ms` — DuckDB enforces the requested millisecond deadline
  with a per-query watchdog that calls `Connection.interrupt()` and drains the
  watchdog before the shared connection is reused. Warehouse-native adapters
  with second-granularity controls round up to the next second.
- `max_rows` — DB-API, DuckDB, and native Snowflake cursors fetch at most
  `max_rows + 1`, return at most `max_rows`, and set `truncated=true` when an
  additional row exists. Adapters without bounded cursor reads still apply the
  cap after materialization.

Unrecognized keys are ignored. Invalid values (non-int, negative) are
treated as unset. This is scaffolding for hosted operators to enforce
per-tenant policies without forking; the OSS default has no limits.

`order_by.field` can reference:

- a projected alias like `orders`
- a grouped dimension ID like `dimension.jaffle_store_name`
- the stable reserved alias `time` whenever the query has a time axis

Output column names must be unique across:

- `select[*].as`
- grouped dimension IDs
- the projected time-axis alias such as `temporal_role.jaffle_order_time__month`

Duplicate output names fail validation with `DUPLICATE_OUTPUT_ALIAS`.

Distinct-values examples:

```json
{
  "version": 1,
  "group_by": ["dimension.jaffle_store_name"],
  "order_by": [{ "field": "dimension.jaffle_store_name", "direction": "ASC" }],
  "limit": 25
}
```

```json
{
  "version": 1,
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2017-01-01 00:00:00",
    "end": "2017-04-01 00:00:00"
  },
  "order_by": [{ "field": "time", "direction": "ASC" }]
}
```

## Expression Nodes

### Query-Time Expression Nodes

Supported in `select` and `metric_filters.expression`:

- measure reference

```json
{ "measure": "measure.jaffle.order_count", "aggregation": "count_distinct" }
```

- metric reference

```json
{ "metric": "metric.sales.aov_usd" }
```

- arithmetic

```json
{
  "kind": "arithmetic",
  "op": "divide",
  "left": { "measure": "measure.jaffle.revenue_usd" },
  "right": { "measure": "measure.jaffle.order_count" }
}
```

- aggregate parameters

```json
{
  "measure": "measure.jaffle.item_revenue_usd",
  "aggregation": "percentile",
  "parameters": { "p": 0.95 }
}
```

- cumulative

```json
{
  "kind": "cumulative",
  "input": { "measure": "measure.jaffle.order_count" },
  "partition_by": []
}
```

- rolling

```json
{
  "kind": "rolling",
  "input": { "measure": "measure.jaffle.revenue_usd" },
  "window": { "unit": "day", "value": 7 },
  "partition_by": []
}
```

- prior period

```json
{
  "kind": "prior_period",
  "input": { "measure": "measure.jaffle.revenue_usd" },
  "offset": { "unit": "month", "value": 1 }
}
```

- period to date

```json
{
  "kind": "period_to_date",
  "input": { "measure": "measure.jaffle.revenue_usd" },
  "period": "month",
  "partition_by": []
}
```

- metric predicate

Supported in metric definitions and in `metric_filters.expression`. Predicate expressions are not valid as projected result columns.

```json
{
  "kind": "metric_predicate",
  "entity": "entity.jaffle_customer",
  "scope_mode": "contextual",
  "input": { "measure": "measure.jaffle.order_count" },
  "op": ">",
  "value": 10
}
```

`metric_predicate` rules:

- query-time predicates default to `scope_mode: "contextual"` when omitted
- package-authored predicates must declare `scope_mode`
- supported scope modes are `contextual` and `entity_only`
- contextual predicates inherit the outer time bucket and compatible grouped context entities
- outer `where` filters are inherited when they are compatible, but they do not widen the scoped join key
- optional `time_grain` is only valid for contextual predicates and must be a coarser deterministic ancestor of the outer query grain on the same calendar
- contextual predicates should omit `time_alignment`; they inherit the query period by default
- public `time_alignment` values are `query_window` and `rolling_window_in_period`, and only for `entity_only` bounded-window predicates

Monthly contextual example:

```json
{
  "kind": "metric_predicate",
  "entity": "entity.jaffle_customer",
  "scope_mode": "contextual",
  "input": { "measure": "measure.jaffle.order_count" },
  "op": ">",
  "value": 10
}
```

Runtime-composed qualified rollup example:

```json
{
  "version": 1,
  "select": [{
    "as": "revenue_usd_from_qualified_customers",
    "expression": {
      "kind": "scoped_aggregate",
      "measure": "measure.jaffle.revenue_usd",
      "aggregation": "sum",
      "predicates": [{
        "measure": "measure.jaffle.order_count",
        "entity": "entity.jaffle_customer",
        "op": ">=",
        "value": 10
      }]
    }
  }],
  "group_by": ["dimension.jaffle_store_name"],
  "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}
}
```

Daily outer grain with monthly predicate grain:

```json
{
  "kind": "metric_predicate",
  "entity": "entity.jaffle_customer",
  "scope_mode": "contextual",
  "time_grain": "month",
  "input": { "measure": "measure.jaffle.order_count" },
  "op": ">",
  "value": 10
}
```

Entity-only lifetime example:

```json
{
  "kind": "metric_predicate",
  "entity": "entity.jaffle_customer",
  "scope_mode": "entity_only",
  "input": { "measure": "measure.jaffle.lifetime_order_count" },
  "op": ">",
  "value": 1
}
```

- conversion

```json
{
  "kind": "conversion",
  "entity": "entity.jaffle_customer",
  "window": { "unit": "day", "value": 7 },
  "matching_mode": "first_converted_after_base",
  "base": { "measure": "measure.jaffle.session_starts" },
  "converted": { "measure": "measure.jaffle.order_count" }
}
```

Current limit:

- enriched conversion expressions are executable for the supported event-count model
- unsupported conversion shapes still reject explicitly with `CONVERSION_NOT_SUPPORTED` rather than silently degrading into ratio math

### Config-Time Measure Expression Nodes

Supported in measure `expr`:

- `column`
- `literal`
- `arithmetic`
- `comparison`
- `boolean`
- `call`
- `case`

## Endpoint Contracts

### `POST /api/v1/discover`

Request:

```json
{
  "terms": "drinks",
  "kinds": ["measure", "metric", "dimension", "dimension_value"],
  "stage": "initial",
  "verbosity": "compact",
  "query": {},
  "limit": 5
}
```

Response groups:

- `measures`
- `metrics`
- `dimensions`
- `dimension_values`
- `entities`
- `blocked`

Each result includes:

- `id`
- `object_type`
- `name`
- `label`
- `description`
- `score`
- `match_reasons` — may include a `cross-entity ...` entry when the
  candidate's `root_entity` disagrees with the entity inferred from the
  top measure/metric candidates; blind agents reading `dimensions[0]`
  should branch on this rather than trusting raw rank
- `available`
- `blocked_reason`
- `root_entity`
- `default_temporal_role`
- `recommended_next_actions`
- `starter_query_patch` — runnable IR seed attached to every measure,
  metric, and dimension candidate. Measures/metrics seed `select`;
  dimensions seed `group_by`. Removes the discover → inspect round-trip
  blind agents otherwise need just to copy the patch shape.

Supported request controls:

- `stage`
- `verbosity`

### `POST /api/v1/inspect`

Request:

```json
{
  "object_id": "measure.jaffle.order_count"
}
```

Response keys:

- `ok`
- `object_id`
- `query_state`
- `card`

Measure cards expose aggregation guidance, default time behavior, recommended dimensions, and related curated metrics. Dimension cards expose categorical semantics, preferred filter operators, and sample values when available.
History-backed cards can also expose:

- `coverage_notes`
- `null_bucket_meaning`

Packages that opt into `defaults.operational` can also expose a nested `operational` block on measure and metric cards.

Useful card fields for measures:

- `usage_summary`
- `recommended_dimensions`
- `recommended_filters`
- `sample_questions`
- `comparison_family`
- `comparison_peers`
- `clock_variants`
- `preferred_companion_metrics`
- `starter_query_patches` — list of `{kind, query_patch [, note]}` entries.
  Always includes `select` (and `group_by` when a recommended dimension
  exists, `time` when a default temporal role exists). May also include:
  - `order_by` — when the card has a `default_temporal_role`; seeds
    `[{field: "time", direction: "DESC"}]`.
  - `where` — when a recommended dimension has a declared `value_domain`;
    seeds a sample-value filter using `{field, op, value}`. The `note`
    field flags the value as scaffolding.
  - `metric_filters` — only on predicate-backed metric cards; seeds the
    `{kind: "metric_predicate", metric, op, value}` shape. The `note`
    field flags the op/value as scaffolding to replace before executing.
- `policy_effects`
- `policy_visibility`

### `POST /api/v1/build-options`

Request:

```json
{
  "query": {
    "version": 1,
    "select": [
      {
        "expression": {
          "measure": "measure.jaffle.order_count",
          "aggregation": "count_distinct"
        },
        "as": "orders"
      }
    ]
  },
  "focus_terms": "orders by store",
  "focus_object_id": "measure.jaffle.order_count",
  "step": "group_by",
  "verbosity": "compact",
  "limit": 10
}
```

Response keys:

- `ok`
- `root_entity`
- `builder_step` — the single next step inferred from the partial query
- `next_legal_steps` — ordered list of remaining builder steps from
  the current partial query (e.g. `["group_by", "filter_dimension",
  "time", "review"]`). `review` is always the tail. Lets agents see the
  full remaining path, not just the next single step.
- `stage`
- `verbosity`
- `query_state`
- `selection`
- `focus_object_id`
- `focus_terms`
- `recommended`
- `available`
- `blocked`
- `query_patches`

Each builder row also includes:

- `why_recommended` — derived from the first match reason on the
  underlying discover row (focus-terms case) or from the candidate's
  topic + `review_priority` (empty-query case). Always carries signal;
  the empty-query path no longer falls back to `"default ordering"`.
- `result_shape_preview`
- `policy_effects`

`build-options` is the guided-builder endpoint for ranked next-step recommendations.

Supported builder steps:

- `measure`
- `aggregation`
- `group_by`
- `filter_dimension`
- `filter_value`
- `time`
- `review`

Compact builder results include a starter `query_patch` scaffold so callers do not need to learn the full AST before taking the next step.

### `GET` or `POST /api/v1/catalog`

Response keys:

- `ok`
- `package_id`
- `catalog.entities`
- `catalog.dimensions`
- `catalog.measures`
- `catalog.temporal_roles`
- `catalog.relationships`
- `catalog.metrics`
- `catalog.capabilities`
- `catalog.unsupported_capabilities`

Optional `POST /catalog` inputs:

- `view`
- `verbosity`
- `kind`
- `search`
- `entity`
- `policy_context`

When `verbosity=full`, measure and metric rows can include a top-level `operational` block and the same fields inside `payload.operational` for packages that declare operational metadata.

### `POST /api/v1/valid-values`

Request fields:

- `dimension_id`
- `query`
- `search`
- `limit`
- `offset`
- `include_counts`
- `allow_live_query`
- `policy_context`

Response keys:

- `ok`
- `dimension`
- `values`
- `source`
- `query_state`
- `selection`
- `value_domain_id`
- `provenance`
- `total_count`
- `has_more`
- `selection_context`
- `value_source_type`
- `estimated_cost`
- `anchor_measure`

`source` is typically `value_domain` or `duckdb`.

### `POST /api/v1/plan`

Request:

```json
{
  "intent": "monthly revenue from customers with at least 10 orders by store",
  "detail": "best",
  "limit": 3
}
```

`detail` defaults to `best`. Use `detail: "full"` when the agent needs
alternatives and blocked drafts, and `detail: "debug"` when it also needs
`compose_hints`.

Response keys:

- `ok`
- `plan_version`
- `intent`
- `intent_ir`
- `status` (`ok`, `low_confidence`, `unrealizable`, or `out_of_scope`)
- `best` (`null` unless a draft exists)
- `next`
- `why` when `status != "ok"`
- `alternatives` and `blocked` when `detail` is `full` or `debug`
- `compose_hints` when `detail` is `debug`

`detail="query"` is an MCP-oriented compact projection. It preserves
`plan_version`, `intent`, `status`, `why`, `tie_break_hints`, and a compact
`best` containing `pattern`, `query_ir`, `resolved`, `validation_ok`, and
`subject_ids_used`.

For `status="out_of_scope"`, `why.code` distinguishes classifier
refusals (`OUT_OF_SCOPE`) from relevance-floor refusals
(`LOW_RELEVANCE`).

For `status="low_confidence"`, inspect `why` before execution. In particular,
`PLAN_FALLBACK_SEMANTIC_DRIFT` means a fallback draft validated but changed or dropped requested
intent slots such as target, grouping, qualification, filters, or time scope; the runtime keeps the
closest primary draft instead of silently returning a semantically different answer.

`best.query_ir` is the canonical Query IR for the selected draft. When
`status="ok"`, `plan` has already called `validate`, which pays the
compile cost and warms the runtime compile cache. Agents can forward
`best.query_ir` to `compile` or `query`; call `validate` again only when
they need the full diagnostics envelope or are editing the IR by hand.

Every `best` entry may include a compact `trace` showing extracted intent slots, selected
subjects/groupings/filters/paths, and whether a fallback was used. Treat it as diagnostic context,
not as a separate workflow or server-side trace store.

`plan` preserves any caller-supplied partial Query IR fields. Additive
fields such as `select`, `where`, `metric_filters`, `order_by`, and
`group_by` keep the caller entries first, then append generated entries
when needed.

Qualified metric asks return `interpreted_intent.pattern: "qualified_metric_rollup"` and a validated runtime-composed `scoped_aggregate`. Contextual predicates omit `time_alignment`; `time_grain` appears only when the qualification grain differs from the output grain, such as daily output qualified by monthly customer activity.

### `POST /api/v1/validate`

Response keys:

- `ok`
- `version`
- `status`
- `errors`
- `warnings`
- `recovery_hints`
- `assumptions`
- `policy_effects`
- `provenance_summary`
- `disabled_options`
- `logical_plan`
- `explain`
- `query`
- `normalized_query`
- `timing_ms`

`validate` returns structured diagnostics. It does not need to throw on semantic failure.
History-backed queries emit compact warnings when null-preserving temporal-validity joins are in play.

### `POST /api/v1/query`

Response keys:

- `ok`
- `status`
- `rows`
- `row_count`
- `rendered_sql`
- `logical_plan`
- `sql_plan`
- `explain`
- `query`
- `normalized_query`
- `warnings`
- `errors`
- `recovery_hints`
- `assumptions`
- `policy_effects`
- `provenance_summary`

These are the `compact` (HTTP default) response keys. Through the MCP adapter the same
operation is the `execute` tool and defaults to `verbosity: "minimal"`, which keeps `rows`
and `row_count` but strips the plan/explain fields; pass `verbosity` explicitly to override.
MCP `execute` also accepts `row_format: "columns"` to return `columns: [...]` and
array rows instead of record objects. The default remains `row_format: "records"`.

### Segment Endpoints

Segments are package-authored entity sets. The supported HTTP routes mirror the CLI segment commands:

- `POST /api/v1/segment-validate`
- `POST /api/v1/segment-explain`
- `POST /api/v1/segment-preview`

Common request fields:

- `segment_id`
- `limit` for `segment-preview`

Common response fields:

- `ok`
- `segment`
- `normalized_segment`
- `derived_query`
- `status`
- `errors`
- `warnings`
- `rendered_sql` for explain and preview routes
- `rows`, `preview_row_count`, and `member_count` for preview routes

## Error Codes

Canonical public error codes:

- `AMBIGUOUS_ALIAS`
- `AMBIGUOUS_PATH`
- `UNSUPPORTED_AGGREGATION`
- `INVALID_TEMPORAL_ROLE`
- `FANOUT_UNSAFE`
- `MIXED_GRAIN_INVALID`
- `NO_VALID_VALUES_SOURCE`
- `POLICY_DENIED` — object access or metric-constraint policy blocked the query.
- `REWRITE_NOT_SUPPORTED`
- `INVALID_EXPRESSION_AST` — ships a `USE_OBJECT_SHAPE` recovery hint in
  `details.recovery_hints` when the failure is a window/offset payload
  that was passed as an int instead of a `{unit, value}` object.
- `WINDOWED_TIME_FILTER_UNSUPPORTED` — ships two ordered recovery hints
  at the top-level `recovery_hints` field: `drop_time_start` (always-safe
  fallback, listed first) and `widen_time_window` (computed
  `suggested_start`; agents should widen by lookback + one full bucket
  when the metric is at a discrete grain).

### Warning Codes

The response `warnings` array can carry these non-error signals:

- `UNGRAINED_TIME_PROJECTION` — fires when `time.temporal_role` is set
  without `time.grain` AND the query has no `group_by` AND no inline
  window expression (prior_period / rolling / cumulative /
  period_to_date) carries its own grain. The planner will group by the
  raw timestamp column and return one row per distinct value;
  `details.recovery_hints[0]` (`SET_TIME_GRAIN`) recommends either
  adding `time.grain` (to bucket) or dropping `time.temporal_role` (for
  a scalar over `[start, end]`).
- `EXPRESSION_NORMALIZED_AWAY` — fires when an input expression `kind`
  was recognized by the parser but did not survive normalization (or
  the user's `as:` alias is missing from compiled output). Carries
  `details.dropped_expression` and `details.compiled_kind` so the agent
  can decide whether to retry with a different shape.
- `SEMANTIC_CAVEAT_APPLIED` — fires when a package-authored caveat is
  relevant to the compiled query shape and time window. Caveats are
  advisory interpretation context: they do not alter SQL, rows, access,
  discovery, or policy behavior. Severity `info` adds definitional
  framing to repeat when narrating results; severity `warning` means the
  matched window or slice itself is affected and comparisons built on it
  can mislead. Agents should mention caveats only when explaining
  affected results.
- `SEMANTIC_CAVEATS_TRUNCATED` — fires when more caveats matched than
  the current verbosity returns. Increase `verbosity` to inspect the
  omitted advisory context.

HTTP failures return:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_TEMPORAL_ROLE",
    "message": "Unknown temporal role 't.does_not_exist'",
    "details": {}
  }
}
```

## Real Example

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "version": 1,
      "select": [
        { "expression": { "measure": "measure.jaffle.order_count" }, "as": "orders" },
        { "expression": { "metric": "metric.sales.aov_usd" }, "as": "aov_usd" }
      ],
      "group_by": ["dimension.jaffle_store_name"],
      "time": { "temporal_role": "temporal_role.jaffle_order_time", "grain": "month" },
      "limit": 5
    }
  }'
```
