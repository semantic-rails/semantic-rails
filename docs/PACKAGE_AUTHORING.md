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
  7. Calendar — the date spine rolling, prior-period and growth metrics need
```

Use `author metric` (or any other kind) to skip the first menu. The flow lists
valid package objects where a reference is required, recommends the common
choice, shows the target file and YAML before writing, calls out exact or
similar existing definitions as soon as you name the object (choosing other
wording asks for the key and label again), and defaults every create/update
confirmation to No. Press Ctrl-C, or type `cancel` at a text prompt or list, to
stop without writing (in an arrow-key list, `cancel` picks an option containing it,
such as "Cancelled orders"). In arrow-key pickers a suggested default is a
placeholder: typing replaces it. Managing an existing object starts every prompt at its saved
value, even a value the menu does not otherwise list, so pressing Enter
throughout leaves it as it was. Changing a measure's default aggregation warns
and names the metrics whose numbers change. Successful edits are parse-validated
and can be restored with `undo` during the same REPL session. A failed parse
restores the original files automatically.

`author model` lists the tables in the package's DuckDB file. When the seed files
changed after that file was built, it prints the `STALE_SEED_DATABASE` warning and
its rebuild command first. It refuses a new typed name that is neither a table in
that file nor a relation pipeline. It doesn't pre-tick `_cents` columns as money
amounts: as currency they would print cents as dollars. Publish them in dollars
with a measure such as `amount_cents / 100.0`.

Rolling windows, prior periods and growth fill empty periods from a calendar. At
query time a package without one uses the engine's implicit Gregorian calendar
(QUERY_IR_SCHEMA "Which calendar fills"); author one for fiscal or custom periods,
Sunday weeks, or a ClickHouse package. `author metric` offers these metrics only
once the package has a calendar. A calendar
is a model whose graph entity has `kind: time`: one row per day in a `date_day`
column, plus `week_start`, `month_start`, `quarter_start` and `year_start` date
columns for the coarser units (see `models/core/calendar.yml` in the bundled
package). An authored default calendar replaces the implicit one for every grain,
so a grain whose column it lacks is refused. `author calendar` writes it from your
date-spine table.

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

Without `--package` or `--path`, a command uses the package directory it runs in
(or a parent), then this profile. With neither, at an interactive terminal, `repl`
and bare `semantic-rails` open a home screen, and `ask`, `ls` and `project` offer
the bundled `jaffle_shop` sample package (default No); everything else stops and
lists how to choose a package. Scripts that want the sample pass
`--package jaffle_shop`.

The home screen, also the REPL's `home` command, offers:

- **Open**: a package found in or below the working directory (a bounded scan that
  skips hidden, build and dependency folders), or any folder by path.
- **Create**: the starter package `init` writes, in a new folder.
- **Import a dbt project**: pick models from a dbt-duckdb project's `target/`
  (after `dbt build` and `dbt docs generate`). It creates a package over DuckDB data
  that dbt builds (`seed: {kind: external}`) and imports the models as
  `import_dbt_project` does (see [ARCHITECT_MCP.md](ARCHITECT_MCP.md#dbt-projects)).
  Point the dbt profile's `path` at the package's `data/<package_id>.duckdb`. A
  failed or cancelled import keeps nothing.
- **Try the bundled sample**, labelled as sample data.

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

`defaults.observation_scope` sets where a sum or count is judged to have data, so that a
group with no rows reads `0` rather than `NULL`. `dataset` (the default) judges it across the
measure's own rows under its authored conditions and the caller's row filters: if store 5 sold
no apples, "apples at store 5" reads `0`, as in a breakdown by store. `query` judges it inside
each query's filters, so the same question reads `NULL` with `NO_DATA_IN_SCOPE`; experiments
that must not mistake a filtered-out population for zero want it. A query's own
`observation_scope` overrides it, and any other value is `INVALID_CONFIG`. See
[Empty groups](QUERY_IR_SCHEMA.md#empty-groups-null-or-0).

### DuckDB seeds and externally built databases

`seed.kind` says who builds the file at `default_db`:

- `sql_script` (a SQL file) or `csv_dir_duckdb` (a directory of CSVs plus
  optional `post_sql`): the runtime builds the database only when the file is
  missing. It records package provenance and a content hash of the seed files
  inside the new file. When those files change later, queries and runtime
  validation return a `STALE_SEED_DATABASE` warning with the command that
  deletes the file; the next run rebuilds it from the current seed. Publication is
  atomic and never overwrites a file another process created in the meantime.
  If the filesystem cannot publish without an overwrite (for example one
  without hard links on POSIX), creation fails with `INVALID_CONFIG`; build the
  database explicitly on a supported filesystem before starting the runtime.
- `external`: another tool (for example `dbt build` with dbt-duckdb) builds and
  owns the file. It takes no `source` or `post_sql`, and the runtime only reads
  the file: a missing file is an `INVALID_CONFIG` error.

```yaml
package:
  warehouse: duckdb
  default_db: data/warehouse.duckdb   # the file dbt-duckdb writes
  seed: { kind: external }
