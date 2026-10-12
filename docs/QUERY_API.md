# Query API

This guide covers the active CLI, HTTP routes, the query call, and response shapes.

The API is served by the public `semantic-rails` runtime and CLI.

> **The query call:** [QUERY_CALL.md](QUERY_CALL.md) is the reference, with worked examples;
> [`schemas/query.v2.json`](../schemas/query.v2.json) (Draft 2020-12) is the machine-readable contract.

The stable HTTP contract is served under `/api/v1/*`.

## Package Loading

The query API runs against the normalized runtime package contract. All packages use
`schema_version: 1`.

Directory packages are loaded from a package root containing `package.yml`. The loader merges:

- `package.yml`
- optional top-level `defaults.yml`, `graph.yml`, `metrics.yml`, `policies.yml`, and `caveats.yml`
- every YAML file under `models/**`
- every YAML file under `metrics/**`

Use [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md) for the authoring contract. The query call is unchanged by how the package is split across files.

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
uv run semantic-rails serve --package jaffle_shop --host 127.0.0.1 --port 8091
```

## HTTP Routes

`serve` and ASGI also expose stateless Streamable HTTP at `/mcp`; see
[MCP_INTERFACE.md](MCP_INTERFACE.md). `/health` and `/api/v1/health` are public
health probes even when API keys are configured.

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
- `warnings`, `errors` and `recovery_hints`, each only when it has entries
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
`{name, description, example}` dict so agents can introspect the call at
runtime without reading `schemas/query.v2.json` out of band.
The capability list advertises `query_call_v2`. Python capability payloads and MCP
discovery/catalog resources expose the same capability list.

## Request Context And API Key Shim

The runtime resolves who is asking from the request's credentials, through the policy context
resolver, for metadata, validate, compile, and query calls. The default resolver reads these
headers; a request body or query string that says who is asking (`policy_context`, or `environment`
and `audience` query parameters) is refused with `IDENTITY_CLAIM_REFUSED`, never stripped:

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

The request context's `audience`, `environment`, `tenant`, and `roles` are **trusted upstream
context**. The policy engine matches policies
against these fields (for example, a policy with `audiences: [finance]` only fires when the
resolved context carries `audience=finance`). The default runtime reads them from request
headers only. Trusted attributes are reserved for an embedding host
([EMBEDDING.md](EMBEDDING.md)); a caller cannot send them.

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
    def resolve(self, headers, *, request_id=""):
        identity = verify_jwt(headers.get("Authorization", ""))
        return RequestContext(
            request_id=request_id,
            actor=identity.subject,
            tenant=identity.tenant_id,  # trust comes from the token, not headers
            audience=identity.audience,  # ditto
            environment=identity.environment,
            roles=tuple(identity.roles),
        )


set_policy_context_resolver(IdentityDerivedResolver())
```

The query call, policy engine, and HTTP envelope do not change — the resolver is the only
integration seam. The default `HeaderPolicyContextResolver` remains the OSS behavior so a
single-user local install needs no additional configuration.

### Restricted Metric Grants

An identity-derived resolver may additionally set `RequestContext.metric_allowlist`
and `dimension_allowlist` to tuples of canonical semantic IDs. `metric_allowlist=None`
retains unrestricted local behavior; an empty tuple denies every metric. With an
explicit metric allowlist, missing or empty dimension grants allow no caller-selected
dimensions. Dimension grants may contain dimension and temporal-role IDs. Neither
grant list is returned in the public `request_context` envelope. Roles and audience
continue to mean business policy attributes; hosts enforce operation permissions
separately before invoking the engine.

Restricted queries select explicit metric references, such as
`{"expression": "metric.sales.customer_count", "as": "customers"}`.
The allowed recipe can read its internal dependencies, subject to package access
policies. Callers cannot select those measures or columns independently. Grouping,
filters, and explicit time roles require corresponding dimension grants. Raw measures,
column/conditional aggregates, runtime expression composition, entity filters, `having`,
and dimension-only queries are unavailable in this bounded contract. Denial occurs before
compilation or warehouse access, including cache hits. Metric and dimension grants do not
authorize caller-selected entity scopes; an `any`/`none` entity filter is denied even when all
its condition dimensions are granted.

Catalog, discovery, inspection, and build options return a small projection containing
only visible metric/dimension/temporal-role IDs and labels. Restricted planning matches
explicit visible names, labels, or IDs, emits a direct metric reference, and can include
explicit granted dimensions. General pattern composition, segment operations, and
valid-value lookups are unavailable for restricted grants. Plans contain portable calls;
each subsequent request must independently resolve its trusted grants. Restricted
compile/query results retain SQL and result rows but omit package-wide diagnostics,
dependency descriptions, and related-object suggestions. Grant responses carry only
listed engine diagnostics (`NO_DATA_YET`, `NO_DATA_IN_SCOPE`, `VALUES_WITHHELD`, `FILTER_VALUE_NOT_FOUND`,
`FILTER_VALUE_UNVERIFIED`, `FILTERED_SERIES_BUCKETS_DROPPED`, and
`FILTERED_SERIES_BUCKETS_UNVERIFIED`) whose named objects are all granted. Bucket diagnostics
re-enter authorization and reveal no bucket keys when the source read is denied. Requests
cannot replace the resolved grants: a `policy_context` claim, top-level or nested, is refused over
HTTP and hosted MCP with `IDENTITY_CLAIM_REFUSED`.

`NO_DATA_YET` fires for guarded `NULL` series buckets beyond their visible coverage,
empty totals with coverage proven before the window, or no visible coverage.
`details.outputs` lists output aliases; `details.measures` lists `{id, edge, edge_source}`:
`edge` is the date of the measure's last visible row (`edge_source` `last_row`), and both
are null with no visible coverage. It replaces
`EMPTY_RESULT_WINDOW` and excludes covered outputs from `NO_DATA_IN_SCOPE`. It is granted
only when every named measure is granted.

