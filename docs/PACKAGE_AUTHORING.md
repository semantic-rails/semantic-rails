# Package Authoring

Semantic Rails packages are authored as a directory of YAML files. The authoring
contract is the v1 contract — `schema_version: 1`, with the loader normalizing the
ergonomic surface into the runtime `PackageConfig`.

This guide is the canonical narrative and per-attribute authoring reference for
the public engine.

## Measures vs metrics — the conceptual split

Before any syntax: understand the two layers.

**Measures are primitives.** A measure is a columnar fact (a sum, a count, a
count-distinct) that the API can query flexibly — by any reachable entity, time
grain, dimension breakdown, or aggregation function within the measure's allowed
set. Measures are the building blocks. ARR is a measure: sum of monthly ARR
contributions, queryable by customer, by segment, by month.

**Metrics are governed access patterns.** A metric is a named, stable contract
that codifies a specific use of one or more measures — with conditions, filters,
time alignment, or composition (ratio, cumulative, derived). Metrics exist for
governance and clarity. NRR is a metric: `(start_arr + expansion_arr − churn_arr)
/ start_arr` with specific cohort and time-alignment conditions.

Implications:

- Not every measure needs a metric. Many measures are queryable as primitives.
- The catalog lists `measures` and `metrics` as distinct surfaces. Both are
  queryable; only metrics carry stable governance.
- Measures do not auto-publish to metrics. If you want a measure exposed as a
  governed metric, write it explicitly in the `metrics:` block.

## Quickstart: `init` a directory package

The fastest way to a working package from a pip install is the split-layout
scaffold:

```bash
semantic-rails init my_pkg --yes
semantic-rails project status --path ./my_pkg
semantic-rails project validate --path ./my_pkg
semantic-rails repl --path ./my_pkg
semantic-rails ask --path ./my_pkg "total amount by event type" --run
semantic-rails mcp setup --path ./my_pkg
```

From a source checkout, prefix the same commands with `uv run`.

Inside the REPL, `author` is the guided package-management entry point:

```text
semantic-rails [my_pkg] › author
  1. Model/entity — connect a table and define its business grain
  2. Dimension — something people group or filter by
  3. Time — when an event or state occurred
  4. Measure — a primitive count, sum, or aggregatable fact
  5. Metric — a stable governed KPI built from measures or metrics
  6. Segment — a reusable entity cohort
```

Use `author metric` (or any other kind) to skip the first menu. The flow lists
valid package objects where a reference is required, recommends the common
choice, shows the target file and YAML before writing, calls out exact or
similar existing definitions, and defaults every create/update confirmation to
No. Successful edits are parse-validated and can be restored with `undo` during
the same REPL session. A failed parse restores the original files automatically.

`validate` is intentionally the safe, parse-only check. `validate runtime`,
`validate examples`, `validate tests`, and `validate full` may query or refresh
the configured warehouse, so the REPL describes that operational boundary and
asks before continuing.

`init <name>` writes a **directory package**: `package.yml`, `graph.yml`,
`models/`, `metrics/`, `examples/`, `tests/`, and starter CSV data. Directory
packages are loaded by pointing `--path` at the directory:

```bash
semantic-rails catalog --path ./my_pkg --verbosity summary
semantic-rails plan --path ./my_pkg --intent "monthly revenue"
semantic-rails mcp setup --path ./my_pkg
semantic-rails mcp http --path ./my_pkg --host 127.0.0.1 --port 8091
```

Architect MCP and the REPL use the same workspace-scoped model, metric, and
segment upsert service. Architect MCP can run the workflow from an MCP client;
the REPL gives terminal users the same parse-safe mutations with interactive
choice guidance and previews. The CLI `init` path remains the deterministic
terminal baseline for scaffolding a public release.

Optional local defaults are separate from package authoring. If you want the
human-facing CLI to use this package when you omit `--path`, run:

```bash
semantic-rails profile init --package-path ./my_pkg
semantic-rails profile show
```

This writes `~/.semantic_rails/profiles.yml` (or
`$SEMANTIC_RAILS_HOME/profiles.yml`). Treat it like a dbt profile: useful for a
developer machine, not checked into the package, not a secret store, and not
hosted control-plane configuration.

`validate-config` and `project validate` write a `.compiled/manifest.json` next
to the package. The manifest holds `package_id`, a content `fingerprint` of the
source, and pre-rendered catalog variants the runtime can serve without
recompiling; a stale fingerprint falls back to live compute. Pass `--no-manifest`
to `validate-config` to skip writing it.

The older single-file starter is still available when you specifically want one
YAML file:

```bash
semantic-rails init --single-file --output ./my_single_file_pkg --package-id my_single_file_pkg
semantic-rails validate-config --path ./my_single_file_pkg/package.yml
```

Single-file packages are loaded by pointing `--path` at the **file**, not the
directory. They can still have sibling `examples/` and `tests/` directories next
to the `package.yml`; `run-examples`, `test-package`, and `check` pick them up
from the file's parent directory, and `build-package` bundles them plus the
referenced seed SQL.

Two rules apply to directory packages:

1. `graph.yml` and `models/` are required — a directory source without them is
   rejected.
2. `package.id` must equal the directory name (`configs/semantic_rails/shop/`
   must declare `package.id: shop`); validation fails on a mismatch.

## Directory layout

```
configs/semantic_rails/<package>/    # directory name must match package.id
  package.yml          # identity, warehouse, connection, seeds, defaults
  graph.yml            # canonical entities and explicit relationships
  defaults.yml         # optional — package-wide defaults merged before models
  policies.yml         # optional — visibility / access / release labels
  caveats.yml          # optional — advisory interpretation context
  models/              # one file per warehouse table or mart
    <model>.yml
  metrics/             # optional — governed access patterns
    <metric>.yml
  segments/            # optional — entity-bounded membership filters
    <segment>.yml
  examples/            # optional — runnable example queries
    <example>.yml
  tests/               # optional — package-local regression tests
    <test>.yml
```

The loader merges every YAML file under `models/**`, `metrics/**`, and
`segments/*` into a single `PackageConfig`.