```

Relations may be schema-qualified (`relation: main_marts.fct_orders`). The
validation probe checks the relations the package reads, including views and
stored sources of relation pipelines. If an existing database lacks any of
these relations, the runtime raises `INVALID_CONFIG` with
`details.missing_relations` and leaves the file intact, regardless of its seed
provenance. Have the owner (such as dbt) build the missing relations. For a
disposable database generated from this package's seed, stop its users, back
up any data you need, and explicitly remove the database file before
restarting so bootstrap can create a fresh one. The former
`SEMANTIC_RAILS_ALLOW_DB_RESEED` setting does not enable automatic replacement.

The operator-invoked `seed_db` and CSV loader helpers still replace an existing
file, but refuse to publish while its `.wal` recovery log exists. Close and
checkpoint the database before invoking either helper. Keep other writers
stopped through publication: the WAL check cannot prevent a writer from
creating a new log immediately after it runs.

SQL seed sources and CSV `post_sql` files accept LF or CRLF line endings.
CRLF bytes inside string literals are preserved as authored, without normalization to LF.
A bare carriage return refuses the script before any of its statements execute
with `INVALID_CONFIG`, `details.reason: bare_carriage_return_sql_script` and
`details.file` naming the SQL file. Save the file with LF or CRLF and retry.

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

Three connection kinds are supported:

- `snowflake_adbc` — opt-in Arrow connector with password or PKCS #8 key-pair
  authentication, bound row-filter parameters and exact decimal results. See
  [installation and connection options](ADDING_A_DIALECT.md#experimental-snowflake-profile).
- `snowflake_cli` — uses a configured Snow CLI profile by name.
- `snowflake_native` — direct connector via env-var indirection (account, user,
  password, etc. read from environment variables).

Literal secrets in YAML are rejected.

For `snowflake_adbc`, driver names, shared-library paths and manifests belong to
the runtime operator's environment; package options selecting them are rejected
with `INVALID_CONFIG`. Temporal values with nonzero sub-microsecond precision
refuse with `RESULT_VALUE_UNSUPPORTED`; out-of-range nanosecond timestamps refuse
with `QUERY_EXECUTION_ERROR`.
Out-of-range timestamps such as `9999-12-31` refuse; cast those columns to
`TIMESTAMP_*(6)` or `DATE` in the model.
With `use_high_precision=true`, scale-0 `NUMBER` columns come back typed `decimal`.
Account and user may be literals (`account`, `user`) or env-indirected
(`account_env`, `user_env`); the optional key passphrase may use
`private_key_passphrase_env` or `private_key_passphrase_file`.
Passphrase files preserve whitespace except for one optional trailing LF (`\n`)
or CRLF (`\r\n`); other `*_file` secrets still strip surrounding whitespace.
Package loading checks both locators, exactly one password or key source, and a
key source when a passphrase is authored, without reading credentials;
`connection.name` is refused for this kind.

### Native adapter timeouts

Postgres, ClickHouse, Databricks, Snowflake native, BigQuery, and Athena
connections accept `connect_timeout_seconds` and `read_timeout_seconds` in
`package.connection.options`. Both must be positive integers. The defaults are
10 seconds for connecting and 65 seconds for network reads or query waiting.
Set a larger read timeout when queries normally take longer. Postgres and
Snowflake native also accept `statement_timeout_seconds`; server statement
limits remain opt-in. An explicit `"0"` preserves the server default on
Postgres and disables the session limit on Snowflake, as before. Without an
authored positive Postgres limit or any Snowflake limit, role/user/account
defaults are preserved.
An explicit statement timeout raises the default read timeout to at least five
seconds beyond it. Snowflake named profiles retain their inherited login,
network/socket and session settings unless the package explicitly overrides
numeric timeout options. With a named profile, a nonempty authored `query_tag`
is refused with `INVALID_CONFIG` before connecting; configure `QUERY_TAG` in
the named profile instead. Inherited `QUERY_TAG` and `TIMEZONE` remain intact
when a numeric timeout is overridden. Direct connections still pass authored
tags through the connector's `session_parameters`.

The drivers apply these limits differently: ClickHouse bounds connection and
HTTP send/receive time; Snowflake bounds login, network, and socket operations;
BigQuery bounds query submission and result waiting; Athena bounds API calls
and query polling. Databricks exposes one socket timeout for connection and
reads, so the larger configured value applies to both. Postgres uses libpq's
connection timeout and TCP keepalives; libpq has no separate socket read
deadline, and healthy queries without an authored server timeout can continue.
These network limits apply per driver operation/attempt; driver retries and
Databricks polling can extend total elapsed time. MotherDuck and Snowflake CLI
are outside these client-wait defaults.

Per-request `limits.statement_timeout_ms` sets each supported warehouse's
statement deadline. ClickHouse, Databricks, Snowflake native, BigQuery and
Athena client waits accommodate a longer request with a five-second margin.
BigQuery sets a server job deadline to the read wait when no request limit is
supplied and attempts cancellation if result waiting times out. Athena cancels
an unfinished query before returning its polling timeout; its request limit
remains best-effort, with the client polling margin and workgroup server limits.

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
each with an `id`, a `kind`, and (except for `package_release` and `row_filter`)
the `object_ids` it governs. Six kinds exist, each driving a different runtime
behavior:

Policy kinds and actions are a closed list in both default and strict validation.
Unknown kinds or unsupported actions fail to load with `INVALID_CONFIG`; a policy
constructed directly in Python is checked again before query binding and cache
lookup, and during policy evaluation. Release labels belong in `config.label`.

| Kind | Allowed action |
| --- | --- |
| `package_release` | omitted or `label` |
| `object_visibility` | `hidden`, `visible` (required) |
| `object_access` | `deny`, `redact`, `withhold_values` (required) |
| `protected_object` | omitted or `protected` |
| `metric_constraint` | omitted or `constrain` |
| `row_filter` | omitted |

The existing nested `config.action` and `config.visibility` aliases use the same
action checks. Action text is trimmed and lowercased; kind names must match exactly.

- **`package_release`** — labels the package's release status. `config.label`
  (e.g. `stable`, `preview`) surfaces in the package manifest and discovery
  metadata; it gates nothing by itself.
- **`object_visibility`** — hides matching objects from `catalog`, `discover`,
  and `inspect` for the scoped audiences/environments/roles. `action: hidden` is the
  useful value; a hidden measure cannot be discovered but a query that names
  it directly is governed by `object_access`, not visibility.
- **`object_access`** — enforced at query time. `action: deny` refuses the
  query with a structured policy error; `action: redact` executes but replaces
  the governed object's values in the result. An `aggregate_if` reads every
  dimension declared over the columns in its condition or value, including on
  the measure's own entity, as a `where` filter or `group_by` on them does. A
  matching `deny`, `redact` or `hidden` object policy on any such dimension
  refuses the query with `POLICY_DENIED` before SQL is rendered. Own-entity
  columns with no declared dimension remain allowed. While any `object_access`
  or `object_visibility` policy is declared, it may not read another entity's column
  that no dimension declares (`POLICY_DENIED`, reason `column_without_dimension`).
  `action: withhold_values` lets the scoped caller rank by a metric or measure without
  seeing its values ("the 3 biggest accounts in EMEA"); see below.
- **`protected_object`** — pins an object as protected in the named
  environments; `promote-package` and `impact-report` treat changes to
  protected objects as release-gated.
- **`metric_constraint`** — enforced at query time for scoped callers. It
  restricts how governed metrics or measures may be cut by `group_by`,
  `where`, temporal role, and metric-filter predicates.
  `allowed_metric_filter_entities` and `allowed_metric_filter_metrics` apply
  to compiler-resolved cuts in `metric_filters` and inside select or recipe
  expressions, including aggregate filters and scoped predicates. Measures,
  columns and dimensions count as their owning entities; referenced recipes
  count even when nested. The outer query's grouping, where, time axis and
  attachment joins are excluded from these filter dependency sets. Constant
  cuts have no object reads; unresolved cut bindings are refused under these
  allowlists. A cut in `metric_filters` counts for every governed object in
  the query. A cut inside a select or recipe expression counts only for the
  measures and recipes whose values it filters, so an unfiltered governed
  measure may sit beside a separately filtered one. When a cut has no single
  owner, or the governed object is read inside a cut, every cut in the query
  counts. `allow_metric_filters: false` refuses any cut that counts for the
  governed object.
  `allowed_temporal_roles` checks the query axis and the governed object's
  effective bucket and ordering roles, including expression roles and overrides.
  Grouping by a role's dimension remains subject to `allowed_group_by`.
- **`row_filter`** — limits a relation's rows to one customer (or other
  principal) by comparing a `dimension` with a trusted request `attribute`
  that the embedding host supplies (see [EMBEDDING.md](EMBEDDING.md)). The
  compiler adds `<column> = ?` to the relation's scan and the runtime binds the
  attribute's value as a parameter; values never enter SQL text. The dimension
  must be a plain `string`, `integer` or `boolean` column; an id-kind dimension
  needs `type:`. The policy takes no `object_ids`, `action` or operator. A request
  it applies to is denied if the attribute is missing or of another type, and
  so is any query outside the qualified family: the compiled statement must
  read one ordinary scan of the filtered relation, as its only relation. Engine-generated
  observation and coverage scans may repeat that relation; each receives the same policy
  filter. Joins, metric filters, calendar spines (prior-period comparisons, fill) and
  other second scans are refused, and rollups are not routed to. The zero-row
  data-coverage probe is skipped. Such a policy loads for any warehouse, but only
  DuckDB and Postgres execute these statements today; every other adapter
  refuses them. Postgres finalizes the placeholders as `$1`, `$2`, … before
  execution and binds trusted values through ADBC.
  An unscoped row filter applies to every request. A scoped one applies only
  when the request context carries the listed audience, environment or role,
  so a request without it is not filtered: scope by them only when the host
  always sets them from verified identity for end-user requests. A policy of
  another kind that carries `attribute:`, or a kind that is a near miss of
  `row_filter` (`row-filter`, `row_filters`, ...), fails to load rather than
  being ignored.


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
    allowed_temporal_roles: [temporal_role.shop_order_ordered_at]
    allow_metric_filters: false
    rationale: Sales and CSM revenue access is limited to store-level cuts.

  - id: policy.shop.own_orders
    kind: row_filter
    dimension: dimension.shop_order_customer_id
    attribute: customer_id
    rationale: Each customer sees only their own orders.
```

### Ranking by withheld values

```yaml
semantic_policies:
  - id: policy.revenue_rank_only
    kind: object_access
    action: withhold_values
    object_ids: [metric.revenue]
    roles: [sales]
    config: {max_rank: 10}
```

A `withhold_values` policy answers one shape: a grouped query that selects the governed
metric or measure directly (no wrapper, aggregation or time-role override) and names it
first in `order_by`, then every group key (and the time bucket) in the same direction, with
a `limit` of at most `config.max_rank` (an integer from 1 to 100; default 10). When
`order_by` names only the metric, the engine adds the group keys. Dimension filters and
time windows work as usual. The response's `rows` hold the group keys only: the metric's
column is removed from `rows`, `column_types` and `output_columns`. A top-level `withheld`
lists the withheld objects, and one `VALUES_WITHHELD` warning says the rows are ordered by
them. Because ties are ordered by the keys in the rank's direction, ascending is the exact
reverse of descending, so flipping it cannot tell a tie from a strict order. Every order
key has a portable NULL indicator in that same direction: NULLs sort first on ascending
and last on descending, for both the ranked value and nullable group keys.

Every other use is refused with `POLICY_DENIED`, `details.withheld_objects`, a `reason`
and a recovery hint naming the accepted shape, on validate, compile, execute and MCP:
selecting it in any expression, metric or measure that reads it (ratios, arithmetic,
comparisons, prior period, rolling and other windows), ordering by such an expression, a
`where`, `metric_filters` or threshold reading it, a segment defined on it, `export`, a
missing or larger `limit`, and other tie orders. Dependencies are the compiler's own, so a
derived metric cannot stand in for the withheld one. Compiled SQL still contains the
metric's expression; values come only from the warehouse and never appear in errors,
warnings or explain output. Data-dependent diagnostics exclude withheld outputs before
reading row values, including whether they are NULL; permitted outputs keep their
diagnostics. Any unresolved filter or threshold dependency refuses with reason
`withheld_unproven`. Resource-granted responses retain `withheld`, warnings naming only
granted objects, and the runtime's output descriptors after the withheld column is removed.
`valid-values` never anchors a live lookup on a withheld
measure, or on one a withheld metric reads. As with `deny`, a policy on a metric does not
govern its measures when they are selected directly: list them too.

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