Restricted responses reuse the ordinary catalog formatter and output-column builder.
Compact catalogs retain the 200-row cap per kind, `counts`, `counts_total`, and
`truncated` indicators; use `verbosity=full` or `search`/`kind` filters to retrieve the
remaining visible objects. Discovery respects `limit` and returns runnable metric
`starter_query_patch` values. Catalog/discovery do not offer cursor or offset paging;
query `limit` bounds rows, while the separately documented valid-values `offset` API
remains unavailable under restricted grants. Output descriptors retain `field`,
`semantic_id`, `display_label`, `sql_alias`, and `type`, including for empty results,
but omit lineage. MCP `row_format=columns` therefore retains its schema on zero rows.

For example, after discovering the granted Customer count metric and Customer type
dimension, `plan("Show me customer count by customer type")` produces a portable call
that can pass through validate, compile, and execute. Additional unsupported intent
clauses return an unrealizable plan. This is a bounded planning contract, not full
parity with the general expression and segment workflows.

The full metadata builders and planner cannot simply run before filtering their output:
they enumerate all package measures and recipes, emit related-object references and
diagnostic candidates, and may lower a named metric to an independently queryable
measure. Restricted operations therefore project candidates before planning and use
the shared formatter only after authorization. Compilation still uses the same engine
and immutable package; it is never performed against a truncated dependency graph.
Context-aware capabilities advertise the available metric-reference shape and omit
package connection, seed, and filesystem settings. Authorization errors remain opaque;
safe operational codes such as `QUERY_EXECUTION_ERROR` remain distinguishable without
returning warehouse or package-generated error details.

### Raw SQL In Error Envelopes

`QUERY_EXECUTION_ERROR` envelopes redact the rendered SQL by default (sha256 + outline +
char count). Raw SQL is only included in `details.sql` when **all three** of these hold:

1. The caller asks for it: `Runtime.query(call, debug=True)` (an embedding host or local
   caller; HTTP and MCP requests carry no debug option).
2. The operator sets the env var `SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1`.
3. The resolved request context carries the `debug` role.