## What the loader does for you

Setting `package.namespace` (or letting it default to `package.id`) buys you a lot
of YAML you never have to write. The contract leans hard on this — author the
business meaning; the loader fills in identifiers and traversal.

| Authored | Auto-derived |
|---|---|
| `package.namespace + key` | `id`, `name` for every object |
| `graph.entities.<x>.key` | Key dimensions on every model that exposes the entity |
| `model.entities:` block with FK references | Default `RelationshipConfig` between every co-declared pair |
| `model.entities.<primary>.key` matches `model.grain` | Primary entity (no marker needed) |
| `times:` block on a model | Backing date/timestamp dimension |
| `accumulation: { kind: flow }` | `default_aggregation = sum` and the allowed-aggregation set |
| `model.variants:` rollup entries | Exact `AggregateRelationConfig` rows the planner can route to |

If you need to override a derived ID (typically because external systems hard-code
a public reference), use the `as:` escape hatch.

### Derived-ID grammar

Knowing the grammar lets you predict every queryable ID before running `catalog`
(`<ns>` is `package.namespace`):

| Kind | Grammar | Example (starter package, `namespace: mypkg`) |
|---|---|---|
| Entity | `entity.<ns>_<entity_key>` | `entity.mypkg_order` |
| Dimension | `dimension.<ns>_<entity>_<dimension_key>` | `dimension.mypkg_order_channel` |
| Temporal role | `temporal_role.<ns>_<entity>_<time_key>` | `temporal_role.mypkg_order_ordered_at` |
| Measure | `measure.<ns>.<measure_key>` | `measure.mypkg.order_count` |
| Metric | `metric.<ns>.<metric_key>` | `metric.mypkg.revenue_usd` |

Notes:

- Measures and metrics use a **dot** between namespace and key; entities,
  dimensions, and temporal roles use underscores throughout.
- Auto-created key dimensions collapse a duplicated entity prefix: the
  `customer` entity's `customer_id` key column becomes
  `dimension.mypkg_customer_id`, not `dimension.mypkg_customer_customer_id`.
  An FK column on another model keeps its full name:
  `dimension.mypkg_order_customer_id`.
- Dimensions auto-created from `times:` blocks use the backing column name
  (`dimension.mypkg_order_ordered_at`).
- `as:` overrides any of these (jaffle's `temporal_role.jaffle_order_time` is an
  `as:` override of the derived `temporal_role.jaffle_order_ordered_at`).

## Minimal example

A working `shop` package with one entity, one model, one measure, and one metric:

```yaml
# package.yml
schema_version: 1

package:
  id: shop
  namespace: shop
  warehouse: duckdb
  default_db: data/shop.duckdb
  seed: { kind: sql_script, source: data/seed.sql }
```

```yaml
# graph.yml
graph:
  entities:
    order:
      label: Order
      key: [order_id]                 # scalar and list forms both load; composite keys need the list
      model: orders                   # which model declares this entity as primary
      disallowed_names: [ord_id, orderid]
    customer:
      label: Customer
      key: [customer_id]
      model: customers                # see customers model below
      disallowed_names: [cust_id, custid, customerid]
```

```yaml
# models/orders.yml
model:
  id: orders
  label: Orders
  relation: shop_order
  # grain: is derived — the graph's `model:` pointer marks this model as
  # primary for `order`. In schema_strict DIRECTORY packages, authoring
  # `grain:` alongside `entities:` is rejected ("Drop 'grain:'"). In
  # single-file packages both are accepted (the init starter authors
  # `grain:` explicitly to pin the primary entity).

  entities:
    order: {}                         # primary (grain = graph's order.key)
    customer: {}                      # FK reference; column = graph's customer_id

  times:
    ordered_at:
      label: Order time
      column: ordered_at
      kind: timestamp
      class: event_time
      supported_grains: [day, week, month, quarter, year]
      default: true

  dimensions:
    status:
      label: Order Status
      kind: categorical

  measures:
    order_count:
      label: Order Count
      kind: entity_count
      entity_key: order_id            # the key COLUMN, not the entity name
      accumulation: { kind: event }
      value_type: count

    revenue_usd:
      label: Revenue (USD)
      kind: aggregate
      expr: amount_usd
      default_agg: sum
      accumulation: { kind: flow }
      value_type: currency
      publish: false                  # metric defined explicitly below
```

```yaml
# models/customers.yml — every graph entity needs a model: pointer
model:
  id: customers
  label: Customers
  relation: shop_customer

  entities:
    customer: {}                      # primary (grain = graph's customer.key)

  times:
    signed_up_at:
      label: Signup time
      column: signed_up_at
      kind: timestamp
      class: event_time
      supported_grains: [day, week, month, quarter, year]
      default: true

  measures:
    customer_count:
      label: Customer Count
      kind: entity_count
      entity_key: customer_id         # the key COLUMN, not the entity name
      accumulation: { kind: event }
      value_type: count
```

```yaml
# metrics/revenue.yml
metrics:
  revenue_usd:
    label: Revenue (USD)
    description: Total revenue. Codified for stable reference.
    kind: aggregate
    measure: revenue_usd
    value_type: currency
```

This package compiles to:

- One entity (`order`) plus the `customer` reference (FK).
- One model with a primary entity, one time role, one categorical dimension,
  two measures.
- One metric (`metric.shop.revenue_usd`) — the `order_count` measure is
  queryable directly as a primitive without a corresponding metric.

## `package.yml`

Declares identity and runtime targets.

```yaml
schema_version: 1

package:
  id: shop
  namespace: shop
  warehouse: duckdb               # or snowflake
  default_db: data/shop.duckdb    # required for duckdb
  seed: { kind: sql_script, source: data/seed.sql }
  schema_strict: true             # opt-in v1 strict validation (recommended)

defaults:
  dimension: { groupable: true, filterable: true }
  time:
    timezone: UTC
    supported_grains: [day, week, month, quarter, year]
```