Each graph entity must have a key, declared on the entity or through its own
model's `keys.primary:` or `grain:`. An explicit graph model binding makes that
entity the model's primary entity, regardless of the order of its `entities:` block. A
conflicting resolved `entity:` fails with `INVALID_CONFIG` naming both entities,
including for implicit bindings and bindings by model name. A
keyless entity cannot borrow a foreign entity's key: loading fails with
`INVALID_CONFIG` naming the entity and model before graph relationships are
translated. Each model can be the primary home of only one graph entity.

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
      temporal_validity:                        # <model relation name>.<column>
        valid_from: shop_customer_history.effective_from
        valid_to: shop_customer_history.effective_to
```

A query that joins into the table holding the window needs a `time`, so each row reads the
version valid at its time. Without one, a many-to-one hop into it is refused with
`FANOUT_UNSAFE`, naming the relationship and the entity, because joining every version would
count a row once per version. A hop out of that table (`customer_history → customer` here)
reads one version per row and needs no time. The validity predicate applies only when joining
into the versioned table: an outgoing lookup keeps the existing version's dimensions even
when the query clock is its `valid_to`, including a NULL end time on an open version.
A metric predicate is stricter: when the path from
its input to its entity, context or filters crosses a time-valid relationship in either
direction, it needs a time of its own (a contextual predicate takes the query's `time`).

Qualify the validity columns with the model's full `relation` name, including its schema or
catalog when present (for example, `analytics.shop_customer_history.effective_from`). The
relationship's declared window determines which side holds versions; schema qualification
does not make an outgoing lookup require a query time.

### `disallowed_names:` — explicit anti-pattern guard

Author the names that should NEVER appear as a column, dimension, or measure on
any model. The validator rejects any model that introduces a name in the list and
points to the canonical entity column or the `expr:` escape hatch for intentional
renames. This replaces heuristic near-duplicate detection — explicit and
configurable.

### Bidirectional relationships

Each `relationships:` entry is an unordered pair of entities. Cardinality is
declared relative to that pair (`many_to_one` = first is many, second is one).
`rollup_safe.reverse: [count_distinct]` permits the population-count rewrite
when traversing from the second entity to the first. Measures aggregate at their
own model's row grain; parent-rollup declarations are not supported.
To migrate existing packages, delete `subject_entity` and `aggregation_entity`
lines from `defaults.measure` and individual measures. Defaults are checked once
per package, with an error naming `defaults.measure.<key>` and the line to delete.
In `graph.relationships`, `rollup_safe` must be a mapping containing only `reverse`;
forward declarations, the former list form, and `rollup_safe_aggregations` in model
joins or relationship defaults fail loading with `INVALID_CONFIG` naming the authored location.
Model joins and `defaults.relationship` do not accept `rollup_safe` in any form;
declare reverse permissions in `graph.relationships` using `rollup_safe.reverse`.
A `rollup_safe` or `rollup_safe_aggregations` default is refused by key presence,
including `null`, even when the package has no relationships.
Authored model joins are checked before graph relationships override them;
removed keys are refused even when their value is `null`.

Every authored relationship must declare `entities: [source, target]` and attach
to graph models for both endpoints. An invalid or unattached entry fails loading
with `INVALID_CONFIG` naming the relationship; it is never silently omitted.

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
entity (the jaffle and tpch packages author it this way), and an omitted row grain
is derived from that entity's canonical key. Single-file packages may instead pin
the primary by authoring `grain:` — the entity whose key matches the grain is
primary. Under `schema_strict`, directory packages reject
`grain:` authored alongside an `entities:` block ("Drop 'grain:' — it's derived
from the primary entity's key"); single-file packages accept both.

Without an explicit graph model binding, the loader resolves the primary from
an authored singular `entity:`, then a matching grain, then an entity listed in
the model's `entities:` block whose name matches the model and which has no
explicit graph binding. It back-fills implicit graph bindings from the resolved
identity. Declaration order never selects the primary; loading fails with
`INVALID_CONFIG` if it cannot be resolved or two graph entities bind to one model.
If multiple entity keys match the grain, bind the model in the graph or set
`entity:`; otherwise loading fails with `INVALID_CONFIG` naming the model and
matching entities.
Two models claiming the same unbound graph entity also fail with `INVALID_CONFIG`
naming both models. An explicit graph `model:` binding fixes primary identity
independently of an authored `grain:`. That grain describes measure rows and may
differ from the entity key (for example, payment rows belonging to one receipt,
or snapshot rows keyed by their clock). It never replaces the bound entity's key.
Without an explicit graph binding, when `grain:` accompanies an `entities:` block,
it must match the resolved primary entity's `expr:` override or canonical graph
key, including when a singular `entity:` supplies the identity. When neither
declares columns, the check uses the model's own `keys.primary:`. If no key is declared,
its own model's grain can supply it. Empty `entities:` blocks retain the graph's
model-name default and still validate the grain.

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

Time is optional. A package without dates can omit every model's `times:` block.
Counts, sums, ratios, grouping, filters and lookups work without a time axis.
Explicit Query IR requests for time ranges, grains, windows, temporal overrides,
prior-period or cumulative expressions are refused with
`INVALID_TEMPORAL_ROLE`: the package declares no time. Declare a `times:` entry
before requesting time analysis. A `defaults.time.default_query_axis: true`
also requires a declared temporal role. On a package without time, every
natural-language plan with a draft returns `low_confidence`, retains its Query IR
and adds the same `INVALID_TEMPORAL_ROLE` warning: "This package has no time; check
the question doesn't ask for a time breakdown or window." This includes plain
catalogue questions and catalogue labels or dimension values containing time words.
For example, "monthly item count for the monthly category" retains the category
filter, but needs review before execution.
Architect and CLI scaffolds accept a blank
`time_column` to generate a package, seed, examples and tests without dates.

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

A measure is timed by the roles in its own `times:` list, or, when it lists none, by its
model's `default: true` time. A measure with neither has no clock: it answers without `time`
or grouped by a plain date dimension, but a query that puts it on a time grain (for example
`time: {temporal_role: ..., grain: month}`) is refused with `INCOMPATIBLE_TEMPORAL_ROLE`. Mark
the role `default: true` or list it under the measure's `times:`.

`timezone:` (default `UTC`) is the zone the role answers in. Every grain's
buckets, and a query's `start`/`end` bounds, are in that zone:

- A naive `TIMESTAMP` or a `DATE` column is read as stored. If it stores
  another zone's clock, name that zone in `column_timezone:` (for example
  `column_timezone: UTC` with `timezone: America/New_York`), and the engine
  converts it.
- A zone-aware column (`TIMESTAMP WITH TIME ZONE`) holds instants. On DuckDB,
  MotherDuck, DuckLake and Postgres, each query runs with the session time zone
  set to its time role's zone (UTC for a query without one), and only for that
  query. So these columns bucket and filter in the role's zone whatever the
  server's or machine's default. Leave `column_timezone:` off them. (MotherDuck
  gets the setting on its client connection; this hasn't been checked against
  the service.)
- Everything else zone-dependent in the query follows that zone too:
  - an authored `call` over a zone-aware value, such as `date_part('hour', …)`
    or a cast to `DATE`;
  - `now()` and `current_date`;
  - zone-aware values returned in rows, which are the same instants shown with
    that zone's offset.

  The rendered SQL doesn't show the zone. To reproduce an answer in a SQL
  console, set the session's `TimeZone` to it first.
- A query whose measures are bucketed on time roles in different zones runs in
  the zone of its `time.temporal_role`. It returns a `TIME_ZONE_NOT_APPLIED`
  warning that names the roles whose own zone it didn't use.
- The other warehouses don't do this yet, so there a zone-aware column follows
  the warehouse's own rules:
  - Snowflake `TIMESTAMP_LTZ` and Databricks `TIMESTAMP` use the session time zone.
  - Snowflake `TIMESTAMP_TZ` keeps each value's own offset.
  - Athena/Trino `timestamp with time zone` keeps each value's own zone.
  - ClickHouse `DateTime` uses the column's or the server's zone.
  - BigQuery buckets `DATETIME` values, so a `TIMESTAMP` column isn't bucketed in the role's zone.

  On those warehouses, store naive timestamps and declare their zone with
  `column_timezone:`.

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
    additive: true                    # false: already aggregated, never summed (see below)
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

An `aggregate` measure with `expr: "1"`, `default_agg: sum`,
`accumulation: { kind: flow }`, and `value_type: count` counts source rows.
Package checks treat literal values as constants with no column dependencies;
columns inside compound expressions are still checked against the warehouse.

`entity_key:` on `kind: entity_count` measures names the **key column** declared
in `graph.entities.<entity>.key` (e.g. `order_id`), not the entity name
(`order`). The loader treats the value as a literal column reference.

`accumulation:` is always object form: `{ kind: flow }`, `{ kind: event }`,
`{ kind: population }`, or `{ kind: stock, snapshot: end_of_period }`. The strict
enum is `{flow, stock, event, population}` — anything else is rejected.

A `stock` measure answers with each series' last snapshot in each period (its first,
with `snapshot: start_of_period`), then adds up the series. A series is the row key
without the clock's column, so a snapshot table's entity key is the series columns plus
the snapshot time: `key: [store_id, date_day]` for inventory per store per day, not a
surrogate such as `inventory_row_id` that is unique per snapshot row, and the series
columns must not be unique per row themselves (`[inventory_row_id, date_day]` passes the
check below but still sums). Give the snapshot time `class: as_of_time`.

For grouping, the snapshot is chosen per series per period first. A grouped attribute
stored on the snapshot rows, such as an account's plan that changes mid-week, is read
from that snapshot, so the series counts once, under the value it holds that day, and
the summed grouped rows add up to the ungrouped total. A `where` filter on the attribute
applies before the snapshot is chosen. The stock's own clock and calendar dimensions
instead split the period: grouped by the snapshot day, each day keeps its own snapshot.
Ratios whose numerator alone has extra conditions keep one snapshot per series per
time bucket.

- Grouping a stock by a date or timestamp attribute that is neither its ordering clock
  nor a calendar dimension is refused with `REWRITE_NOT_SUPPORTED`, with
  `details.reason: stock_grouped_by_date_attribute` and the dimension id. Group by the
  stock's clock or a calendar dimension instead.

- A stock whose key doesn't contain its clock's column gets a
  `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` parse warning: each key value counts as its own
  series, so two snapshots of one series in the same week are added together.
- If the key holds none of the stock's `as_of_time` clocks (in its `times:`), every query
  of the stock is refused with `INVALID_CONFIG`, whatever clock it's ordered by.
- Ordered by any other clock, a key that holds an as-of clock leaves it in the series, so
  each snapshot is its own series: those queries are refused too, with a
  `STOCK_SERIES_HOLDS_AS_OF_CLOCK` parse warning. Keep one as-of clock in the key and
  query the stock on it. An event-time column in the key, such as a cohort month, is part
  of the series and is fine on the as-of clock.
- A current-state table with one row per series (a customer's lifetime spend on the
  customers table) has the same shape and is right as keyed; the warning is expected there.
  Because the engine can't tell the two apart on an event- or state-time clock, each query,
  validate and compile answer from such a stock also carries the
  `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` warning (`segment_preview` returns no warnings yet).

`additive: false` marks an `aggregate` measure whose values are already aggregated
and must never be added together: a vendor's pre-counted distinct values (daily unique
visitors, a page's unique visitors over 14 days) or a stored ratio. Three pages with 3,
2 and 2 unique visitors can have 4 distinct visitors between them, not 7.

- A query that would sum more than one of its rows into an output row is refused with
  `ROLLUP_UNSAFE` (`details.unsupported_construct: non_additive_sum`). A stock sums its
  series' last snapshots, so there each series must be one output row.
- An output row holds one row when every column of the row key (for a stock, the key
  without its clock) is grouped by, pinned with a top-level `=` filter or a one-value
  `in`, or reached through the key of a many-to-one relationship on that column (when
  it's the only relationship between the two entities); a `date` clock also counts at
  `grain: day`. Metric and segment filters don't count.
  The refusal points to the measure's key or a finer grain.
- `avg`, `min`, `max`, `median` and `percentile` stay available (average daily unique
  visitors is a real question), and so does `prior_period`. Cumulative, rolling and
  period-to-date metrics, scoped aggregates and metric predicates over it are refused.
- `project validate` probes such a measure grouped by those dimensions.
- It only applies to `kind: aggregate`: an `entity_count` is a distinct count the
  engine computes itself.

`default_agg:` (the new name for `agg_function:`) is the default aggregation the
API uses if the caller doesn't specify. The accumulation class drives the
default-allowed aggregation set; `disallowed_aggregations:` subtracts from it.

`rollup:` is optional and only affects physical-variant routing. Use
`rollup: additive` when the measure can be safely summed from a lower-grain
rollup, or `rollup: precomputed` when the variant stores the final value for
the requested grain. Unsupported or missing rollup semantics force the planner
back to the raw model relation.

#### Lookup measures

A lookup carries a parent's total onto each of its child rows: a coverage's premium on
every claim made against it. It takes exactly three keys:

```yaml
# models/claims.yml (claims and premiums both reference coverage)
measures:
  coverage_premium:
    kind: lookup
    from: premium_amount    # a measure on another model, totalled per `via` key
    via: coverage           # the parent entity