The env var (#2) is the operator opt-in: with the OSS default resolver, any client can
self-assert the `debug` role in the `X-Semantic-Roles` header, so role membership alone is not
sufficient authority. Operators flip the env var only after replacing the resolver with one bound to
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
   Check the call and get repair hints, output columns, and performance risk.
6. `compile`
   Generate SQL without execution. The response includes an `explain` payload with
   chosen relationship paths, candidates, contracts, and the full plan.
7. `query`
   Execute once the query is assembled.

`plan` is the agent planning entrypoint. It returns `status`, `intent_ir`, and a validated best draft call (`best.query`) by default. `detail: "query"` (the MCP default) is for when the next step is direct execution; it keeps the same planning and validation behavior but omits trace, alternatives, `next`, and compose hints. For advanced runtime-composition requests, drafts may use inline kinds such as `ratio`, `scoped_aggregate`, `aggregate_if`, `metric_predicate`, `entity_value`, `distribution`, `between`, and `arithmetic`.

## Compile-Only SQL

`POST /compile` accepts the same body as `validate` and `query`, but never executes SQL. It returns:

- `rendered_sql`
- `sql_profile`
- `warehouse`
- `dialect`
- `warehouse_capabilities`
- `output_columns`
- `physical_plan` (top-level at `verbosity: "full"`; the `compact` HTTP default keeps it nested under `explain`)
- `performance_plan` (same gating as `physical_plan`)

When package authors declare physical rollups with `model.variants:`, compile
output also exposes routing decisions:

- `MeasurePlan.aggregate_relation_id` is set when a measure leaf was routed.
- `logical_plan.measure_plans[].rewrite_strategy` is `aggregate_relation` for
  routed measure leaves.
- `physical_plan.nodes[].details.selected_relation_type` is `aggregate_relation`
  or `raw`.
- `physical_plan.nodes[].details.aggregate_relation_id` names the selected
  rollup relation when routing succeeds.
- `performance_plan.aggregate_routing.selected` lists the aggregate relation IDs
  the compiled SQL reads, including branches compiled as separate queries.
- `logical_plan.measure_plans[].aggregate_relation_rejections` maps each rejected
  rollup of the leaf's entity to a reason code, such as `missing_dimension`,
  `non_nesting_grain`, `time_bounds_not_aligned`, `timezone_mismatch`,
  `calendar_mismatch`, `metric_predicate_filter`,
  `join_path_mismatch`, `aggregation_not_reaggregable`, `one_to_many_hop` (the
  leaf crosses a one-to-many hop, which a rollup would multiply) or
  `not_certified`. It is empty for a package without rollups.
- `performance_plan.aggregate_routing.candidates` lists every rollup considered
  for each measure leaf: `{leaf_id, measure_id, relation_id, decision, reason}`,
  where `decision` is `selected`, `eligible` (it passed every rule but wasn't
  read: another rollup ranked higher, or, with reason `lowered_separately`, no
  branch read it), `rejected` (with the reason code) or `unknown`. A leaf the plan
  lowers separately (a `distribution` compiles each branch as its own query)
  reports each rollup its planner didn't reject with reason `lowered_separately`:
  `unknown` when some branch reads it (the report can't say which leaf's), and
  `eligible` when none does. The list stops at 200 rows, and
  `candidates_omitted` counts any rows left out.

Routing is on by default. An operator turns it off with the environment variable
`SEMANTIC_RAILS_AGGREGATE_ROUTING=off` (read when the runtime starts; `on` or
`off`), and an embedding host with `runtime.set_aggregate_routing(False)`. Off,
every measure leaf runs on the base tables and each rollup is reported as
`aggregate_routing_off`. The compile cache keys on the switch, so it applies to
the next request even when the query's plan is cached.

Routing is exact-first. If the rollup misses a grouped or filtered dimension,
uses a non-default source, has non-exact equivalence, lacks the requested time
grain, has buckets that don't nest in the query's (a weekly rollup for a month
query), doesn't align with the query's `start` or `end`, can't match the time
role's zone conversion or the query's calendar, declares its own `filters`, or
cannot serve the requested measure aggregation (including a distinct count of
anything but the model's single-column row key), the compiler falls back to the
raw relation.

`validate`, `compile`, and `query` take a `verbosity` option (`minimal` | `compact` |
`full`; over HTTP `options.verbosity`). The HTTP default is `compact`. Over MCP they are one
tool, `execute` with `mode` `validate`, `sql` or `run`, which defaults to `minimal` instead: it
keeps only `{ok, status, errors, warnings}` (plus `error` on failure) and `rendered_sql` in mode
`sql` and `rows` + `row_count` in mode `run`, plus the answer fields that change what the
numbers mean (`window`, `clock`, `observation_scope`, `routes`, `assumptions`) — see
[MCP_INTERFACE.md](MCP_INTERFACE.md). Recovery hints live on each issue in `errors`.
An explicit `verbosity` argument overrides the default on both surfaces; inside the call it is
refused.

`sql_profile` (`options.sql_profile` over HTTP) defaults to `audit`. Callers may pass `compact` or `debug`; unsupported profile-specific rewrites fall back to audit-safe SQL while preserving the requested profile in metadata.

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

## The Call

`validate`, `compile` and `query` take a query call: the question, with nothing in it that says
who is asking or how to render the answer. [QUERY_CALL.md](QUERY_CALL.md) is the reference.
Over HTTP the body is the call with its options beside it:

```json
{
  "query": {
    "select": ["measure.jaffle.order_count", {"expression": "metric.sales.aov_usd", "as": "aov_usd"}],
    "group_by": ["dimension.jaffle_store_name"],
    "where": [{"field": "dimension.jaffle_store_name", "op": "not", "value": "Brooklyn"}],
    "having": [{"field": "order_count", "op": "gt", "value": 0}],
    "time": {"start": "2016-09-01", "end": "2016-12-01", "grain": "month"},
    "order_by": [{"field": "order_count", "direction": "desc"}],
    "limit": 25
  },
  "options": {"now": "2017-09-04T12:00:00Z", "verbosity": "compact"}
}
```

The body may also be the call itself. `options` takes `now`, `verbosity` and `sql_profile`;
any other key beside `query` and `options` is refused. Identity never travels in the body:
the resolved request context scopes visibility, access and metric-constraint policies, and a
`policy_context` anywhere in the body is refused with `IDENTITY_CLAIM_REFUSED`. `options.now` pins the
instant relative windows (`time.last`, `time.this`) resolve against; `plan` takes `options.now`
beside `intent` and its seed `query`.

- `time.start` is inclusive and `time.end` exclusive: the window is `[start, end)`. The
  example covers September through November 2016.
- A window with no `time.grain` is one total per `group_by` group, with no time column; the
  response says so in `assumptions` (`WINDOW_TOTAL`). With a grain, the
  time axis is the `period` column (an ISO date) and each row carries `period_partial`.
- A named window (`last`, `this`, or `start` with `end`) answers every period, except
  beside a `group_by` on a date or time dimension, on ClickHouse (no day spine), or on a clock
  whose zone is not a known time zone, where it keeps the rows with data and says why
  (`OBSERVED_PERIODS_ONLY`); see [Named and open windows](QUERY_CALL.md#named-and-open-windows).
- `order_by.field` names a select alias, a `group_by` dimension id, or `period`. Without
  `order_by`, a series returns its periods ascending, then its `group_by` dimensions in their
  stated order (also ascending), on every warehouse. A window total has no default order.
- Output column names (`select[*].as`, `group_by` ids, `period`) must be unique; duplicates
  fail validation with `DUPLICATE_OUTPUT_ALIAS`.
- `select` is optional when `group_by` is present: the call lists the distinct rows.

```json
{"group_by": ["dimension.jaffle_store_name"], "order_by": [{"field": "dimension.jaffle_store_name", "direction": "asc"}], "limit": 25}
```

### Row caps and timeouts

Python callers pass `limits={"statement_timeout_ms": ..., "max_rows": ...}` beside the call
(`Runtime.query(call, limits=...)`); over MCP the row cap is the `execute` tool's `max_rows`.
The call's own `limit` is the question's top N, not a cap.

- `statement_timeout_ms` — DuckDB enforces the requested millisecond deadline
  with a per-query watchdog that interrupts the cursor running that query (never
  the shared connection or another query) and drains the watchdog before the
  query returns. Warehouse-native adapters
  with second-granularity controls round up to the next second.
- `max_rows` — DuckDB, DuckLake and MotherDuck cap fetched rows at `max_rows + 1`.
  Parameter-free reads use a relation limit; a single parameterized DuckDB SELECT
  applies the cap inside SQL before eager materialization, preserving Unicode
  literals and identifiers with optional statement terminators and trailing comments.
  Other parameterized statements are capped after materialization. Other DB-API
  and native Snowflake cursors use bounded fetches where supported. They return at most `max_rows` and set
  `truncated=true` when an additional row exists. Adapters without bounded reads
  still apply the cap after materialization.

Unrecognized `limits` keys are ignored. Invalid values (non-int, negative) are
treated as unset. This is scaffolding for hosted operators to enforce
per-tenant policies without forking; the OSS default has no limits.

## Inline Expressions

A `select` item's `expression` is an id string or an inline kind; a reference anywhere inside
a kind is an id string too. The kinds and their keys are listed in
[Inline expression kinds](QUERY_CALL.md#inline-expression-kinds):

```json
{
  "select": [
    {"expression": {"kind": "measure", "measure": "measure.jaffle.item_revenue_usd", "aggregation": "percentile", "parameters": {"p": 0.95}}, "as": "p95_item_revenue"},
    {"expression": {"kind": "arithmetic", "op": "divide", "left": "measure.jaffle.revenue_usd", "right": "measure.jaffle.order_count"}, "as": "aov"}
  ],
  "group_by": ["dimension.jaffle_store_name"]
}
```

Windowed kinds need `time.grain`:

```json
{
  "select": [
    {"expression": {"kind": "cumulative", "input": "measure.jaffle.order_count"}, "as": "orders_to_date"},
    {"expression": {"kind": "rolling", "input": "measure.jaffle.revenue_usd", "window": {"unit": "month", "value": 3}}, "as": "revenue_3m"},
    {"expression": {"kind": "prior_period", "input": "measure.jaffle.revenue_usd", "offset": {"unit": "month", "value": 1}}, "as": "revenue_month_before"},
    {"expression": {"kind": "period_to_date", "input": "measure.jaffle.revenue_usd", "period": "year"}, "as": "revenue_ytd"}
  ],
  "time": {"grain": "month", "end": "2016-12-01"}
}
```

```jsonc
{"rows": [{"period": "2016-09-01", "period_partial": false, "orders_to_date": 1367, "revenue_3m": 18053.279999999995, "revenue_month_before": null, "revenue_ytd": 18053.279999999995},
          {"period": "2016-10-01", "period_partial": false, "orders_to_date": 3052, "revenue_3m": 39977.229999999996, "revenue_month_before": 18053.279999999995, "revenue_ytd": 39977.229999999996},
          {"period": "2016-11-01", "period_partial": false, "orders_to_date": 5332, "revenue_3m": 67894.04999999999, "revenue_month_before": 21923.949999999997, "revenue_ytd": 67894.04999999999}]}
```

They read periods before the ones they return, so a `time.start` (or a `where` on a date) that
would cut that lookback is refused; an `end` alone runs. See
[Windows that read earlier periods](QUERY_CALL.md#windows-that-read-earlier-periods).

### Predicates

A predicate keeps the measure's rows whose entity passes a test on one of its own aggregates.
In a call it is an entity filter in `where`
(`{"entity", "having": [{field, op, value}], "over"}`, see
[Entity filters](QUERY_CALL.md#entity-filters)) or a `metric_predicate` inside a
`scoped_aggregate`. Predicate expressions are not valid as projected result columns.

```json
{
  "select": [{
    "as": "revenue_usd_from_qualified_customers",
    "expression": {
      "kind": "scoped_aggregate",
      "measure": "measure.jaffle.revenue_usd",
      "predicates": [{"input": "measure.jaffle.order_count", "entity": "entity.jaffle_customer", "op": "gte", "value": 10, "over": "period"}]
    }
  }],
  "group_by": ["dimension.jaffle_store_name"],
  "time": {"grain": "month"}
}
```

Predicate rules:

- `over` is required: `all_time` (every row of the entity, ignoring the call's window),
  `window` (the entity's rows inside the call's window) or `period` (per entity per output
  period; needs `time.grain`). Without it the call is refused with
  `ENTITY_FILTER_PERIOD_UNSTATED`. The aggregate is per entity and never split by the call's
  other dimensions. `op` is `is`, `not`, `gt`, `gte`, `lt` or `lte`.
- Package-authored predicates (in a metric's `filter:`) keep the engine's own scope keys
  (`scope_mode: contextual | entity_only`, `time_grain`, `time_alignment`); see
  [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md).
- An `over: window` predicate uses the query clock when compatible, or its input's sole
  compatible clock. If the query clock is incompatible and the input has several clocks, it
  refuses with `INVALID_TEMPORAL_BINDING` and lists the candidates. A measure's
  `default_temporal_role` does not choose among those clocks. For a direct measure input, pin
  its `temporal_role` (`{"kind": "measure", "measure", "temporal_role"}`). For a metric input
  advertising several `compatible_temporal_roles`, pins inside the metric do not narrow those
  clocks; set `time.temporal_role` to one of the listed clocks. Or use `over: all_time`. A
  model's default time supplies the clock only when the measure does not declare its own
  `times` list.
- A window-aligned predicate refuses with `INVALID_TEMPORAL_BINDING` if the chosen window clock
  is excluded by any measure inside the input, by its pin or its declared clocks; for a
  conversion, its base measure (the period filters base events; converted events match each
  base event's window).
- A threshold that zero satisfies (`is 0`, `lt 3`, `lte 0`, `not 1`) counts an entity with no
  rows at all as 0 when the input is a count or a sum, or an add or subtract of them, so
  "customers with no orders" works. Every entity, with rows or none, reads by one rule (see
  [Empty groups](QUERY_CALL.md#empty-groups-null-or-0)): an operand is 0 where its measure has
  data somewhere in the predicate's scope, and `NULL` where it has none. So
  `large_order_count is 0` keeps every customer without a large order when some order in scope
  is large, and keeps nobody when none is. `NULL` satisfies no threshold, as with a `having`
  on the same expression. An average, minimum, maximum, median or ratio over no rows is `NULL`
  too, so `average_order_value lt 20` keeps only customers that have orders, as SQL
  `HAVING AVG(x) < 20` does. Conversion metrics and anchored `scoped_aggregate` ratios refuse
  a count or sum threshold that zero satisfies for now.

### Conversion

```json
{
  "select": [{
    "expression": {
      "kind": "conversion",
      "entity": "entity.jaffle_customer",
      "window": {"unit": "day", "value": 7},
      "matching_mode": "first_converted_after_base",
      "base": "measure.jaffle.session_starts",
      "converted": "measure.jaffle.order_count"
    },
    "as": "conversion_7d"
  }]
}
```

The `window` is a duration measured from the base event: a converted event counts when
`base <= converted < base + window`, on every warehouse. A 7-day window ends exactly 7 × 24 hours
after the base event, not at the end of the 7th calendar day, and `month`, `quarter` and `year`
windows add calendar months (January 31 plus one month is the last day of February). An event at
the base instant counts and one exactly at the end does not, like the half-open `time` range. When
the event times are dates, a 7-day window covers the base date and the 6 dates after it. These are
MetricFlow's conversion bounds.

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
- `starter_query_patch` — runnable call seed attached to every measure,
  metric, and dimension candidate. Measures/metrics seed `select`;
  dimensions seed `group_by`. Removes the discover → inspect round-trip
  blind agents otherwise need just to copy the patch shape.

Supported request controls:

- `stage`
- `verbosity`
- `kinds` — an array, a comma-separated string, or a JSON array in a string. Ranked kinds are
  `measure`, `metric`, `segment`, `dimension`, `entity` and `dimension_value`; a value that
  does not parse or names another kind is refused with `400` instead of returning empty buckets.
  In resource-grant mode only `metric`, `dimension` and `temporal_role` are produced, and the
  refusal lists them in `details.valid_kinds`. A `limit` below 1 is refused.

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

A dimension that `discover`, `inspect` or `build-options` lists through a time-valid
relationship may still need `time` at compile time: without one, the query is refused with
`FANOUT_UNSAFE`, naming the relationship and asking for `time`.

Packages that opt into `defaults.operational` can also expose a nested `operational` block on measure and metric cards.

Useful card fields for measures:

- `usage_summary`
- `recommended_dimensions`
- `recommended_filters`
- `sample_questions`
- `comparison_family`
- `preferred_companion_metrics`
- `starter_query_patches` — list of `{kind, query_patch [, note]}` entries.
  Always includes `select` (and `group_by` when a recommended dimension
  exists, `time` when a default temporal role exists). May also include:
  - `order_by` — when the card has a `default_temporal_role`; seeds
    `[{field: "period", direction: "desc"}]`.
  - `where` — when a recommended dimension has a declared `value_domain`;
    seeds a sample-value filter using `{field, op, value}`. The `note`
    field flags the value as scaffolding.
  - an entity filter — only on predicate-backed metric cards; seeds
    `{entity, having: [{field, op, value}], over}`. The `note` field flags the
    op/value as scaffolding to replace before executing.
- `policy_effects`
- `policy_visibility`

### `POST /api/v1/build-options`

Request:

```json
{
  "query": {
    "select": [{"expression": "measure.jaffle.order_count", "as": "orders"}]
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

Response keys:

- `ok`
- `status`
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

`source` is typically `value_domain` or `duckdb`. Without a declared domain and
with `allow_live_query=false` (the default), HTTP 200 returns `ok: false`,
`status: "needs_live_query"`, `source: "none"`, empty `values`, and a
`VALID_VALUES_NO_DOMAIN` warning. Retry with `allow_live_query=true` to permit a
live lookup. A declared empty domain remains a successful empty result. This
payload is shared with MCP; only MCP adds `next_call` naming its tool. HTTP
returns no `next_call`.

Live lookups choose the first eligible anchor measure in the existing order
(query measures first, then package order). Candidates must pass the same bound
query policies and row-filter requirements as execution; hidden, restricted,
withheld or denied measures are skipped without appearing in the response or
source diagnostics. If all available candidates are denied, it keeps the
`POLICY_DENIED` refusal without naming them; otherwise a lookup with no source
refuses with `NO_VALID_VALUES_SOURCE`. Row filters still constrain the returned
values and counts.

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
- `status` (`ok`, `needs_clarification`, `low_confidence`, `unrealizable`, or `out_of_scope`)
- `best` (`null` unless a draft exists)
- `next`
- `why` when `status != "ok"`
- `assumptions` when the draft's window rests on a reading of the question's open end, such
  as an inclusive last day
- `alternatives` and `blocked` when `detail` is `full` or `debug`
- `compose_hints` when `detail` is `debug`

`detail="query"` is an MCP-oriented compact projection. It preserves
`plan_version`, `intent`, `status`, `why`, `tie_break_hints`, `assumptions`, `warnings`, and a compact
`best` containing `pattern`, `query`, `resolved`, `validation_ok`, and
`subject_ids_used`.

For `status="out_of_scope"`, `why.code` distinguishes classifier
refusals (`OUT_OF_SCOPE`) from relevance-floor refusals
(`LOW_RELEVANCE`). The relevance floor and its `catalog_token_sample`, in `plan` and in
`discover`, read only objects the caller may see: a hidden object's label, id or synonym
never counts toward relevance or appears in the sample.

For `status="low_confidence"`, inspect `why` before execution. In particular,
`PLAN_FALLBACK_SEMANTIC_DRIFT` means a fallback draft validated but changed or dropped requested
intent slots such as target, grouping, qualification, filters, or time scope; the runtime keeps the
closest primary draft instead of silently returning a semantically different answer.
Parsed qualification drafts are held with `PLAN_INTENT_COVERAGE_GAP` until their cohort
and time scope can be proven. Grouping by entity keys or selecting a key count alone does not
prove that the qualification was applied. Store grouping terms use the same
dimension resolution as other entities. A bare "by store" can therefore require clarification;
name the intended dimension, such as "by store id" or "by store name".

`best.query` is the call for the selected draft. When
`status="ok"`, `plan` has already called `validate`, which pays the
compile cost and warms the runtime compile cache. Agents can forward
`best.query` to `compile` or `query`; call `validate` again only when
they need the full diagnostics envelope or are editing the call by hand.

Planning reads the time reference against the request's `now` (`options.now` over HTTP, the
`now` argument over MCP; default the server clock): `today`,
`yesterday`, `this/current <day|week|month|quarter|year>` and relative ranges use
that clock. Plan drafts and checks every window in one planning zone: the package's
default zone (UTC when unset). `now` (or, without it, the current instant) is read in each
zone; a `now` without an offset is UTC. A draft is `ok`
only when the question's window reads the same days in the selected temporal
role's zone as in the planning zone; otherwise plan holds with
`TIME_WINDOW_UNRESOLVED`, names the window phrases in
`why.details.unresolved_phrases` and returns no `best.query`. The check covers every
role a metric's legs are read on: a leg that can't read the selected role keeps its
own, and `why.details.temporal_roles` names each such role whose zone reads other
days, with its zone. A relative phrase stays relative in the draft ("last week" is
`time.last`, "this month" `time.this`), so the call resolves against the `now` it is sent
with. Two periods side by side ("month over month") return `needs_clarification` with
`PERIOD_COMPARISON` and a call that reads them as one series; a comparison whose period isn't
over at `now` holds with `PERIOD_COMPARISON_INCOMPLETE`. A caller bound is read only at a
whole day
(a date, or 00:00 with no offset or a UTC one), and one with a zone designator (`Z`
or `±hh:mm`) only when its offset is the role zone's at that instant; a window
missing either bound, a bound with another time of day, or a window whose start is
not before its end, doesn't match the question's window.
Existing holds on explicit caller windows remain conservative. As-of phrases
(`now`, `right now`, `currently`, `at the
moment`, `as of now`, `current` before a metric, `end of <window>`, and `as of
<window or date>`) remain `low_confidence` with `TIME_WINDOW_UNRESOLVED` naming
the complete cue. An explicit interval cannot consume an as-of request. A range
ending in an as-of cue (`2026-01-01 to now`) stays unresolved in full, and an
unrepresentable closing-day bound also returns this hold rather than an exception.

Planning reads the package as the caller sees it (the resolved request context), before matching
or ranking dimensions, measures and metrics, including catalog fallback and Intent IR
groupings and subjects: an object hidden from the caller is absent from drafts,
alternatives, diagnostics and composition hints in every detail mode, and naming one has
the same outcome as naming an object the package doesn't have
([What a caller sees of a hidden object](PACKAGE_AUTHORING.md#what-a-caller-sees-of-a-hidden-object)).

When a grouping word names available dimensions on several entities and none
belongs to the selected measure's root entity, planning holds the draft with
`low_confidence` and `PLAN_UNMATCHED_TERMS`. This applies to primary planning
and catalog fallback, adding discovery's additional matches, including plurals,
unless a strict match names the whole term by label or dimension ID (underscores
read as spaces). A column name alone does not settle it. Discovery evidence can add a hold or an option;
only strict matches satisfy a grouping. The reason lists
`ambiguous_groupings` and `details.clarification.options`, with one validated
option per visible candidate dimension. Each option names its `id`, `label`
and grouping `term`. With exactly one unclear term, it also carries
`group_by`, `where` and `order_by`: apply those three fields to `best.query`,
then validate or execute the chosen reading. Filters and unrelated groupings
are preserved; dimension sort fields follow the chosen grouping. With two or
more unclear terms, options carry `id`, `label`, `term` and `replaces`, the draft
`group_by` IDs matching that term. In `best.query`, for each ambiguous term,
remove its chosen option's `replaces` IDs from `group_by` and the corresponding
`order_by` entries, add the chosen `id` to `group_by`, keep `group_by` IDs sorted
so choices compose in any order, then validate. When two terms could replace the
same grouping, plan offers no options; ask the user. For example,
"item revenue by district" asks whether Store district or Customer district
is intended.

A root-owned matching dimension takes precedence: a draft using another
entity's match instead is held with `PLAN_UNMATCHED_TERMS`. Planning keeps
that draft for inspection rather than changing its business meaning. A
caller-supplied `group_by` settles a shared non-root grouping only when the
draft adds no competing match. No heuristic match alone makes a held draft
ready.

Catalog candidate lists added during validation and error enrichment come from the
same caller's view, so recovery hints and near-match suggestions never name a hidden
object. A filter alias shared with a hidden object names the visible one, as it would in
the package without the hidden object. Error messages refer
to structured clock alternatives without embedding catalog IDs. Package authoring
validation retains the full catalog for reference suggestions.

Validate or execute `best.query` directly. `next` carries `ready_for` and optional
`valid_values` calls, without duplicating the query. For `detail="best"`, fallback drift
reasons use slot paths in `best.trace.intent_slots` and `why.details.fallback_slots`
instead of repeating their values. Full/debug and query detail retain expanded diagnostics.
Identical catalog rows in `intent_ir` and query fields in gap diagnostics use `$ref`
objects pointing to their canonical value by a dot-separated response path.

Every `best` entry may include a compact `trace` showing extracted intent slots, selected
subjects/groupings/filters/paths, and whether a fallback was used. Treat it as diagnostic context,
not as a separate workflow or server-side trace store.

`plan` preserves any caller-supplied partial call fields. Additive
fields such as `select`, `where`, `having`, `order_by`, and
`group_by` keep the caller entries first, then append generated entries
when needed.

The question's values from one value-list phrase on the same dimension become one
filter: `is` with one value or a list. This yields one total or one
combined ranking; no grouping is added for those values. A per-value breakdown
needs an explicit grouping in the question (for example, "by store") or the
caller's `group_by`.
For example, "top 3 product type by item revenue for Brooklyn and
Philadelphia" groups by product type and uses both stores in one membership
filter. Catalog fallback resolves each requested grouping before deduplicating
by dimension ID.
When the caller passes `group_by` and the draft adds a grouping dimension the
caller didn't pass, the plan is not ready to execute: the plan keeps both
groupings in `group_by` and returns `low_confidence` with an
`ambiguous_grouping` gap in `PLAN_INTENT_COVERAGE_GAP` and a `clarify_grouping`
hint. The planner doesn't guess whether the question's grouping phrase restates
a caller dimension or asks for another one. For example, "item revenue by store"
with `group_by: ["dimension.jaffle_item_product_type"]` keeps both the product
type and store groupings. To execute, pass every intended grouping dimension ID
in `group_by`, here both IDs.
Caller filter rows stay as written, in their original order, with only string
field IDs stripped of surrounding whitespace. Their operators and values are
preserved, and generated rows are appended unless identical rows already exist.
Conflicting filters or exclusions of requested values yield `low_confidence`.
Separate equality clauses remain separate predicates: "where store is Brooklyn
and store is Philadelphia" retains both equalities, reports `contradictory_filters`,
and has no `execute` readiness. Values without a proven shared list phrase also
remain separate predicates.

Qualified metric asks return `interpreted_intent.pattern: "qualified_metric_rollup"` and a validated runtime-composed `scoped_aggregate` whose predicates state `over`.

### `POST /api/v1/validate`

Response keys, besides [the answer](QUERY_CALL.md#the-answer) (`status`, `window`, `clock`,
`observation_scope`, and `routes`, `assumptions`, `warnings` and `errors` when they have
entries; no field is ever empty):

- `ok`
- `version`
- `recovery_hints` (when there are some)
- `policy_effects`
- `provenance_summary`
- `disabled_options` (when there are some)
- `logical_plan`
- `explain`
- `query` (the call it answered, in the call's spelling)
- `normalized_query` (the call as the engine read it, defaults filled in, in the call's
  spelling)
- `timing_ms`

`validate` returns structured diagnostics. It does not need to throw on semantic failure.
A query that groups by, or applies a known NULL-excluding filter to, a dimension read through
a hop into a validity window gets
`NULL_PRESERVING_HISTORY`, naming the hop; on `execute`, a grouped one counts its empty group
from the returned rows (see
[`graph.yml`](PACKAGE_AUTHORING.md#graphyml)).

### `POST /api/v1/query`

Response keys, besides [the answer](QUERY_CALL.md#the-answer):

- `ok`
- `rows`
- `row_count`
- `rendered_sql`
- `logical_plan`
- `sql_plan`
- `explain`
- `query` (the call it answered, in the call's spelling)
- `normalized_query` (the call as the engine read it, defaults filled in, in the call's
  spelling)
- `recovery_hints` (when there are some)
- `withheld` (only for a rank by values a `withhold_values` policy withholds: the withheld
  objects, whose column `rows`, `column_types` and `output_columns` leave out; retained at
  minimal verbosity and under resource grants)
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
  `details.policy_effects` lists each effect as the caller may see it: a policy that lists
  an object hidden from the caller, or whose text names one, appears with its kind, action,
  visible `object_ids` and the action's fixed text, without `policy_id`. When what the
  caller may see can't be resolved, every operation refuses before binding or the
  warehouse with `details: {"reason": "visibility_unresolved"}`, naming nothing.
- `REWRITE_NOT_SUPPORTED`
- `INVALID_EXPRESSION_AST` — ships a `USE_OBJECT_SHAPE` recovery hint in
  `details.recovery_hints` when the failure is a window/offset payload
  that was passed as an int instead of a `{unit, value}` object.
- `WINDOWED_TIME_FILTER_UNSUPPORTED` — ships two ordered recovery hints
  at the top-level `recovery_hints` field: `drop_time_start` (always-safe
  fallback, listed first) and `widen_time_window` (computed
  `suggested_start`; agents should widen by lookback + one full bucket
  when the metric is at a discrete grain). When a `where` filter on a
  date or calendar dimension caused it, `details.where_path` names the
  filter and `drop_time_start` removes that filter instead.
  For a filter on the window's measure input or an applied row policy
  (`details.filter_source`), no time-boundary
  recovery patch is offered; query an unwindowed measure or ask the package author
  for a supported metric. Removing `time.start` cannot restore rows removed by
  those filters, and the engine never widens a row policy to read lookback history.
  Separately filtered aggregates and policies on unread tables do not truncate
  the window's input. Source refusals follow object authorization and omit
  authored conditions and values; a denied caller receives `POLICY_DENIED`.

### Warning Codes

The response `warnings` array can carry these non-error signals:

- `NO_DATA_YET` — fires on `execute` for guarded `NULL` series buckets beyond visible
  coverage, empty totals whose coverage is proven before the window, or no visible
  coverage. `details.outputs` lists output aliases; `details.measures` lists
  `{id, edge, edge_source}`: `edge` is the date, in the clock's zone, of the measure's last
  visible row, with `edge_source` `last_row`. Both are null with no visible coverage.
  An empty series with dated coverage keeps existing warnings. `NO_DATA_YET` replaces
  `EMPTY_RESULT_WINDOW`, and covered outputs are excluded from `NO_DATA_IN_SCOPE`.
  Resource grants expose it only when every named measure is granted.
  See [Empty groups](QUERY_CALL.md#empty-groups-null-or-0).
- `NO_DATA_IN_SCOPE` — fires on `execute` when a sum, count or distinct count (or a sum or
  difference of them) reads `NULL` on every returned row, or nothing came back and neither a
  `start`/`end` window nor an entity filter explains it. Such a
  measure reads `0` in an empty group only where it has data in scope; here it has none, so it
  is `NULL`. `details.outputs` names the outputs. It never fires on a clipped (`truncated`)
  result, nor, under `observation_scope: "dataset"`, on an empty answer to a filtered query.
  See [Empty groups](QUERY_CALL.md#empty-groups-null-or-0).
- `FILTER_VALUE_NOT_FOUND` — fires on `execute` under `observation_scope: "dataset"` (the
  default) when a string `is` `where` value matches no row of its dimension that the
  caller can read: there a sum reads `0`, which a misspelled value shouldn't produce silently.
  Aggregate-filter literals of a retained additive series are checked in both observation scopes.
  One warning per query; `details.filters` lists each `dimension`, `value` and the closest
  `suggestion`.
- `FILTERED_SERIES_BUCKETS_DROPPED` — an unsupported filtered additive series omits
  observed buckets. `details.dropped_buckets` lists their time and grouping keys from a
  separately authorized source query; averages over returned rows omit those buckets.
- `FILTERED_SERIES_BUCKETS_UNVERIFIED` — that source query was denied, failed or capped,
  or the answer has a limit or population filter. `details.reason` explains why the
  missing buckets could not be established. See [Empty groups](QUERY_CALL.md#empty-groups-null-or-0).
- `UNKNOWN_ROWS_KEPT` — a `not` condition kept rows whose value is unknown (NULL or a
  declared `unknown_values` entry). `details` has `field`, `rows` (how many) and `values`; the
  message gives the `missing: false` condition that leaves them out. Only when `rows` > 0.
- `MIXED_TIME_ROLES` — fires on `validate`, `compile` and `execute` when a query with no
  `time` block selects measures of different entities or governed metrics with differing
  sets of real time roles, mixing at least two distinct roles. Undated measures are ignored;
  a governed metric counts as one clock. The message names the roles and says each period is
  read on its own role's clock; measure-level filters can bound those periods.
  `details.clocks` lists each `subject` with its `temporal_roles`. With a `time` block and no
  `temporal_role`, the same mix is refused instead, with one option per clock (see
  [The clock](QUERY_CALL.md#the-clock)).
- `EXPRESSION_NORMALIZED_AWAY` — fires when an input expression `kind`
  was recognized by the parser but did not survive normalization (or
  the user's `as:` alias is missing from compiled output). Carries
  `details.dropped_expression` and `details.compiled_kind` so the agent
  can decide whether to retry with a different shape.
- `QUERY_SHORTHAND_NORMALIZED` — fires on `validate`, `compile` and `execute`
  (not `plan`) once per `dimension.` id in `select` that was moved to `group_by`.
  Carries `details.path`, `details.received` and `details.canonical`. See
  [`select`](QUERY_CALL.md#select).
- `SEMANTIC_CAVEAT_APPLIED` — fires when a package-authored caveat is
  relevant to the compiled query shape and time window. Caveats are
  advisory interpretation context: they do not alter SQL, rows, access,
  discovery, or policy behavior. Severity `info` adds definitional
  framing to repeat when narrating results; severity `warning` means the
  matched window or slice itself is affected and comparisons built on it
  can mislead. Agents should mention caveats only when explaining
  affected results.
  Every matching caveat is stated at every verbosity; verbosity trims only
  each caveat's details.
- `ROUTE_PASS_THROUGH` — severity `warning`, at every verbosity: the package's
  one route for a pair the query reads goes down into another table's rows and
  back up to another parent ("the Team of any of the Member's Member events"),
  and the package doesn't declare that crossing, so a start row with no such
  row is left out and one with several can count under several targets. The
  message gives the meaning and the fixes; `details.route`, `details.meaning`,
  `details.through` and `details.fixes` (`declare_key`, `declare_link_table`,
  `record_route`, and `entity_filter` when the query has conditions on the target). See
  [the route rule](PACKAGE_AUTHORING.md#the-route-rule).

The answer's `routes` (not a warning) names each entity pair whose route was
chosen among two or more, as `{from, to, via}`, at every verbosity; a pair with
one route is not listed. A route the call chose among its `AMBIGUOUS_PATH`
clarification's options adds `other_routes`: at most three of the other options,
each ready to send as `routes`, with `more_routes` counting the rest. Validate
the call without `routes` to receive every option without a warehouse query;
alternatives are never executed. A compact or full answer adds how each route
was chosen (`basis`): `routes` (the call's, with `replaced`: `decided`,
`colocated_key`, `inherited`, `only_route` or `undecided`), `own_key` (the
start entity's own key, with `alternatives`, the row that would make each other
route the default when it would load beside the package's rows, and
`conflicts_with`, any other route with the rows its row would disagree with) or
`recorded` (the package's `graph.path_preferences` rows, with `recorded_for`
naming the pairs whose rows an inherited route follows). Route clarifications,
conflict rows, switches and plan candidates name only routes whose
relationships and every waypoint are visible to the caller. Routes are never re-chosen per caller: when the
  package's route for a pair reads a hidden entity or relationship, the pair has
  no route for that caller (`PATH_NOT_FOUND`, `details.reason:
  "no_relationship_chain"`), and a `routes` entry naming a hidden
  relationship is an unknown id. An ambiguous pair offers only its visible
  routes and is never answered by the remaining one. See
  [`routes`](QUERY_CALL.md#routes).

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

`details` is returned whole on every surface (HTTP, MCP and CLI). An
`AMBIGUOUS_PATH` refusal's `details` carry `reason: route_decision_required`,
`start`, `target`, `hint` and `clarification`. Customers by customer-history segment, on
jaffle_shop:

```json
{
  "code": "AMBIGUOUS_PATH",
  "details": {
    "reason": "route_decision_required",
    "start": "entity.jaffle_customer",
    "target": "entity.jaffle_customer_history",
    "clarification": {
      "kind": "route",
      "apply": ["query", "package"],
      "question": "Which Customer history does the question mean for a Customer?",
      "options": [
        {
          "id": "order_customer_history",
          "meaning": "the Customer history valid at the time of any of the Customer's Orders",
          "relationship_path": ["relationship.orders_customer", "relationship.order_to_customer_history"],
          "decision": {
            "source_entity": "entity.jaffle_customer",
            "target_entity": "entity.jaffle_customer_history",
            "relationship_path": ["relationship.orders_customer", "relationship.order_to_customer_history"],
            "label": "the Customer history valid at the time of any of the Customer's Orders"
          },
          "query": {
            "select": ["measure.jaffle.customer_count"],
            "group_by": ["dimension.jaffle_customer_history_segment"],
            "time": {"grain": "year"},
            "routes": [{"from": "entity.jaffle_customer", "to": "entity.jaffle_customer_history",
                        "via": ["relationship.orders_customer", "relationship.order_to_customer_history"]}]
          }
        }
      ]
    },
    "hint": "Which route is meant is a business definition. ..."
  }
}
```

Send an option's `query` to answer the question with that route (this call only), or record
its `decision` in the package with Architect `record_route_decision`.

## Real Example

```bash
curl -s -X POST http://127.0.0.1:8081/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "select": ["measure.jaffle.order_count", {"expression": "metric.sales.aov_usd", "as": "aov_usd"}],
      "group_by": ["dimension.jaffle_store_name"],
      "time": {"last": {"unit": "month", "value": 3}, "grain": "month"},
      "limit": 5
    },
    "options": {"now": "2017-09-04T12:00:00Z"}
  }'
```