`schema_strict: true` turns on strict v1 validation (see the
[Validation profile](#validation-profile) section). Recommended for new packages.

### `package.environments` and governance `meta:`

```yaml
package:
  environments: [development, staging, production]
```

`package.environments` declares the environment names the package recognizes.
`promote-package --environment <name>` rejects undeclared environments with
`INVALID_CONFIG` (`details.allowed_environments`), and policies that declare
`environments:` only fire when `policy_context.environment` matches one of
them. `validate-config` warns (advisory) when a package omits the block.

Measures and curated metrics also accept a `meta:` block that drives advisory
governance warnings:

```yaml
meta:
  owner_team: finance_analytics
  review_priority: high
  change_risk: medium
```

`validate-config` warns when a public measure or curated metric omits
`meta.owner_team`, `meta.review_priority`, or `meta.change_risk`. The warnings
are advisory — they never block loading — but a warning-free package gives
reviewers an owner and a blast-radius signal for every governed object.

For Snowflake-backed packages, replace `seed` with `connection`:

```yaml
package:
  id: shop_prod
  namespace: shop
  warehouse: snowflake
  connection:
    kind: snowflake_native
    name: prod_native
    options:
      account_env: SNOWFLAKE_ACCOUNT
      user_env: SNOWFLAKE_USER
      password_env: SNOWFLAKE_PASSWORD
      warehouse: COMPUTE_WH
      query_tag: semantic-rails
```

Two connection kinds are supported:

- `snowflake_cli` — uses a configured Snow CLI profile by name.
- `snowflake_native` — direct connector via env-var indirection (account, user,
  password, etc. read from environment variables).

Literal credentials in YAML are rejected.

### Secrets

**Secrets must come from process environment or an external secret store. Package
YAML must never contain literal credentials.** This is a hard contract enforced
by the loader.

The runtime rejects literal `password`, `token`, and `private_key` values inside
`package.connection.options` for `snowflake_native` and raises
`INVALID_CONFIG`. The allowed pattern is env-var indirection:

```yaml
package:
  connection:
    kind: snowflake_native
    options:
      account_env: SNOWFLAKE_ACCOUNT       # name of the env var, NOT the value
      user_env: SNOWFLAKE_USER
      password_env: SNOWFLAKE_PASSWORD
      private_key_file: /run/secrets/snowflake_pk  # file path is OK; literal key text is not
      private_key_passphrase_env: SNOWFLAKE_PK_PASS
      query_tag: semantic-rails
```

`snowflake_cli` resolves credentials through the operator's Snow CLI profile
(see `snow connection list`); package YAML never sees the secret material.

DuckDB packages have no credential surface — the connection is a local file
path. The `seed.source` and `package.default_db` paths are not secrets but
should be treated as deployment-private if they point at hydrated production
data.

This contract matters because package YAML is the unit of authoring artifact —
it is shared in pull requests, committed to git, screenshotted in demos, and
mounted into containers. Anything written there is effectively public.
Centralizing secret material in the process environment (or a mounted secret
store) keeps the YAML authoring surface trustworthy.

Hosted deployments will typically inject secrets via:

- Kubernetes secrets mounted as env vars or files
- AWS Secrets Manager / GCP Secret Manager loaded at startup
- HashiCorp Vault sidecars
- Cloud-provider IAM (e.g. Snowflake OAuth with a workload identity token)

The runtime does not care which — it only reads the env vars named by
`*_env` options or the files named by `*_file` options at connection time.

## `policies.yml`

`policies.yml` is the governance surface: a list of `semantic_policies:` rows,
each with an `id`, a `kind`, and (except for `package_release`) the
`object_ids` it governs. Five kinds exist, each driving a different runtime
behavior:

- **`package_release`** — labels the package's release status. `config.label`
  (e.g. `stable`, `preview`) surfaces in the package manifest and discovery
  metadata; it gates nothing by itself.
- **`object_visibility`** — hides matching objects from `catalog`, `discover`,
  and `inspect` for the scoped audiences/environments/roles. `action: hidden` is the
  useful value; a hidden measure cannot be discovered but a query that names
  it directly is governed by `object_access`, not visibility.
- **`object_access`** — enforced at query time. `action: deny` refuses the
  query with a structured policy error; `action: redact` executes but replaces
  the governed object's values in the result.
- **`protected_object`** — pins an object as protected in the named
  environments; `promote-package` and `impact-report` treat changes to
  protected objects as release-gated.
- **`metric_constraint`** — enforced at query time for scoped callers. It
  restricts how governed metrics or measures may be cut by `group_by`,
  `where`, temporal role, and metric-filter predicates.

Scoping works the same way as caveats: `audiences:`, `environments:`, and
`roles:` lists restrict when a policy applies, and an empty list means
"applies to every context". Role matching uses intersection with
`policy_context.roles`, so `roles: [sales, csm]` applies when either role is
present. The query-side context arrives via `policy_context` in the Query IR
payload (`{"policy_context": {"environment": "production", "audience":
"finance", "roles": ["sales"]}}`) or the CLI flags `--environment` /
`--audience`. A `rationale:` string is strongly encouraged — it is echoed in
policy-effect reports so the person whose query was denied learns why.

```yaml
semantic_policies:
  - id: policy.shop.redact_revenue_for_external
    kind: object_access
    object_ids: [measure.shop.revenue_usd]
    audiences: [external_partner]
    action: redact
    rationale: Raw revenue is sensitive; partners get redacted values.

  - id: policy.shop.sales_revenue_store_cuts
    kind: metric_constraint
    object_ids: [measure.shop.revenue_usd, metric.shop.cumulative_revenue]
    roles: [sales, csm]
    allowed_group_by: [dimension.shop_store_name]
    allowed_where: [dimension.shop_store_name]
    allowed_temporal_roles: [temporal_role.shop_order_time]
    allow_metric_filters: false
    rationale: Sales and CSM revenue access is limited to store-level cuts.
```

The annotated policy example lives in
[configs/semantic_rails/jaffle_shop/policies.yml](../configs/semantic_rails/jaffle_shop/policies.yml);
the enforcement semantics are implemented in `semantic_rails/policies.py` and
surfaced per query under `policy_effects`.

## `caveats.yml`

`caveats.yml` lets package authors attach human-written context that should
surface only when a query is likely to need it. Caveats do not change SQL,
rows, discovery, or policy behavior; they append `SEMANTIC_CAVEAT_APPLIED`
warnings on `validate`, `compile`, and `query`.

```yaml
semantic_caveats:
  - id: caveat.shop.store_a_closed_feb_2016
    kind: business_event
    message: Store A was closed in Feb 2016; store comparisons need context.
    object_ids: [dimension.shop_store_name]
    entity_values:
      - entity: entity.shop_store
        dimension: dimension.shop_store_name
        value: Store A
    time:
      from: "2016-02-01"
      to: "2016-03-01"
    severity: warning
```

The only caveat kinds are `business_event`, `definition_change`, and
`data_quality`; use `time.at`, `time.from`/`time.to`, `entity_values`, and
`object_ids` for specificity instead of inventing new kinds.

`severity` separates two distinct agent actions. `info` means the numbers are
right but carry a definitional nuance worth repeating when narrating results
("MRR is not the same as revenue"). `warning` (the default) means the matched
window or slice itself is affected — partial operating periods, definition
changes mid-series, known data-quality gaps — so comparisons and trends built
on it can mislead. If a caveat only adds vocabulary or framing, set `info`
explicitly.

Matching is deliberately conservative; a caveat that does not fire is usually
hitting one of these rules:

- **Time-bound caveats need a query window.** A caveat with `time:` never
  fires on a query without an explicit time range or an inferable comparison
  window (`prior_period` with a known grain).
- **Broad scalar totals suppress short caveats.** On an ungrained,
  non-comparative, non-grouped scalar query, a point caveat is suppressed
  when the query spans 90 days or more, and a range caveat is suppressed when
  it covers less than 25% of the query window. A March soft-launch is not
  worth mentioning on a single full-year total; group by a time grain or by
  the affected dimension and it fires again.
- **Entity involvement alone never matches.** Listing an entity in
  `object_ids` fires only when the query exposes that entity through a
  grouped or filtered dimension. Entities that merely participate via measure
  internals or the join plan are ignored — otherwise every revenue query
  would carry every store caveat.
- **`entity_values` match the declared dimension only.** A caveat keyed to
  `dimension.shop_store_name` = "Store A" does not fire when the query
  filters the same store through a different dimension such as its ID.
  If a value is commonly reached through more than one dimension, declare
  one `entity_values` row per dimension.

## `graph.yml`

The graph is the canonical source for entity identity. It declares the entities
the package exposes, their key column names, and any non-default relationships
between them.

```yaml
graph:
  entities:
    order:
      label: Order
      key: order_id                             # scalar or list — both load
      model: orders                             # the model that declares order as primary
      disallowed_names: [ord_id, orderid]      # anti-pattern guard
    customer:
      label: Customer
      key: customer_id
      model: customers
      disallowed_names: [cust_id, custid, customerid]
    product:
      label: Product
      key: product_id
      model: products
    customer_history:
      label: Customer history
      key: [customer_id, valid_from]            # composite keys need the list form
      model: customer_history

  # Explicit relationships — only for pairs that need non-default rules.
  # Most relationships are inferred from model.entities: blocks.
  relationships:
    customer_history_x_customer:
      entities: [customer_history, customer]    # bidirectional pair
      cardinality: many_to_one                  # first→second (history is many; customer is one)
      safety: requires_rewrite
      temporal_validity:
        valid_from: effective_from
        valid_to: effective_to
      rollup_safe:
        forward: [sum, count]                   # aggregating customer_history → customer
        reverse: []                             # aggregating customer → customer_history
```

### `disallowed_names:` — explicit anti-pattern guard

Author the names that should NEVER appear as a column, dimension, or measure on
any model. The validator rejects any model that introduces a name in the list and
points to the canonical entity column or the `expr:` escape hatch for intentional
renames. This replaces heuristic near-duplicate detection — explicit and
configurable.

### Bidirectional relationships

Each `relationships:` entry is an unordered pair of entities. Cardinality is
declared relative to that pair (`many_to_one` = first is many, second is one).
`rollup_safe` specifies which aggregations roll up safely in each direction.

Most relationships are **inferred** from FK references in `model.entities:`
blocks. Author an explicit `graph.relationships:` entry only when you need a
non-default rule:

- Per-direction rollup safety
- SCD2 `temporal_validity:`
- Custom `cardinality:` override
- `allowed_directions:` restriction

## Models

A model declares one warehouse table or mart, the entities it exposes, the
columns the planner can use, and the measures attached to its grain.

```yaml
# models/order_items.yml
model:
  id: order_items
  label: Order Items
  relation: shop_order_item
  # No grain: — graph.entities.order_item.model: order_items marks this
  # model as the primary home of order_item.

  entities:
    order_item: {}                      # primary
    order: {}                           # FK reference
    product: {}                         # FK reference

  times:
    ordered_at:
      column: ordered_at
      kind: timestamp
      class: event_time
      supported_grains: [day, week, month, quarter, year]
      default: true

  dimensions:
    quantity:
      kind: integer
    item_status:
      kind: categorical

  measures:
    line_revenue_usd:
      label: Line Revenue (USD)
      kind: aggregate
      expr: line_total_cents / 100.0
      default_agg: sum
      accumulation: { kind: flow }
      value_type: currency

    item_count:
      label: Item Count
      kind: entity_count
      entity_key: order_item_id         # the key COLUMN, not the entity name
      accumulation: { kind: event }
      value_type: count
```

### `model.entities:` — explicit declaration of exposed entities

Required on every model. Lists which entities the model exposes. Defaults bind to
the entity's canonical column from `graph.entities.<x>.key:`. Override per entity
with `expr:` when the model's column name differs:

```yaml
model:
  id: order_renamed_columns
  relation: shop_order
  entities:
    order: { expr: ord_id }                  # primary, column renamed
    customer: { expr: cust_id }              # FK, column renamed
```

The **primary entity** of a model is resolved without authoring `grain:`:
`graph.entities.<x>.model:` names the model that is the primary home of each
entity (the jaffle and tpch packages author it this way), and the row grain is
derived from that entity's canonical key. Single-file packages may instead pin
the primary by authoring `grain:` — the entity whose key matches the grain is
primary, and all entities whose keys are in a compound grain (`grain: [date_id,
account_id]`) are co-primary. Under `schema_strict`, directory packages reject
`grain:` authored alongside an `entities:` block ("Drop 'grain:' — it's derived
from the primary entity's key"); single-file packages accept both.

### `bridge: false` — junction tables

Set `bridge: false` on the entities block when the model is a junction or partial
bridge that should not be auto-used as a multi-hop join path. Queries within the
model still work; the planner just won't route through it.

```yaml
model:
  id: customer_segment_membership
  relation: shop_customer_segment
  entities:
    bridge: false
    customer: {}
    segment: {}
```

Use cases: junction/mapping tables, denormalized snapshots that shouldn't be
joined to live data, partial bridges where the data isn't complete enough for
arbitrary multi-hop traversal.

### `times:` — temporal roles

The `times:` block key IS the temporal role. The backing date/timestamp dimension
is auto-created from `column:`. `default: true` replaces the separate
`default_time:` field.

```yaml
times:
  ordered_at:
    label: Order time
    column: ordered_at
    kind: timestamp
    class: event_time                          # event_time | calendar_time | as_of_time | state_time
    supported_grains: [day, week, month, quarter, year]
    timezone: UTC
    default: true
```

`class:`, `supported_grains:`, and `default_query_axis:` are load-bearing — the
planner uses them to decide alignment and pick implicit time axes.

### Dimensions

Only behavioral dimensions are authored. Key dimensions auto-create from
`graph.entities.<x>.key`. Date/timestamp dimensions auto-create from `times:`
blocks.

```yaml
dimensions:
  status:
    label: Order Status
    kind: categorical
    domain: [placed, shipped, delivered, cancelled]

  is_promo:
    kind: boolean
```

`domain:` is a **list** of allowed values — scalars, or `{value, label}` entries
when you want display labels:

```yaml
domain:
  - value: jaffle
    label: Food
  - value: beverage
    label: Drink
```

Do not write the mapping form `domain: { values: [...] }` — the loader reads
`domain:` as a list, so a mapping silently degrades into its keys (a single
bogus `values` allowed-value).

Supported `kind:` values: `categorical`, `boolean`, `integer`, `continuous`,
`number`, `percent`, `currency`, `date`, `timestamp`. `kind: id` is no longer
authored — entity keys are auto-created from `graph.entities.<x>.key`.

### Measures

Each measure declares the explicit triple (`kind`, `accumulation`, `value_type`)
plus an aggregation expression.

```yaml
measures:
  revenue_usd:
    label: Revenue (USD)
    kind: aggregate
    expr: amount_usd                  # column or scalar expression
    default_agg: sum                  # default the API uses if no override
    rollup: additive                  # optional physical-variant routing hint
    accumulation: { kind: flow }
    value_type: currency
    disallowed_aggregations: [median] # subtract from accumulation-derived allowed set

  order_count:
    label: Order Count
    kind: entity_count
    entity_key: order_id              # the key COLUMN (graph.entities.order.key)
    accumulation: { kind: event }
    value_type: count

  active_subscribers:
    label: Active Subscribers
    kind: entity_count
    entity_key: customer_id           # the key COLUMN, not the entity name
    accumulation: { kind: stock, snapshot: end_of_period }
    value_type: count
```

`entity_key:` on `kind: entity_count` measures names the **key column** declared
in `graph.entities.<entity>.key` (e.g. `order_id`), not the entity name
(`order`). The loader treats the value as a literal column reference.

`accumulation:` is always object form: `{ kind: flow }`, `{ kind: event }`,
`{ kind: population }`, or `{ kind: stock, snapshot: end_of_period }`. The strict
enum is `{flow, stock, event, population}` — anything else is rejected.

`default_agg:` (the new name for `agg_function:`) is the default aggregation the
API uses if the caller doesn't specify. The accumulation class drives the
default-allowed aggregation set; `disallowed_aggregations:` subtracts from it.

`rollup:` is optional and only affects physical-variant routing. Use
`rollup: additive` when the measure can be safely summed from a lower-grain
rollup, or `rollup: precomputed` when the variant stores the final value for
the requested grain. Unsupported or missing rollup semantics force the planner
back to the raw model relation.

## Metrics

Metrics codify governed access patterns. Each metric carries a `kind:` that
determines the required fields.

### Common kinds — direct named fields

```yaml
metrics:
  # kind: aggregate — publish a measure
  revenue_usd:
    label: Revenue (USD)
    description: Total revenue. Codified for stable reference.
    kind: aggregate
    measure: revenue_usd
    value_type: currency

  # kind: ratio — direct numerator / denominator
  aov_usd:
    label: Average order value (USD)
    description: Revenue per order.
    kind: ratio
    numerator: revenue_usd
    denominator: order_count
    null_behavior: null_if_zero       # default
    value_type: currency
    time: ordered_at

  # kind: cumulative — running total over time axis
  cumulative_revenue_usd:
    label: Cumulative Revenue (USD)
    kind: cumulative
    measure: revenue_usd
    value_type: currency
```

In **direct named fields** (`measure:`, `numerator:`, `denominator:`), references
use package-relative keys (`revenue_usd`) — the loader resolves them. Inside an
**`expression:` AST** the rule differs: measure references must be fully
qualified (`measure: measure.shop.revenue_usd`); a package-relative measure key
there fails validation with `OBJECT_NOT_FOUND` ("Unknown measure
'revenue_usd'"). Metric references inside an AST (`{kind: metric, metric:
revenue_usd}`) still resolve package-relative.

`value_type:` is required on every metric. Default is `number` only when nothing
is authored.

### Filtered aggregates — author via the `expression:` AST

`kind: aggregate` direct fields don't include a `filter:` parameter — the
direct surface only publishes a measure. To filter the aggregate (e.g.,
"orders by repeat customers only"), author the AST under `expression:`
with `kind: aggregate` and a nested `filter:` block. The loader keeps the
AST shape verbatim:

```yaml
metrics:
  repeat_customer_orders:
    label: Repeat customer orders
    description: Orders placed by customers who have more than one lifetime order.
    kind: aggregate
    value_type: count
    temporal_role: temporal_role.shop_order_time
    expression:
      kind: aggregate
      measure: measure.shop.order_count
      aggregation: count_distinct
      filter:
        kind: metric_predicate
        scope_mode: contextual
        input:
          kind: aggregate
          measure: measure.shop.lifetime_order_count
          aggregation: max
        op: ">"
        value: 1
```

The buried `expression:` form is the canonical surface for filtered
aggregates. There is intentionally no top-level `filter:` direct field on
`kind: aggregate` — once a filter is involved, the metric needs the AST's
expressive power (multiple filter kinds: dimension, metric_predicate,
boolean composition).

### Long-tail kind — `derived` (expression AST)

For arbitrary formulas, `kind: derived` keeps the existing AST authoring path:

```yaml
metrics:
  margin_pct:
    label: Gross Margin (%)
    description: (revenue − cogs) / revenue
    kind: derived
    value_type: percent
    expression:
      kind: arithmetic
      op: divide
      left:
        kind: arithmetic
        op: subtract
        left:  { kind: metric, metric: revenue_usd }
        right: { kind: metric, metric: cogs_usd }
      right: { kind: metric, metric: revenue_usd }
```

### Per-kind authoring shape

| `kind:` | Direct named fields | Notes |
|---|---|---|
| `aggregate` | `measure: <key>` | publish a measure as a metric |
| `ratio` | `numerator: <key>`, `denominator: <key>`, `null_behavior:` | default `null_if_zero` |
| `cumulative` | `measure: <key>`, optional `window:` | running total |
| `prior_period` | `measure: <key>`, `period:` | comparison value at prior period |
| `period_to_date` | `measure: <key>`, `period:` | MTD / QTD / YTD |
| `rolling` | `measure: <key>`, `window:` | trailing window |
| `semi_additive` | `measure: <key>`, kind-specific options | applies measure's snapshot policy |
| `derived` | `expression: <AST>` | long-tail case; full AST |
| `conversion` | kind-specific options | event-pair conversion with windows |

The expression AST stays the runtime representation — the loader translates
direct named fields into the equivalent AST shape. You only write the AST for
`kind: derived` and `kind: conversion`.

### `as:` — preserve a public ID

The mapping key (e.g., `revenue_usd`) seeds the auto-derived ID
(`metric.shop.revenue_usd`). When external systems hard-code a different ID and
you don't want to break them while renaming the local key:

```yaml
metrics:
  revenue_usd:                            # new local key (clearer)
    as: metric.shop.gross_revenue_usd     # preserve the old ID external systems still call
    label: Revenue (USD)
    kind: aggregate
    measure: revenue_usd
    value_type: currency
```

`as:` does not cross namespaces — single-package authoring only. The validator
rejects `as:` whose namespace doesn't match `package.namespace` and warns when
`as:` produces an ID identical to the auto-derived one (use is unnecessary).

## Validation profile

When `schema_strict: true` is set on the package, the loader rejects the
authoring forms below with clear errors and migration pointers. These raw-YAML
checks run for directory packages; single-file packages get the compiled-config
checks but skip the raw-YAML strict pass (which is why the `init` starter can
author `grain:` alongside `entities:`).

| Rejected | Use instead |
|---|---|
| `id:` on semantic objects (graph entities, dimensions, measures) | Auto-derived from `namespace + key`; use `as:` only to preserve a public reference. Does NOT apply to model files — `model.id:` is the model's identity field in the directory layout (in single-file form, an authored model `id:` is only rejected when it differs from the `models:` mapping key) |
| `name:` matching the auto-derived value | Remove — auto-derived from key |
| Duplicate date/timestamp dimension when `times:` covers the same column | Drop the dimension; loader auto-creates it |
| `accumulation: stock` + sibling `snapshot_policy:` | Nested `accumulation: { kind: stock, snapshot: end_of_period }` |
| Model-level `entity:` (singular) + `keys.foreign:` + `joins:` blocks | `model.entities:` block; explicit overrides in `graph.relationships:` |
| Authored `model.grain:` alongside an `entities:` block (directory packages; single-file packages accept both) | Derived from the primary entity's key via `graph.entities.<x>.model:` |
| Names appearing in any entity's `disallowed_names:` | Use the canonical column or `expr:` rename |
| `accumulation:` value not in `{flow, stock, event, population}` | Use the canonical enum |
| Metric without `value_type:` | Always declare; default to `number` only when intentional |
| Buried `expression:` AST on metric kinds with direct named fields | Use direct fields (`kind: derived` and `kind: conversion` keep the AST) |
| `dimension.preferred_filter_ops` | Drop — metadata-only, no planner gating |
| `measure.clock_variants`, `comparison_peers`, `preferred_companion_metrics` | Drop on measures — metadata-only, no planner gating. (`preferred_companion_metrics` is allowed on metrics as advisory governance metadata; companion-metric relationships are too volatile to lock in at the measure layer.) |
| `topics:` on any object | Drop — no validation, no scaling pattern |
| `policy.kind: plan_constraint` | Drop — runtime no-op (the real kinds are `package_release`, `object_visibility`, `object_access`, `protected_object`, `metric_constraint`) |

Warnings (advisory only):

- `as:` used where the resulting ID matches the auto-computed one (use is
  unnecessary).
- Package omits `package.environments`.
- A public measure or curated metric omits `meta.owner_team`,
  `meta.review_priority`, or `meta.change_risk` (see
  [`package.environments` and governance `meta:`](#packageenvironments-and-governance-meta)).
- A measure omits an explicit `default_temporal_role` while declaring
  compatible temporal roles.

## Path-finding behavior (entity hopping)

When a query asks for "X per Y" where X is a measure on one model and Y is a
dimension on a different entity, the planner walks the inferred entity graph for
the shortest/most-complete path. This is automatic — most packages never author
join paths.

- "orders per customer": the orders model has both `order_id` (primary) and
  `customer_id` (FK). The planner uses that table directly.
- "items per customer": no single table has all three columns. The planner walks
  `order_item → order → customer` via inferred relationships.
- "items per customer" when a denormalized table contains all three: the planner
  prefers the direct table over the multi-hop path
  (`path_preference` on `RelationshipConfig` handles this).

Long chains are first-class: a measure can be grouped or filtered by a
dimension four relationships away (`line_item → order → customer → city →
region`), with each hop cardinality-checked. Every hop must be `N:1`/`1:1` in
the traversal direction (or carry a declared rewrite, e.g. `rollup_safe`
reverse aggregations or `temporal_validity`); anything else is a structured
refusal, never a silently fanned-out number.

### `graph.path_policy:` — hop ceiling

Path enumeration is bounded at 4 relationships by default. Raising it is an
explicit author decision:

```yaml
# graph.yml
graph:
  path_policy:
    max_hops: 6        # 1–8; default 4
```

A query that needs a longer chain than the ceiling fails with
`PATH_NOT_FOUND` and `details.reason: hop_limit_exceeded` plus
`details.reachable_at_hops`, so "the chain exists but is too long" is
distinguishable from "no relationship chain exists at all".

### `graph.path_preferences:` — pinning a route

When two routes reach the same entity (role-playing foreign keys are the
classic case: `order.ship_city_id` vs `order.customer → customer.city_id`),
the routes have different *meanings*, and hop count alone must not decide
which one a question gets. Pin the route per entity pair:

```yaml
# graph.yml
graph:
  path_preferences:
    - source_entity: line_item
      target_entity: region
      relationship_path:
        - relationship.line_items_order
        - relationship.orders_customer
        - relationship.customers_city
        - relationship.cities_region
```

Pinned paths are validated at load time (unknown relationships, broken
chains, and disallowed traversal directions are `INVALID_CONFIG`), and the
fanout safety analysis still applies to the pinned route.

Three guard rails back this up at query time:

- **`PATH_ALTERNATES_UNPINNED` warning** — emitted when hop count alone
  decided between routes with different hop counts and the author expressed
  no preference (no `path_preference` on any involved relationship, no
  `path_preferences` pin). Adding a shortcut relationship to a package can
  silently re-route existing queries; this warning is the tripwire.
- **`AMBIGUOUS_PATH` error** — two routes with identical hop count and
  preference score refuse to compile rather than pick arbitrarily.
- **`PATH_JOIN_CONFLICT` error** — one query needs the same physical table
  through two different relationships (e.g. region pinned to the home-city
  route while city resolves via the ship-to shortcut). One table instance
  cannot serve both semantics, so the compiler refuses with both routes
  named. Fix by pinning every affected target to a consistent route, or by
  modeling the second role as its own entity over a dedicated relation.

### `hop_profile` — observing entity hops

Every compile/query response carries a `hop_profile`: the root entity, the
chosen relationship chain per target entity with per-hop direction /
cardinality / safety, the hop ceiling, and `long_hop_targets` (targets 3+
hops out). Operators can log this to find questions that repeatedly cross
many entities — those are the candidates for a shortcut relationship, an
authored `aggregate_relations:` rollup, or physical colocation in the
warehouse.

## Physical variants and aggregate routing

Use `model.variants:` when one semantic model is physically stored at multiple
time grains, such as transaction, daily, weekly, and monthly tables. The model
continues to own the measures, dimensions, entities, and default time role. Each
non-transaction variant declares what the rollup table covers and which columns
or dimensions differ from the transaction table.

The loader normalizes each eligible non-transaction variant into an internal
`AggregateRelationConfig`. Query compilation can then route a compatible measure
leaf to the rollup relation instead of the raw relation while preserving the
same public measure and dimension IDs.

```yaml
# models/orders.yml
model:
  id: orders
  relation: order_fact
  default_variant: tx
  grain: [order_id]
  entities:
    order: {}
    customer: {}
    store: {}
  times:
    ordered_at:
      column: ordered_at
      kind: timestamp
      class: event_time
      default: true
  dimensions:
    store_id: { kind: categorical }
    customer_id: { kind: categorical }
  measures:
    revenue_usd:
      kind: aggregate
      expr: order_total_cents / 100.0
      default_agg: sum
      rollup: additive
      accumulation: { kind: flow }
      value_type: currency

  variants:
    tx:
      relation: order_fact
      grain: { time: transaction, entities: [order] }
      covers: inherit_all

    monthly:
      relation: order_monthly
      grain: { time: month, entities: [store] }
      time: { role: ordered_at, column: month_start }
      excludes:
        dimensions: [customer_id]
      columns:
        store_id: store_id
        revenue_usd: revenue_usd
      eligible_time_grains: [month, quarter, year]
      selection: { priority: 50 }
      equivalence: { kind: exact }
      source: default
```

Routing is conservative in the MVP:

- `source` must be `default`. Cross-warehouse routing is intentionally a future
  opportunity, not current behavior.
- `equivalence.kind` must be `exact`.
- The query must declare a time grain, and that grain must be listed in
  `eligible_time_grains`.
- The rollup grain must not be coarser than the requested query grain. A monthly
  table can answer month, quarter, or year queries, but not day queries.
- Every selected measure must have a column in the variant. Additive and
  precomputed rollups are supported; non-additive rollup semantics fall back to
  raw.
- Every grouped or filtered dimension must be covered by the variant. If a
  query groups by `customer_id` and the monthly table excludes that dimension,
  the planner scans the raw relation.
- Query-time `metric_predicate` shapes do not route through variants yet.

Strict config validation checks the `variants` shape, nested keys
(`grain`, `time`, `excludes`, `selection`, `equivalence`), value-list fields,
inheritance cycles, unknown semantic references, and the MVP `source: default`
constraint. Authors can use `inherits_from` on variants to share common fields
between daily, weekly, and monthly rollups, then override only the differences.

## Default-time cascade

A model's default `times:` entry (the one with `default: true`) cascades to its
measures. Measures only need an explicit `time:` field when they override the
model default:

```yaml
model:
  times:
    ordered_at:
      column: ordered_at
      class: event_time
      default: true
  measures:
    order_count:
      kind: entity_count
      entity_key: order_id     # inherits time: ordered_at
    refund_amount:
      kind: aggregate
      expr: refund_usd
      default_agg: sum
      time: refund_recognized_time   # explicit override
```

Metrics do NOT inherit `default_time` from a model — they remain explicit because
metrics often span entities.

## Examples and tests

Package-local review assets:

- `examples/` — runnable example queries surfaced through discovery and inspect.
- `tests/` — package-local semantic assertions (`query_returns_columns`,
  `query_row_count_bounds`, `validate_fails_with_code`, `explain_contains`,
  `query_matches_snapshot`, `metric_equals_query`). The test runner walks
  `<package>/tests/*.yml` directly.

Both are loaded from **directories only** — the package root's `examples/` and
`tests/` folders (for a single-file package, the directory holding the
`package.yml`). Top-level `examples:` or `tests:` blocks written inside
`package.yml` are silently ignored by `run-examples` and `test-package`; keep
them in sibling files.

An `examples/` file maps example IDs to a question, a Query IR, and an expected
shape (`uv run semantic-rails run-examples` executes them):

```yaml
# examples/core.yml
examples:
  top_stores_by_revenue:
    question: Top stores by revenue
    query:
      version: 1
      select:
        - expression: {measure: measure.shop.revenue_usd}
          as: revenue_usd
      group_by: [dimension.shop_store_name]
      order_by:
        - field: revenue_usd
          direction: DESC
      limit: 5
    expected_shape:
      columns: [dimension.shop_store_name, revenue_usd]
      min_rows: 2
      max_rows: 2
```

A `tests/` file maps test IDs to a `kind:` and its kind-specific assertion
fields (`uv run semantic-rails test-package` runs them):

```yaml
# tests/core.yml
tests:
  monthly_revenue_columns:
    kind: query_returns_columns
    query:
      version: 1
      select:
        - expression: {measure: measure.shop.revenue_usd}
          as: revenue_usd
      time:
        temporal_role: temporal_role.shop_order_ordered_at
        grain: month
      order_by:
        - field: time
          direction: ASC
      limit: 5
    columns: [temporal_role.shop_order_ordered_at__month, revenue_usd]

  duplicate_alias_rejected:
    kind: validate_fails_with_code
    query:
      version: 1
      select:
        - expression: {measure: measure.shop.order_count}
          as: value
        - expression: {measure: measure.shop.revenue_usd}
          as: value
      limit: 5
    code: DUPLICATE_OUTPUT_ALIAS
```

See `configs/semantic_rails/jaffle_shop/examples/core.yml` and
`configs/semantic_rails/jaffle_shop/tests/core.yml` for the full worked set,
including `query_matches_snapshot` (`expected_rows:`) and
`query_row_count_bounds` (`min_rows:` / `max_rows:`).

## Validation commands

`--package` only accepts the **registered** package IDs shipped under
`configs/semantic_rails/` (`jaffle_shop`, `tpch_sf1_showcase`, ...). For a
package you are authoring anywhere else, use `--path` — point it at the
`package.yml` file for single-file packages, or at the package directory for
the split layout:

```bash
uv run semantic-rails parse-config --path ./my_pkg/package.yml
uv run semantic-rails validate-config --path ./my_pkg/package.yml --quiet
uv run semantic-rails run-examples --path ./my_pkg/package.yml
uv run semantic-rails test-package --path ./my_pkg/package.yml
uv run semantic-rails check --path ./my_pkg/package.yml --artifact dist/my_pkg.semantic-rails.tar.gz
uv run pytest -q tests/semantic_rails -n auto
```

Use `semantic-rails check` as the default GitHub PR gate.

The discovery and query surface takes `--path` too, so a custom package gets
the same agent loop as a registered one: `catalog`, `discover`, `inspect`,
`valid-values`, `plan`, `build-options`, `validate`, `compile`, `query`, and
`mcp stdio` / `mcp http` all accept it (query-error recovery hints reference
these commands by name). The config/CI verbs — `parse-config`,
`validate-config`, `check`, `build-package`, `run-examples`, `test-package`,
`diff-package`, `impact-report`, `promote-package`, `doctor` — accept `--path`
as a mutually exclusive alternative to `--package`. Only `serve` and the
`segment-*` commands remain registry-only.

`build-package` (and `check --artifact`) bundles a single-file package's
referenced seed SQL (`package.seed.source` / `post_sql`) plus its sibling
`examples/` and `tests/` directories, so a check-passing artifact can hydrate
its own warehouse. If a declared seed asset is missing on disk, the build fails
with a structured `INVALID_CONFIG` error (`details.missing_assets` +
`recovery_hints`) instead of shipping an artifact that cannot self-hydrate.

### Semantic collision warnings

`validate-config` flags pairs of measures, metrics, dimensions, or
segments whose label / name / search_terms overlap enough that the MCP
`discover` ranker may surface them indistinguishably. Each warning
names both ids and the specific overlap, with a one-line remediation
hint:

> semantic collision risk between measure measure.shop.revenue_a and
> measure measure.shop.revenue_b — identical label; shared search_terms
> ['revenue', 'sales']. MCP `discover` may rank them indistinguishably.
> Differentiate `label`, `name`, or `search_terms` on one of them.

The detector fires conservatively — it skips ID-typed dimensions
(`semantic_kind: id`, auto-generated from entity primary keys) and
measure ↔ auto-published-metric pairs (same `name` is by design).
A zero-collision package is one where every object is distinguishable
by label/name/search_terms from every other in its class. Treat any
flagged pair as an authoring debt: tighten the label or differentiate
search_terms so an LLM-driven agent can pick the right object without
context.

## Reference

The sections above are the source-controlled reference for every supported
authoring field, default, validation rule, and runnable example.