```

The loader copies `value_type` and `currency` from `from`. The value is one total
per parent, never re-aggregated, so it allows only `sum`, is `additive: false` and has
a flow accumulation. Authoring `expr`, an aggregation, `additive`, `accumulation`,
`value_type` or `currency` on a lookup is `INVALID_CONFIG`.

The invariant: a carried total appears at most once per output row's `via` key. It is
never added across two parents, or repeated over the child's own child rows.

- An output row must hold one parent: group by or pin (`=`) the `via` entity's key, or
  every column of the measure's own key. Anything coarser is refused with `ROLLUP_UNSAFE`
  (`details.construct: parent_lookup`), and `project validate` probes the measure grouped by
  the `via` key.
- Grouping or filtering by a child of the child (a claim's lines) is refused with
  `MIXED_GRAIN_INVALID`, and so is a filter on a dimension of the source model.
  Windows, distributions and metric predicates over a lookup are refused too.
- A NULL foreign key reads NULL. A parent with no source rows reads 0 when the source
  holds a value elsewhere in scope, and NULL (with `NO_DATA_IN_SCOPE`) when it holds
  none; an `avg`, `min` or `max` source reads NULL for it.
- The source is compiled as its own query, so its access policies apply. The lookup's
  direct relationship is also a bound dependency: denying or redacting it refuses
  validate, compile and query with `POLICY_DENIED` before rendering or execution. A row filter
  allows one relation per query, so a lookup under any row filter is refused with
  `POLICY_DENIED`.
- Time: the carried value is the parent's all-time total. A query's `where`, `time`
  window and buckets on the child's own clocks select child rows, and leave the value
  unchanged. Bucketing by the source's clock, prior-period, cumulative, rolling and
  period-to-date wrappers, and `temporal_role_overrides` on a lookup are refused with
  `REWRITE_NOT_SUPPORTED` (`details.unsupported_construct: lookup_time`).

The load refuses a lookup with `INVALID_CONFIG`, naming the key at fault, when:

- `from` is a stock, an entity count, `additive: false` or another lookup;
- `via` is a time entity;
- `via` has a composite key, even when both relationships cover every key column;
- `via` isn't the target of exactly one direct, untimed many-to-one relationship
  covering its whole key, both from the child's entity and from the source's entity;
- a `graph.path_preferences` route for the child-to-`via` or source-to-`via` pair uses
  a path other than that direct relationship. A recorded direct route is allowed.

The guard also refuses a different resolved route with `ROLLUP_UNSAFE` if the configuration
bypasses these load checks. Lookup measures support only the direct relationships and a
single-column `via` key.

Each answer carries a `parent_lookup` rewrite step (`REWRITE_APPLIED`) naming `from`, `via`
and the relationships it used. Interchange export leaves lookups out as unsupported.

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
    value_type: currency
    temporal_role: temporal_role.shop_order_ordered_at
    meta: { owner_team: finance_analytics, review_priority: high, change_risk: medium }

  # kind: cumulative — running total over time axis
  cumulative_revenue_usd:
    label: Cumulative Revenue (USD)
    kind: cumulative
    measure: revenue_usd
    value_type: currency
    temporal_role: temporal_role.shop_order_ordered_at
    meta: { owner_team: finance_analytics, review_priority: high, change_risk: medium }
```

A metric over time names its clock by [temporal role ID](#derived-id-grammar), not by
the model's time key: with `time: ordered_at`, validation passes but a query by month
fails with `INVALID_TEMPORAL_ROLE`. Give every measure and metric the governance
[`meta:`](#packageenvironments-and-governance-meta) block too, or `project validate`
warns. The exception is an `aggregate` or `semi_additive` metric named after the measure
it publishes, like `revenue_usd`, whose measure carries the `meta`. Other examples on
this page leave `meta` out for brevity.

In **direct named fields** (`measure:`, `numerator:`, `denominator:`), references
use package-relative keys (`revenue_usd`) — the loader resolves them. Inside an
**`expression:` AST** the rule differs: measure references must be fully
qualified (`measure: measure.shop.revenue_usd`); a package-relative measure key
there fails validation, even `--mode parse` ("metric … references unknown measure
'revenue_usd'; did you mean 'measure.shop.revenue_usd'?"). Metric references inside
an AST (`{kind: metric, metric: revenue_usd}`) still resolve package-relative.

With `schema_strict: true`, every authored metric needs an explicit, nonblank string
`value_type:` in either a directory or single-file package. `number` is valid when
intentional, including on ratio and derived metrics. Without strict validation,
an omitted authored value defaults to `number`.

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
    temporal_role: temporal_role.shop_order_ordered_at
    expression:
      kind: aggregate
      measure: measure.shop.order_count
      aggregation: count_distinct
      filter:
        all:
          - expression:
              kind: metric_predicate
              entity: entity.shop_customer
              scope_mode: entity_only
              input:
                measure: measure.shop.lifetime_order_count
              op: ">"
              value: 1
```

The buried `expression:` form is the canonical surface for filtered
aggregates. There is intentionally no top-level `filter:` direct field on
`kind: aggregate` — once a filter is involved, the metric needs the AST's
filter expression. Each `all:` item is either a dimension
condition with `field`, `op`, and `value`, or an `expression:` containing a
`metric_predicate`. All items are combined with AND. Other filter combinators,
including `any:`, are unsupported.

### Building-block measures

A filtered metric is often the governed form of a measure that also counts rows the
package leaves out: `Active stores` keeps the retail stores of an `Active stores (all kinds)`
count. `plan` answers a question that names such a metric with the metric, and holds a draft
that reads the measure instead (see `governed_metric_unrealized` in
[MCP_INTERFACE.md](MCP_INTERFACE.md)). To keep the measure out of agent search altogether,
author it with `publish: false`:

```yaml
measures:
  active_stores_all_kinds:
    label: Active stores (all kinds)
    kind: entity_count
    entity_key: store_id
    value_type: count
    publish: false        # answered through the metrics that filter it
```

A measure with `publish: false` that a metric reads through a filter (`filter:` on an
aggregate, or `where:` or `predicates:` on a scoped aggregate), and that no metric aggregates
whole, is a building block. `discover` doesn't list it. `plan` drafts the metric when it is the
only one that filters the measure or the question names it, and otherwise holds a draft that
reads the measure. `inspect` and Query IR still take the measure by id. Without
`schema_strict`, `publish: false` also keeps the loader from publishing the measure as a
metric of its own name.

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
| `ratio` | `numerator: <key>`, `denominator: <key>` | a zero denominator reads `NULL` |
| `cumulative` | `measure: <key>`, optional `window:` | running total |
| `prior_period` | `measure: <key>`, `period:` | comparison value at prior period |
| `period_to_date` | `measure: <key>`, `period:` | MTD / QTD / YTD |
| `rolling` | `measure: <key>`, `window:` | trailing window |
| `semi_additive` | `measure: <key>`, kind-specific options | applies measure's snapshot policy |
| `derived` | `expression: <AST>` | long-tail case; full AST |
| `conversion` | kind-specific options | event-pair conversion; converted events count within `window` of the base event, `base <= converted < base + window` |

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
authoring forms below with clear errors and migration pointers. Authored metric
shape and `value_type:` validation runs in both directory and single-file layouts
before loader defaults. The other raw-YAML strict checks run for directory
packages; single-file packages skip that pass but still get compiled-config
checks (which is why the `init` starter can author `grain:` alongside `entities:`).

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
| Metric with absent, null, empty or whitespace-only `value_type:` (both layouts) | Declare a nonblank string explicitly; `number` is valid when intentional, including for ratio or derived metrics |
| Buried `expression:` AST on metric kinds with direct named fields | Use direct fields (`kind: derived` and `kind: conversion` keep the AST) |
| `dimension.preferred_filter_ops` | Drop — metadata-only, no planner gating |
| `measure.clock_variants`, `comparison_peers`, `preferred_companion_metrics` | Drop on measures — metadata-only, no planner gating. (`preferred_companion_metrics` is allowed on metrics as advisory governance metadata; companion-metric relationships are too volatile to lock in at the measure layer.) |
| `topics:` on any object | Drop — no validation, no scaling pattern |
| `policy.kind: plan_constraint` | Drop — runtime no-op (the real kinds are `package_release`, `object_visibility`, `object_access`, `protected_object`, `metric_constraint`, `row_filter`) |

Warnings (advisory only):

- `as:` used where the resulting ID matches the auto-computed one (use is
  unnecessary).
- Package omits `package.environments`.
- A public measure or curated metric omits `meta.owner_team`,
  `meta.review_priority`, or `meta.change_risk` (see
  [`package.environments` and governance `meta:`](#packageenvironments-and-governance-meta)).
- A measure omits an explicit `default_temporal_role` while declaring
  compatible temporal roles.
- Entity pairs a question can need have two or more routes and no recorded
  decision (`ROUTES_UNDECIDED`; see [the route census](#route-census-and-route-changes)).

## Path-finding behavior (entity hopping)

When a query asks for "X per Y" where X is a measure on one model and Y is a
dimension on a different entity, the planner walks the inferred entity graph for
the route to Y's entity. This is automatic — most packages never author join
paths, because most pairs have one route (see [the route rule](#the-route-rule)).

- "orders per customer": the orders model has both `order_id` (primary) and
  `customer_id` (FK). The planner uses that table directly.
- "items per customer": no single table has all three columns. The planner walks
  `order_item → order → customer` via inferred relationships.
- "items per customer" when the items table also carries `customer_id`: the direct
  relationship and `order_item → order → customer` are two routes that can name
  different customers. The item's own `customer_id` is its one direct key, so the
  planner uses it and the response notes it (`ROUTE_COLOCATED_KEY`); a
  `graph.path_preferences` row records the other route when that is the meaning.
- "customers per store" in a package where a customer reaches a store through the
  stores they ordered at and through a preferred store: neither is the customer's own
  key, so the query is refused until a `graph.path_preferences` row records which one
  the package means.

A many-to-one or one-to-one hop never removes a measure's row. Whatever reads the
dimension it looks up (a grouping, a filter, the measure's own filter, an `aggregate_if`
condition or a measure expression), and whatever shape the query takes (pre-aggregated
before its joins, de-duplicated across a one-to-many hop, filtered with `EXISTS`, an
entity-set ratio, or dimensions alone), the hop is a left join. A row whose foreign key is
NULL or matches no row stays, with NULL for everything the hop looks up: it groups under
NULL, and grouped rows add up to the ungrouped total. A filter on such a dimension treats
the row as it treats a NULL value in the row itself, wherever the filter is written: `IS
NULL` selects it, so "passengers excluding crew" through a crew-roster lookup is a
`crew_role IS NULL` filter, while `=`, `!=`, `IN` and `NOT IN` never match it.

Only these reads join a lookup with an inner join, so a row with no match is left out:

- a time role read through a lookup (a row with no time has no time bucket), and so any
  other read of the same hop;
- a metric predicate's own query, and its route to the entity it qualifies. Its set is
  matched on that entity and on the query's grouped (context) entities, so a row with none
  of them is not in the set;
- a distribution's per-entity values (its `over`): a row whose lookup of the entity finds
  no match belongs to no entity, never to a NULL entity of its own;
- conversions (their match keys and properties);
- a dimension a rollup of the measure's model holds (below). That rule covers every
  dimension any rollup of the model holds, even at a grain the rollup can never answer,
  so those rows are dropped for that dimension however the query is grouped. A rollup of
  another model changes nothing: a rollup of the items holding the country keeps an order
  count's rows, even when the orders are counted from the items, and a query of
  dimensions alone reads no rollup;
- every hop on ClickHouse, where an unmatched outer-join column reads `''` or `0` unless
  it is `Nullable`, not NULL.

Hops that fan out are inner joins too. A distinct count read from a child model (a
`rollup_safe` reverse `count_distinct`) joins back to the counted entity with an inner join,
even when grouping only by a child's looked-up dimension and reading no parent dimension
or time axis. A child row whose parent has no record counts nothing; valid parents whose
child lookup finds no match still count under NULL. This shortcut requires exactly one
relationship between child and parent, on the counted path, and permits the forward lookup.
With multiple parent relationships or a reverse-only relationship, the query uses the
counted entity's own leaf instead. The lookups past that entity keep their rows.
Because of those inner reads, an `aggregate_if` condition that a row with no
match could satisfy (such as `IS NULL`) is refused.

Long chains are first-class: a measure can be grouped or filtered by a
dimension four relationships away (`line_item → order → customer → city →
region`), with each hop cardinality-checked. Every hop must be `N:1`/`1:1` in
the traversal direction (or carry a declared rewrite, e.g. `rollup_safe`
reverse aggregations or `temporal_validity`); anything else is a structured
refusal, never a silently fanned-out number. A positive child filter needs no
`rollup_safe` opt-in: it lowers to correlated `EXISTS` and keeps each parent row
once when at least one child matches. Non-temporal paths of declared `N:1`,
`1:N` and `1:1` hops may include a lookup before reaching children, and may
use an alternate parent key; all authored join columns participate in the
correlation. This supports parent counts and sums without multiplying their
values. A lookup-before-child or alternate-key path is the route the route
rule chose (below); a pair it can't decide is refused with `AMBIGUOUS_PATH`,
even when one route is shorter. This applies to query filters and
measure-bound filters, including beside a lookup. Unsafe, unknown-cardinality and temporal paths retain their refusals.
ClickHouse retains a deduplicated-parent leaf for servers without correlated
subqueries. Key-based descents retain their existing SQL shape, including
beside lookup selections, groupings and filters; those lookups remain inner
joins. It refuses paths that look up a parent before reaching children and
paths joined off the parent's declared key, including beside a lookup, with
`MIXED_GRAIN_INVALID`.

Grouping retains a narrower exception: a path that only goes down one-to-many hops before any lookup
(`order → order_item → product`), each hop joined on the declared key of its
one side, lets a distinct parent count be grouped by the far dimension; the entity's key is what
the engine de-duplicates on.
A query states which child rows its conditions mean with a child group in `where`
(`{child, match: any|none, where}`, see
[QUERY_IR_SCHEMA.md](QUERY_IR_SCHEMA.md#child-groups)); two or more positive plain filters
on one child, or one negated one, are refused with `AMBIGUOUS_CHILD_SCOPE` and a
clarification. Its readings are child groups, so it is offered only when a group on that
child takes the plain filters' own route; otherwise the refusal is `MIXED_GRAIN_INVALID`,
naming the `graph.path_preferences` row that records that route. A
measure's own `filter` may cross a one-to-many hop with one positive condition;
negated ones stay refused because "has a non-matching child" and "has no matching
child" differ. A parent sum grouped by child dimensions, or a child value authored
at parent grain, stays `MIXED_GRAIN_INVALID`.

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

### The route rule

When two routes reach the same entity (role-playing foreign keys are the
classic case: `order.ship_city_id` vs `order.customer → customer.city_id`),
the routes have different *meanings*, and which one a question means is a
business definition. The engine never guesses: the decision is recorded once
in the package, then every query uses it, and adding a route never silently
changes an existing answer. A route is chosen only by a recorded decision or
the start entity's own key. For each start entity and target entity, over
every route within the hop ceiling, in this order (a query's own
[`route_decisions`](QUERY_IR_SCHEMA.md#route_decisions) row for exactly the
pair comes first, for that query only):

1. **Decided.** A `graph.path_preferences` row for exactly the pair (below)
   wins.
2. **The start's own key.** When exactly one route is a direct relationship
   from the start entity that reaches at most one row (many-to-one, or
   one-to-one: the start row holds the target's key), it is used, even where a
   row for another pair would point elsewhere. A loan that holds its own
   `district_id` reads that district, though the package records an account's
   district as its owner's. Two such keys to one entity (an origin and a
   destination) are not one: go on.
3. **Inherited.** Every row holds wherever a route walks its pair: a route
   that passes through a row's two entities by another part than the row's
   path is dropped. Walked the other way (from the row's target towards its
   source), the part must be the row's path reversed, when every hop of that
   path allows the reverse walk; otherwise the row says nothing about that
   direction. So a row for (account, district) also decides the district of a
   loan, a card or a transaction reached through its account, and the region
   beyond the district.
4. **Only route.** Exactly one route remains: it is used.
5. Two or more remain: the query is refused with `AMBIGUOUS_PATH`, naming the
   remaining routes, whatever their lengths (equal or not) and whether they fan
   out.
6. The rows dropped every route: the query is refused with `PATH_NOT_FOUND`
   and `details.reason: excluded_by_decision`, naming the rows in
   `details.rows` (raise `max_hops` if a longer route follows them, or change
   the rows).

Hop count and weights never decide. Every path choice goes through this rule:
grouping, filters, a measure's own filter, metric predicates, time roles,
conversions, the direct read of a foreign key, a child filter's `EXISTS`, the
distinct count read from a child's rows, and discovery, grain recovery,
catalog and error hints. A calendar dimension reached only through other
facts' rows is refused the same way, and its recovery hint points at
`time.grain` instead.

The refusal is a clarification: `details.reason` is `route_decision_required`,
and `details.clarification` asks which route the question means, in business
words built only from package labels:

```json
{"kind": "route", "apply": ["query", "package"],
 "question": "Which District does the question mean for an Account?",
 "options": [
   {"id": "branch_district", "meaning": "the District of the Account's Branch",
    "relationship_path": ["relationship.accounts_branch", "relationship.branches_district"],
    "decision": {"source_entity": "entity.bank_account", "target_entity": "entity.bank_district",
                 "relationship_path": ["relationship.accounts_branch", "relationship.branches_district"],
                 "label": "the District of the Account's Branch"}},
   {"id": "owner_district", "meaning": "the District of the Account's Owner", "...": "..."}]}
```

- `meaning` names every entity on the route by its label. A hop between two
  entities related more than once is named by the relationship's own label, or
  else by its foreign-key columns (`the Flight's Airport (origin_airport_id)`),
  and a one-to-many hop reads "any of the …" (`any of the Account's Memberships`).
- `id` is unique within the refusal and never an entity key: the waypoint and
  target entity keys (`branch_district`), or a direct hop's foreign-key column
  without its `_id`/`_key`/`_code` suffix (`origin_airport`); `_2` on a clash.
- `decision` is the `graph.path_preferences` row that makes the option the
  package default, with `label` set to the meaning. When that row would
  disagree with existing rows (see "Rows must agree" below), the option adds
  `conflicts_with`: those rows, to change before recording it. Its `decision`
  still answers per query.

Every option can be applied two ways. For the person who asked, the agent
resends the query with the option's `decision` in
[`route_decisions`](QUERY_IR_SCHEMA.md#route_decisions): that query only, not a
default. For everyone, a maintainer records the same row in the package (Architect
`record_route_decision` writes it for review); then the question answers
without asking.

A relationship's `path_preference` weight no longer exists: a package that
still sets one fails to load with `INVALID_CONFIG`, naming the relationship.
Record the route as a `graph.path_preferences` row instead.

### `graph.path_preferences:` — recording a route

Record the route per entity pair:

```yaml
# graph.yml
graph:
  path_preferences:
    # A line item's region: its customer's home region, not the ship-to region.
    - source_entity: line_item
      target_entity: region
      relationship_path:
        - relationship.line_items_order
        - relationship.orders_customer
        - relationship.customers_city
        - relationship.cities_region
      label: the Region of the City of the Line item's Customer
```

`source_entity` and `target_entity` take an entity's key, name or id (a
clarification option's `decision` uses ids). Rows are validated at load time
(unknown entities and relationships, broken chains, and disallowed traversal
directions are `INVALID_CONFIG`), and the fanout safety analysis still applies
to the recorded route. A row decides its own pair, and every other pair whose
routes walk through it inherits it (rule 3 above), except where the start
entity holds its own key to the target (rule 2). So one row usually serves a
whole family of questions: record the shortest pair that carries the meaning.
The optional `label` states the meaning in business words; it keeps the
decision reviewable, the package writer keeps it, and a compile's `hop_profile`
target and discovery's path availability show it as `route_label` for the
pair's recorded route.

Rows must agree. When one row's path walks through another row's pair, the
part between them must be that row's path (or, walked the other way, its path
reversed); otherwise the package fails to load with `INVALID_CONFIG`, naming
the rows in `details.rows`. A configuration built in code (for example with
`Runtime.from_config`) is held to the same check when it is first used. A row
for a pair with one route is allowed: it records a definition.

Four guard rails back this up:

- **Route notes** — where the engine chose one of two or more routes for a pair
  the compiled query reads (root, leaf, predicate and conversion paths, and the
  direct read of a key), compact and full responses carry one `info` note, with
  the chosen route's relationship ids in `details.route` and its readable
  meaning in the message:
  - `ROUTE_COLOCATED_KEY` (rule 2, `Order → Store (own key)`):
    `details.alternatives` holds, for each other route whose row would load
    beside the package's rows, the `graph.path_preferences` row that would make
    it the default, and `details.conflicts_with` lists any other route with the
    rows its row would disagree with;
  - `ROUTE_RECORDED` (rule 1, `(recorded route)`, or rule 3,
    `(recorded for Account → District)`, with the rows it follows in
    `details.rows`).

  A pair with one route gets none, and the minimal response (the MCP default)
  leaves the notes out. So adding a route never changes an answer silently: a
  pair whose one route is its own key keeps its answer and now notes it, and
  any other pair is refused until a row records it. A note names only a route
  the SQL reads, and only when the pair's resolution chose that route, so the
  same query gets the same notes whatever ran before it; a count read from a
  child's rows along the counted entity's route is noted once, for that route.
  For a recorded route, notes check whether multiple routes fit `max_hops`
  using bounded reachability scans, without enumerating alternatives or caching
  a route refusal just to produce a note.
- **`AMBIGUOUS_PATH` error** — rule 5 above. A `graph.relationships:` entry
  never replaces a foreign key on other columns: the model keeps both, so an
  origin and a destination key into one `airport` entity are two routes. Any
  query that reaches the airport is refused until you record the role it means:
  its city, its key (`airport_code`, even though the leg's table holds the
  foreign key), a filter on either, or a metric predicate on the airport. An
  entry that restates the inferred foreign key (the same `via` columns, or none)
  replaces it. Two authored entries on the same `via` columns are refused at
  load (`INVALID_CONFIG`, naming both): keep one, or give each its own `via`
  if they are different roles.
  A `path_preferences` row for the pair also decides a query from another
  entity whose routes pass through the pair, one that continues past the
  target, and one that starts at the target (walking the row backwards), unless
  that query's start holds its own key to its target. A recorded role reads its
  key through that relationship's join, like any other column of the airport,
  so a leg whose code matches no airport row groups under a NULL key (a lookup
  read, above); a package with a single role and no row for the pair reads the
  key from the leg's own column and groups that leg under its code, when the
  route rule takes that relationship. Every read of the key, a filter on it and
  a metric predicate take the route the rule chose, so the key and the
  airport's other columns always come from the same airport.
- **Route census** — parsing the package (`semantic-rails check`, `validate`)
  lists every entity pair a question can need that is still refused for want of
  a decision, role-playing keys included, and warns once with `ROUTES_UNDECIDED`
  ([below](#route-census-and-route-changes)).
- **`PATH_JOIN_CONFLICT` error** — one query needs the same physical table
  through two different relationships (e.g. a customer's region recorded as
  the regions its orders ship to while its city is read through its own key).
  One table instance cannot serve both semantics, so the compiler refuses with
  both routes named. Fix by recording a consistent route for every affected
  target, or by modeling the second role as its own entity over a dedicated
  relation. Two rows that record different routes through one pair never get
  this far: the package fails to load (above).

### Route census and route changes

A route is a business definition, so a package needs one for every entity pair
a question can need: every entity is a start, including distinct-values and
synthetic-count queries, to each other reachable entity. The
census resolves only pairs with two or more routes, once per pair; impact and
guard comparisons resolve every pair using package decisions, independently
of any active query route overrides.

**Census.** The parse report (`semantic-rails check`, `validate`, and
Architect's `project_status`) carries `route_census`:

- `undecided`: `[{source_entity, target_entity, details}]`, the pairs refused
  with `AMBIGUOUS_PATH`, where `details` is the refusal's own (each route, its
  meaning and `details.clarification.options[*].decision`, the row that records
  it). One `ROUTES_UNDECIDED` warning gives
  their `count` and `pairs`. Record the route each pair means as a
  `graph.path_preferences` row with `record_route_decision` before a question
  needs it. The warning is
  advisory and never blocks a promotion.
- `assumed`: `[{source_entity, target_entity, relationship_path, basis}]`, the
  pairs with two or more routes answered by the
  start's own key (`basis: colocated_key`). Confirm the route, or record
  another.

**Route changes.** A new relationship can give a pair a second route, so a
question that answered before is refused, or now takes the start's own key.
`impact-report` (Architect's `impact_project`) resolves every census pair of
either package, between entities both declare, and lists each one the change
resolves differently under `route_changes`:

```json
{"source_entity": "entity.bank_invoice", "target_entity": "entity.bank_region",
 "base": {"relationship_path": ["relationship.invoices_account", "relationship.accounts_branch_region"]},
 "head": {"refused": "AMBIGUOUS_PATH"}}
```

`base` and `head` hold the pair's route or the code it is refused with.
No recovery rows are suggested. Any entry makes the
risk `high` and counts in `changed_behavior_count`, and the Markdown summary
lists each one in entity labels ("Invoice to Region: was Invoice → Account →
Region, now refused (AMBIGUOUS_PATH)").

**Architect requires explicit route decisions.** Every Architect write goes
through one transaction, which compares the package before and after the
change. A change that would refuse an answered pair whose route still exists,
or answer it by another route, is refused with `ROUTE_DECISION_NOT_RECORDED`
until the author records a decision. Previews use the same guard; nothing is
written and no route rows are generated without an explicit keep choice. The
refusal lists affected pairs in
`details.route_changes`. Its message names the explicit `graph.path_preferences`
fields (`source_entity`, `target_entity`, `relationship_path`), without suggesting
rows. Architect `upsert_relationship(..., keep_existing_routes=True)` records each
moved pair's previous path in the same transaction and confirms newly ambiguous
own-key routes. The result lists those rows in `kept_route_decisions`. Previously
answered pairs must retain their routes. A previously refused pair may take the
relationship's own outcome, but generated decisions must not change that outcome;
otherwise the call refuses without writing. The keep retry hint appears only for
`upsert_relationship` refusals. Otherwise choose routes and call
`record_route_decision(decisions=[...])` with several explicit rows before adding
the relationship, or include chosen rows in the authored change. An ordinary change that
moves an inherited answer also needs that pair's own decision.

An explicit route chooses a relationship path, not a promise that orphan keys
keep their values. For example, after adding an origin role alongside a
destination role, a decision for the destination path uses the airport lookup:
a destination key with no airport row groups under `NULL` and does not match
a filter on the airport key. Review the chosen route against reference SQL.

`record_route_decision` writes where the loader reads route rows (a top-level
`path_preferences` block in `package.yml`, else `graph.yml`, else `package.yml`'s
`graph` block), rewriting that file as Architect YAML and dropping comments.
The `decisions` form accepts a nonempty list of rows with `source_entity`,
`target_entity`, `relationship_path`, and optional `label`, instead of single-pair
arguments. All pair replacements are validated together: one invalid, duplicate,
or conflicting row refuses the whole batch without files or receipts. The result's
`route_decisions` reports each row's `replaced` value and `summary`.
It deliberately changes the default and reports every moved pair, inherited
pairs included. `remove_object` uses the same preservation guard: a removed
route or one beyond the new `max_hops` may leave a pair refused, but switching
to another answer requires the author to record that route first. In every
case `route_changes` lists every changed pair, including refused → answered,
and `route_decisions_added` is empty. Hand edits get the same report from
`impact-report`.

With `validate_after=False`, a write from a loadable package to loader-invalid
input, including a preview, is refused with `INVALID_CONFIG` before anything
is written, so an invalid intermediate edit cannot erase the earlier route baseline. Ordinary
parse-gated rollback and writes that repair an already-invalid package retain
their existing behavior.

### `hop_profile` — observing entity hops

Every compile/query response carries a `hop_profile`: the root entity, the
chosen relationship chain per target entity with per-hop direction /
cardinality / safety and the rule that chose it (`route_basis`: `decided`,
`colocated_key`, `inherited` or `only_route`, the route rule's rungs 1-4), the
hop ceiling, and `long_hop_targets` (targets 3+ hops out). Operators can log this to find questions that repeatedly cross
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
- A weekly rollup answers only week queries, because weeks straddle month,
  quarter, and year boundaries. Its default `eligible_time_grains` is `[week]`.
  Build it on Monday-start (ISO) weeks, the weeks the compiled SQL uses; the
  engine doesn't check this.
- The query's `start` and `end` must fall on the rollup's bucket boundaries, with
  no UTC offset (or a zero one): a monthly table answers `2026-01-01` to
  `2026-04-01`, not `2026-01-15` to `2026-03-31`. A minute or hour rollup needs
  bounds on day boundaries.
- Each measure column holds one aggregate per rollup row, and the query must ask
  for that aggregation: a `sum` column answers `sum` queries (re-added with
  `SUM`), a `min` or `max` column answers `min` or `max` (re-aggregated with
  `MIN` or `MAX`). Declare it with `holds:` (`sum`, `min`, `max` or
  `count_distinct`), as in `revenue: {column: max_amount, holds: max}`. A column
  without `holds:` needs `rollup: additive` (the variant default) or
  `precomputed`, and holds a sum for an `aggregate` measure or a distinct count
  for an `entity_count` measure. `avg`, `median` and `percentile` queries, and
  stock (semi-additive) measures, run on the base tables.
- A `count_distinct` routes across rollup rows only when it counts the
  single-column row key of a model that isn't a fact model (for example distinct
  `order_id` on an orders model with `grain: [order_id]`). Distinct counts of
  anything else, such as customers or one column of a composite key, can't be
  added up across rollup rows. A column declared `holds: count_distinct` still
  answers them at the rollup's own time grain when every rollup dimension is
  grouped or pinned to one value by an `=` filter, so each result row is one
  rollup row. The rollup must have one row per time bucket and dimension columns
  (and per key of any `grain.entities`); an `IN` list or a range on a rollup
  dimension that isn't grouped runs on the base tables.
- A time role whose `column_timezone` differs from its `timezone`, and a query
  with a non-default `calendar_id`, run on the base tables: the rollup path
  buckets the stored column's clock, without the role's zone conversion, on the
  default calendar.
- A dimension column pre-joined from another model (in an `aggregate_relations:`
  entry, for example `region` from customers) declares the relationships it was
  built along: `dimensions: {dimension.region: {column: region, path:
  [relationship.orders_customer]}}`. It routes only when the query joins that
  model along the same path, and only if every hop is many-to-one (or one-to-one)
  with no `temporal_validity`. A rollup holding a pre-joined column without such a
  `path` never routes, even for queries that don't use that column: a one-to-many
  join would have repeated fact rows in every other column.
  Another model's key read from a foreign key (such as the customer key) needs a
  `path` of the one relationship between the two models, and doesn't route when
  two relationships link them. Build a pre-joined column with an inner join: the
  base path joins a dimension a rollup of the measure's model holds with an inner
  join too, so a fact row with no match is left out of both and routing never
  changes an answer. So a rollup with a pre-joined column answers only queries that
  group or filter by that column; the base path doesn't join it otherwise and keeps
  such rows. A measure whose
  expression, or a time role whose column, comes from another model doesn't route.
  An `aggregate_relations:` entry must declare its `temporal_role`.
- Every selected measure must have a column in the variant.
- Every grouped or filtered dimension must be covered by the variant. If a
  query groups by `customer_id` and the monthly table excludes that dimension,
  the planner scans the raw relation.
- Query-time `metric_predicate` shapes, including a `metric_predicate` inside an
  aggregate's `filter`, do not route through variants yet.
- An `aggregate_relations:` entry that declares `filters` doesn't route yet: it
  holds only the rows its filters kept.
- A rollup that declares `requires_certification: true` (on a variant or an
  `aggregate_relations:` entry; default `false`) routes only while the host's
  certification provider says it is certified, and never when none is installed
  (`not_certified`). A host installs one at startup with
  `semantic_rails.acceleration.routing.set_certification_provider(provider)`, where
  `provider.certified(config, relation)` returns `True` for a certified rollup. To
  certify one, a host calls
  `semantic_rails.acceleration.certification.certify_aggregate_relation(config,
  relation_id)`. For each measure column it gets the rule the rollup fails (or none)
  and a query's `base_sql` and `rollup_sql` to run and compare: over all time,
  grouped by every rollup dimension, at the rollup's own grain, so its rows are the
  rollup's buckets. Every other query the rules let it answer re-aggregates those
  buckets. A rollup whose own grain its time role can't be queried at (an hour
  rollup under a role that starts at day) isn't certifiable. A runtime doesn't
  use its compile cache for a package with such a rollup: every request compiles
  again, which costs compile time, so that a revoked certification applies to the
  next request. A rollup under a role whose `timezone:` isn't `UTC` or `Etc/UTC`
  isn't certifiable yet (`timezone_not_utc`), so its queries use the base tables.
  On DuckDB, MotherDuck, DuckLake and Postgres, which run each query in its role's
  zone, build the rollup and run each pair with the session time zone set to UTC
  (`SET TimeZone = 'UTC'`).

When a rollup can't answer a query exactly, the query runs on the base tables, and
`logical_plan.measure_plans[].aggregate_relation_rejections` maps each rejected
rollup of that measure's entity to the reason.
`performance_plan.aggregate_routing.candidates` lists every rollup considered for
each measure leaf as `selected`, `eligible`, `rejected` or `unknown`, with its
reason (at most 200 rows; `candidates_omitted` counts the rest). A leaf lowered as
separate queries, such as a `distribution`'s branches, reports a rollup it didn't
reject with reason `lowered_separately`: `unknown` if some branch reads it,
`eligible` if none does. `aggregate_routing.selected` lists every rollup the
compiled SQL reads, branches included. Set
`SEMANTIC_RAILS_AGGREGATE_ROUTING=off` to run every query on the base tables (see
[QUERY_API.md](QUERY_API.md)).

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

### Segment references

`parse-config`, `validate-config` and `check` reject a segment that the catalog,
`inspect` and segment validate, explain and preview (the MCP `segment` tool, or the
CLI `segment-*` commands) could not serve:

- Its `entity:` names no graph entity. It may be a graph entity key, name or id.
  An `entity:` inside a membership `metric_predicate` must be an entity id. When
  one is close, the error suggests its id.
- The catalog cannot describe it: its entity is not allowed as a query root or has
  no key dimensions, the `basis_metric` is unknown or rooted on another entity,
  or a preview dimension is unknown, not groupable, or belongs to another entity.
- The query that segment validation (MCP `segment` with `action: "validate"`, CLI
  `segment-validate`) derives from it does not compile.

### Unknown keys

`parse-config`, `validate-config` and `check` reject a metric or segment key the
loader doesn't read, in every layout it reads: files under `metrics/` and
`segments/`, root `metrics.yml` and `segments.yml`, and `package.yml`. The loader
would ignore such a key, so the package would behave differently from what it says:

- a mistyped key, such as `valeu_type` on a metric;
- a segment's `where` or `metric_filters` written outside `membership:`;
- an unknown key inside `membership:`. The loader reads `where`, `metric_filters`,
  `time` and `temporal_role_overrides` there. `filters` and
  `dimension_filters`, the spellings other tools use, point to `membership.where`.

Every metric also gets the kind checks: a metric with an unknown `kind:`, or
without a field its kind requires, such as a ratio's `denominator`, is rejected.

The loader hands a metric's `expression:` to the expression parser as written, and
carries every direct field a metric kind takes into the expression it builds, so a part
is kept or rejected and never dropped to make the metric load:

- An unknown expression `kind:`, or a field its kind does not support (a `where` on a
  `metric` reference, an `anchor` on a plain measure), is rejected with the metric named.
- Direct fields follow the same table. `partition_by` on a `rolling`, `period_to_date`
  or `cumulative` metric, `window` on a `rolling` metric, and `window_scope` on a
  `cumulative` metric reach the compiled metric. A field the kind does not take, such as
  `window` on a `cumulative`, `partition_by` on a `prior_period` or a `ratio`, or
  `order_by` anywhere, is rejected. `partition_by` declares a grouping the query must
  include: the window already runs separately within each group the query groups by, so
  the field changes no value, and a query that does not group by the dimension is refused
  with `INVALID_QUERY`, naming the
  metric and listing the dimension under `partition_by_missing_from_group_by`, instead of
  returning an unpartitioned value. A `partition_by` that is not a list is rejected at load.
- A metric is written either with an `expression:` block or with direct fields, never
  both. A metric that has an `expression:` and also any direct field (`measure`,
  `aggregation`, `numerator`, `denominator`, `window`, `window_scope`,
  `offset`, `period`, `partition_by`, `order_by`) is rejected at load, naming the metric
  and the fields; move them inside `expression:`. A `kind:` beside `expression:` is fine.
- Every `partition_by` entry must be a dimension of the package, or the metric is rejected
  at load. A short key resolves against the model of the measure the window reads (a
  `rolling` over a metric reference or a formula takes only full `dimension.` ids), and is
  stored as the full id.
- A `window` on a plain `aggregate` loads but is refused when the metric is queried,
  because a plain aggregate has no window. Use a `rolling` metric.
- `scoped_aggregate` keeps its `anchor`, `window`, `where` and `predicates`. A short
  `measure` key, and a short `where` field key on the measure's own model, resolve as they
  do elsewhere in the package. An `anchor` with a `window` is refused when the metric is
  queried, until anchored windows compile (see `docs/CAPABILITIES.md`); it is never
  computed as a lifetime value.

### Filter values

Validation that reads the data (`validate-config`, `check`, and the `runtime`
and `full` modes) looks up the values of each string dimension a metric's
`filter` compares with `=`, `!=`, `IN` or `NOT IN`, through the same live
lookup as `valid-values`. A literal that matches no value, such as `'Completed'`
over data holding `completed`, gets a `FILTER_VALUE_NOT_FOUND` warning that
names the closest value. It is a warning because sample data can lack a value
that production data holds. A dimension with more values than the
`valid-values` limit is skipped, and parse-only validation reads no data.

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
