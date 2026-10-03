# Query IR JSON Schemas

The canonical, machine-readable contract for the Query IR payload accepted
by `/api/v1/{validate,compile,query}` and the equivalent MCP tools
lives at [`schemas/query_ir.v1.json`](../schemas/query_ir.v1.json). That stable
schema accepts `version: 1` only. The separately versioned
[`schemas/query_ir.preview.v2.json`](../schemas/query_ir.preview.v2.json)
describes the enriched runtime preview used by planner outputs. Preview v2 may
change before promotion to a stable major; consumers must opt into it
explicitly. Both are JSON Schema Draft 2020-12 documents and ship inside the
Python wheel under `semantic_rails.contracts`.

The schema is regression-tested against every IR in the benchmark corpus
and the comparison fixtures: see
[`tests/semantic_rails/test_query_ir_schema.py`](../tests/semantic_rails/test_query_ir_schema.py).

## Top-level payload

| Field | Type | Notes |
|---|---|---|
| `version` | `integer` | Pin the IR schema version. Stable v1 accepts only `1`; the separate preview-v2 schema accepts only `2`. |
| `select` | `array` of `SelectItem` | Projected outputs. |
| `group_by` | `array` of dimension ids | Grouping keys. |
| `where` | `array` of `WhereFilter` | Dimension-level filters with shape `{field, op, value}` where `field` is a dimension id. See "WhereFilter" below for the op list and null semantics. Expression-shaped filters belong in `metric_filters`. |
| `metric_filters` | `array` of `MetricFilter` | Post-aggregation predicates. **There is no top-level `having` key.** |
| `order_by` | `array` of `OrderBy` | Final-select ordering. Uses `{field, direction}` — not a select-style expression. |
| `limit` | `integer` (or `null`) | Optional row cap. |
| `time` | `TimeBlock` (or `null`) | Query-level time anchor: temporal_role + grain + bounds. `start` is inclusive, `end` is exclusive. |
| `temporal_role_overrides` | `object<measure_id, temporal_role_id>` | Per-measure clock bindings. |
| `route_decisions` | `array` of `RouteDecision` | This query's own route for an entity pair: the `decision` of an `AMBIGUOUS_PATH` option. See [`route_decisions`](#route_decisions). |
| `policy_context` | `object` | Caller-supplied access context (`environment`, `audience`, `roles`, `now`, ...). |
| `limits` | `object` | Per-request `statement_timeout_ms`, `max_rows`. |
| `verbosity` | `"summary"\|"minimal"\|"compact"\|"full"` | Response detail level (default `compact`). On `catalog`, `summary` returns counts + flat ID lists per kind (under 10KB) — recommended for cold-start orientation. |
| `sql_profile` | `"audit"\|"compact"\|"debug"\|"off"` | SQL rendering profile (default `audit`). |
| `debug` | `boolean` | Opt-in raw SQL in error envelopes. |
| `explain` | `boolean` | Include explain artifacts. |
| `request_id` | `string` | Echoed in the response envelope and audit log. |

Unknown top-level keys are **rejected** with `INVALID_QUERY` and the
offending keys are returned under `details.unsupported_keys`, so typos
surface as structured errors instead of silently no-op'ing. There is
**no `having` field** — use `metric_filters` (see below). The only
top-level extras accepted by the runtime and schema are
underscore-prefixed annotations such as `_note`, which are ignored before
planning and SQL generation.

### Removed: `path_policy`

`path_policy` (`preference`, `ask_if_ambiguous`) is no longer a Query IR key,
in v1 or preview v2. This is a breaking change made within v1: before 1.0 the
project follows Semantic Versioning's major-zero rule (see
[CHANGELOG.md](../CHANGELOG.md)), under which a 0.x release may change the
public API. The key never changed an answer. A query that still sends it is
refused with `INVALID_QUERY` and `details.unsupported_keys: ["path_policy"]`;
delete it.

The package records a join route with a `graph.path_preferences` row, which
also decides every route that walks its pair. Without a row, a query whose
routes can answer differently uses the start entity's one direct key or is
refused with `AMBIGUOUS_PATH` (see
[the route rule](PACKAGE_AUTHORING.md#the-route-rule)), which asks which route
the question means. Where the engine chose one of two or more routes, compact
and full responses carry an info note, `ROUTE_COLOCATED_KEY` or
`ROUTE_RECORDED`, with the chosen route in `details.route`.

### `route_decisions`

After an `AMBIGUOUS_PATH` refusal, the person's answer goes back in the query:
one row per entity pair, the chosen option's `details.clarification`
`decision`, shaped like a `graph.path_preferences` row (`label` is accepted and
ignored).

```jsonc
{
  "version": 1,
  "select": [{"expression": {"measure": "measure.bank.balance"}, "as": "balance"}],
  "group_by": ["dimension.bank_district_name"],
  "route_decisions": [{
    "source_entity": "entity.bank_account",
    "target_entity": "entity.bank_district",
    "relationship_path": ["relationship.accounts_owner", "relationship.owners_home_district"]
  }]
}
```

- **This query only.** A row is an exact-pair decision: it applies before the
  package's row for the same pair (so it overrides a package default for this
  query), never to other pairs, and is never cached as the package's route. It
  is not a default: to make one, record the row in the package
  (`record_route_decision`).
- **One of the pair's routes.** The path must be one of the routes between the
  pair within `graph.path_policy.max_hops`, the routes the engine itself
  considers (no cycles, no longer chains); anything else is `INVALID_QUERY`
  (`details.reason: route_not_offered`). An unknown entity (by id or name) or
  relationship, a broken chain, a disallowed direction, or a path that doesn't
  end at the target is `invalid_route_decision`; so are a malformed row
  (`malformed_route_decision`), two rows for one pair
  (`duplicate_route_decision`), and a row for a pair the query never walks
  (`route_decision_unused`).
- **Never under a row filter.** When a row filter in the caller's context reads
  any entity on any of the pair's routes, the query is refused with
  `POLICY_DENIED` (`details.reason: route_override_under_row_policy`, with
  `path`, `policy_ids` and `hint`). A reviewed package row is the way to change
  routes there.
- **Disclosed.** Every response carries one `info` warning
  `ROUTE_CHOSEN_BY_QUERY` per row, at every verbosity: `details.row` and
  `details.replaced`, how the package resolves the pair without it (`decided`:
  its own row; `colocated_key`: the start's own key; `inherited`: rows for pairs
  its routes walk through; `only_route`; `undecided`: the package refuses it).
  `hop_profile.targets[*].route_basis` is `query` for the pair.
- `build-options` with a partial query that carries rows shows the dimensions
  they make reachable, and its query patches keep the rows; a patch that would
  leave a row unused is unavailable with that refusal. Live `valid-values`
  checks rows before probing and reads through their routes using only the query's
  configured measures, including those read by metrics; mixed selections containing
  `aggregate_if` are accepted, but synthetic measures never become anchors. Probing
  keeps the executed query's filters and temporal role overrides, so a row read only
  by a metric filter stays in use. If none anchors the dimension, the first anchor's
  refusal is returned unchanged; unrelated measures never supply values. Without a
  configured query measure, `NO_VALID_VALUES_SOURCE` asks for a measure or metric anchor.

## Common gotchas

### `metric_filters`, not `having`

```jsonc
// WRONG — INVALID_QUERY: details.unsupported_keys=["having"]
{
  "select": [...],
  "having": [{ "expression": {"metric": "metric.orders"}, "op": ">", "value": 100 }]
}

// RIGHT
{
  "select": [...],
  "metric_filters": [
    {
      "expression": {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "scope_mode": "entity_only",
        "input": { "metric": "metric.jaffle.lifetime_spend" },
        "op": ">=",
        "value": 500
      },
      "op": "=",
      "value": true
    }
  ]
}
```

### `order_by` uses `{field, direction}`

```jsonc
// WRONG — KeyError pre-fix; now returns INVALID_EXPRESSION with recovery_hints
{
  "order_by": [{ "expression": {"measure": "measure.revenue"}, "direction": "DESC" }]
}

// RIGHT — field must resolve to a select alias, a group_by dimension id,
// the time-axis output alias (e.g. "temporal_role.X__month"), or "time"
{
  "select": [{ "expression": {"measure": "measure.revenue"}, "as": "revenue_usd" }],
  "order_by": [{ "field": "revenue_usd", "direction": "DESC" }]
}
```

### Unknown expression keys are rejected

A typo on a top-level expression key surfaces `INVALID_EXPRESSION_KEY`
with `closest_matches`:

```jsonc
{ "expression": { "meaure": "measure.revenue" } }
// -> INVALID_EXPRESSION_KEY, closest_matches: ["measure"]
```

## SelectItem

```jsonc
{
  "expression": <SelectExpression>,  // see below
  "as": "<output alias>"             // required when no deterministic default
}
```

Four unambiguous slips are rewritten, not refused. `validate`, `compile` and `execute` (the MCP
`execute` tool in every mode) add a `QUERY_SHORTHAND_NORMALIZED` warning naming the canonical
form; `plan` accepts the same shapes in its `query` but returns no such warning, and its
`best.query_ir` is the canonical form. The item must hold exactly the keys shown; a rewrite
never drops a key. A dimension moved to `group_by` leaves `select`, so later select items are
numbered in the rewritten `select`: an unaliased expression after it gets a default alias
(`expr_N`) and a diagnostic path (`select[N]`) by that new position. Give it an `as`:

| Sent | Treated as |
|---|---|
| `{ "metric": "..." }` as the select item itself, with no `expression` wrapper (plus optional `as`) | `{ "expression": {"kind": "metric", "metric": "..."}, "as": ... }` |
| `{ "measure": "...", "aggregation": "sum" }` as the select item itself (`aggregation` optional, plus optional `as`) | `{ "expression": {"kind": "measure", ...}, "as": ... }` |
| `{ "dimension": "..." }` as the whole select item (no `as`) | that id added to `group_by[]`, whatever it already holds |
| `{ "expression": { "dimension": "..." } }` as the whole select item (no `as`), when `group_by` is empty or already lists it | that id on `group_by[]` |

Everything else is refused with `INVALID_EXPRESSION_AST`, and the message shows the canonical
form: an item naming more than one of `metric`, `measure` and `dimension`, a dimension item
carrying `as` or any other key (a `group_by` entry has no alias), `expression` beside `metric`,
`measure` or `dimension`, and any other key on a bare `metric` or `measure` item. A
`{ "expression": { "dimension": "..." } }` item beside a `group_by` naming other dimensions is
refused (`MOVE_DIMENSION_TO_GROUP_BY`).

## SelectExpression (discriminated union)

Most shapes carry an explicit `kind`. The runtime also accepts two kindless
shorthands for the most common cases:

| Shape | Example |
|---|---|
| Measure reference | `{ "measure": "measure.revenue_usd" }` or `{ "kind": "measure", "measure": "..." }` |
| Aggregate override | `{ "kind": "aggregate", "measure": "...", "aggregation": "sum" }` |
| Metric reference | `{ "metric": "metric.jaffle.aov" }` or `{ "kind": "metric", "metric": "..." }` |
| Arithmetic | `{ "kind": "arithmetic", "op": "divide", "left": {...}, "right": {...} }` |
| Ratio | `{ "kind": "ratio", "numerator": {...}, "denominator": {...} }` |
| Case | `{ "kind": "case", "whens": [{"when": {...}, "then": {...}}], "else": {...} }` |
| Aggregate-if | `{ "kind": "aggregate_if", "aggregation": "count", "condition": {...} }` or with `"value": {...}` for sum/avg/min/max. Compiles to `COUNT_IF` / `SUM_IF` on Snowflake, portable `<AGG>(CASE WHEN cond THEN value END)` elsewhere. Column refs inside `condition` / `value` must specify `entity` or `table` (no surrounding measure to inherit from). It aggregates the rows of the value's entity (all `value` columns share it; without a value column, the condition's columns must share one entity). `condition` may also read any entity that entity reaches over declared many-to-one or one-to-one relationships, on the route a `where` filter on that entity takes. A value row with no match on that route never satisfies the condition: for each such entity, a top-level `and` term must compare one of its columns with `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not_in` or `IS NOT` null, and a condition such a row could satisfy (`IS NULL`, an `or` with the value's own column) is refused with `UNSUPPORTED_CONDITIONAL_AGGREGATE`. So is a condition across a one-to-many, many-to-many, bridge or time-valid hop, or over two routes with no path preference. Every dimension declared over a column read by `condition` or `value`, including on the measure's own entity, is governed as in a `where` filter or `group_by`: a matching `deny`, `redact` or `hidden` object policy refuses the query with `POLICY_DENIED` before SQL is rendered. Own-entity columns with no declared dimension remain allowed. |
| Between | `{ "kind": "between", "expr": {...}, "low": {...}, "high": {...} }` — sugar for `expr >= low AND expr <= high`. Use `kind: "not_between"` or `negated: true` for the inverted form (`expr < low OR expr > high`). Desugared at parse time; the kind does not appear in the lowered IR. |
| Literal | `{ "kind": "literal", "value": 0 }` |
| Prior period | `{ "kind": "prior_period", "input": {...}, "offset": {"unit": "month", "value": 1} }` |
| Rolling | `{ "kind": "rolling", "input": {...}, "window": {"unit": "day", "value": 28} }` |
| Cumulative | `{ "kind": "cumulative", "input": {...} }` |
| Period-to-date | `{ "kind": "period_to_date", "input": {...}, "period": "month" }` |
| Conversion | `{ "kind": "conversion", "base": {...}, "converted": {...}, "entity": "...", "window": {"unit": "day", "value": 7}, "matching_mode": "first_converted_after_base" }` — a converted event counts when `base <= converted < base + window` (7 × 24 hours here, not calendar days). |

Comparisons (`kind: "comparison"`) with a literal `null` on either side lower
`=` / `IS` to `IS NULL` and `!=` / `<>` / `IS NOT` to `IS NOT NULL`. This applies
inside CASE and aggregate-if conditions (including a metric predicate's input),
post-aggregation expressions, segment membership, and relation filters and joins,
as well as `where` filters.
Ordering (`<`, `<=`, `>`, `>=`) and LIKE comparisons with a null literal refuse
with `INVALID_QUERY` and the `USE_NULL_TEST_OR_SCALAR` recovery hint, since they
would always evaluate to unknown in SQL. `IS DISTINCT FROM`, `IS NOT DISTINCT FROM`
and `<=>` already handle null, so they pass through unchanged. A comparison
between two nullable columns retains ordinary SQL three-valued semantics, as does
a comparison against a computed null such as `NOT(NULL)`: comparing it to `FALSE`
with `!=` evaluates to unknown (NULL) and retains no rows when used as a filter.
`NOT(NULL)` renders as `CAST(NULL AS Nullable(Bool))` on
ClickHouse and `CAST(NULL AS BOOLEAN)` on other warehouses.
Boolean `and` / `or` expressions require at least two arguments; zero or
single-argument forms, including negated forms, are refused before SQL with
`INVALID_EXPRESSION_AST` and the message "and/or need at least two arguments".
A `metric_predicate` whose `value` is null refuses with `INVALID_METRIC_PREDICATE`:
its input reads `0` for an entity with no rows and `NULL` for one with no data, so
a count of none is `= 0`, and a null test belongs inside the input as an
`aggregate_if` condition.

## Scalar `call` expressions

`{"kind":"call","name":"ROUND","args":[<expression>,{"kind":"literal","value":1}]}`
applies a scalar function. Names are case-insensitive. Each warehouse accepts
the common names below plus its additions; aggregate, window and table
functions must use their semantic expression forms instead of `call`.
`distinct` is not supported on scalar calls. An unsupported name returns
`INVALID_EXPRESSION_AST` with `details.allowed` equal to the warehouse's
accepted set, including `CAST`.

Common names: `ABS`, `CAST`, `CEIL`, `CEILING`, `COALESCE`, `CONCAT`, `DATE_DIFF`, `EXP`,
`FLOOR`, `LENGTH`, `LN`, `LOG`, `LOWER`, `NULLIF`, `POWER`, `REPLACE`, `ROUND`,
`SQRT`, `SUBSTR`, `SUBSTRING`, `TRIM`, `UPPER`.

| Warehouse | Additions or exceptions |
| --- | --- |
| DuckDB, MotherDuck, DuckLake | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `JSON_EXTRACT`, `JSON_EXTRACT_STRING`, `SPLIT`, `STRING_SPLIT`, `STR_SPLIT` |
| Postgres | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT` |
| Snowflake | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `SPLIT` |
| BigQuery | `LEFT`, `RIGHT`, `JSON_EXTRACT`, `SPLIT` |
| Databricks | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `SPLIT` |
| Athena | `DATE_TRUNC`, `JSON_EXTRACT`, `SPLIT` |
| ClickHouse | No additions; `TRIM` is excluded because its plain uppercase spelling is unavailable |

Use each warehouse's scalar argument signatures. For example, Athena `LOG`
takes a base and a value. Engine-generated SQL has a separate function list;
it does not advertise functions that a client can call.

Portable date differences use exactly three args:

```json
{"kind":"call","name":"DATE_DIFF","args":[
  {"kind":"literal","value":"day"},
  {"kind":"column","column":"opened_at","entity":"entity.order"},
  {"kind":"column","column":"closed_at","entity":"entity.order"}
]}
```

The first arg must be a string literal unit: `minute`, `hour`, `day`, `week`,
`month`, `quarter` or `year` (case-insensitive). The result is end minus start,
counting calendar unit boundaries rather than elapsed durations. The `week`
exception is the calendar day difference divided by seven, truncated toward
zero; it does not count Sunday or Monday week boundaries.

BigQuery converts both endpoints to `DATETIME` before taking the difference.
For `TIMESTAMP` endpoints, calendar boundaries are counted in UTC, so
23:00 on January 1 to 01:00 on January 3 returns two days, preserving NULLs.

| Warehouse | Supported units | Refused units |
| --- | --- | --- |
| DuckDB, MotherDuck, DuckLake, Postgres, Databricks | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` | None |
| Snowflake, BigQuery, ClickHouse | `minute`, `hour`, `day`, `month`, `quarter`, `year` | `week` |
| Athena | None | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` |

Athena's native function counts complete elapsed units; Snowflake, BigQuery and
ClickHouse count calendar week boundaries. Those calls return
`INVALID_EXPRESSION_AST` with an unsupported-function message naming the
warehouse and unit, including in `validate` mode and package loading.

For supported calls, if either endpoint is NULL, the result is NULL and is
excluded from averages, never replaced with zero. ClickHouse casts both endpoints
to `Nullable(DateTime64(6))` so this holds even with `cast_keep_nullable=0`, while
preserving pre-1970 dates: `1950-01-01` to `2024-01-01` is 74 years. The same
shape works in query selects, package measure expressions and `aggregate_if`
values. Wrong arity, non-literal units and unknown units return
`INVALID_EXPRESSION_AST`, including in `validate` mode, with the required shape
and recognized units.

Numeric conversion uses exactly two args:

```json
{"kind":"call","name":"CAST","args":[
  {"kind":"column","column":"amount_text","entity":"entity.order"},
  {"kind":"literal","value":"DOUBLE"}
]}
```

The type must be a string literal naming `DOUBLE`, `DECIMAL(p,s)`, `INTEGER`,
`BIGINT` or `VARCHAR` (case-insensitive). Decimal precision is 1–38 and scale
is 0–precision. `INTEGER` and `BIGINT` both select a signed 64-bit type.
The rendered targets are:

| Warehouse | `DOUBLE` | `DECIMAL(p,s)` | `INTEGER`, `BIGINT` | `VARCHAR` |
| --- | --- | --- | --- | --- |
| DuckDB, MotherDuck, DuckLake, Snowflake, Athena | `DOUBLE` | `DECIMAL(p,s)` | `BIGINT` | `VARCHAR` |
| Postgres | `FLOAT8` | `DECIMAL(p,s)` | `BIGINT` | `VARCHAR` |
| BigQuery | `FLOAT64` | Refused (`INVALID_EXPRESSION_AST`) | `INT64` | `STRING` |
| Databricks | `DOUBLE` | `DECIMAL(p,s)` | `BIGINT` | `STRING` |
| ClickHouse | `Nullable(Float64)` | `Nullable(DECIMAL(p,s))` | `Nullable(Int64)` | `Nullable(String)` |

BigQuery only supports parameterized decimal types on columns and script
variables, not CAST targets. Decimal casts are refused rather than silently
discarding the authored precision and scale; use `DOUBLE` for approximate
conversion or declare a parameterized decimal column in the warehouse.
See [BigQuery parameterized type rules](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/data-types#parameterized_data_types).
ClickHouse emits
nullable targets so NULL inputs remain NULL. Invalid conversions fail execution;
CAST does not silently return NULL. `DATE`, `TIMESTAMP`, other target types, non-literal targets and
`TRY_CAST` are refused. The same AST works in package expressions,
conditional aggregates and post-aggregation expressions.

Scalar-call argument types and overload resolution are checked by the warehouse
at execution, for query, package and relation-pipeline expressions alike.
Compilation checks the allowed function name and CAST/DATE_DIFF shapes without inferring
argument categories from literals, dimensions or nested calls. Use CAST when an
explicit conversion is required. Warehouse execution failures use the stable
`QUERY_EXECUTION_ERROR` code and remain redacted.

A top-level request select containing only literals, literal arithmetic or casts
of literals, with no grouping, where, time or metric filter, returns `INVALID_QUERY` with
`details.reason: "literal_only_select"` and the message “A select of literals
only reads no data; add a measure, a group_by dimension or time”.

## MetricFilter expressions

Different shape from `select`. The most common pattern is `kind: metric_predicate`:

```jsonc
{
  "expression": {
    "kind": "metric_predicate",
    "entity": "entity.jaffle_customer",
    "scope_mode": "entity_only",
    "input": { "metric": "metric.jaffle.lifetime_spend" },
    "op": ">=",
    "value": 500
  },
  "op": "=",
  "value": true
}
```

`scope_mode` is either `contextual` (default for query-time) or
`entity_only`. `time_alignment` is one of `same_query_period`,
`query_window`, or `rolling_window_in_period`.

## WhereFilter

```jsonc
{
  "field": "dimension.jaffle_store_name",  // dimension id
  "op": "=",                                // default "="
  "value": "Philadelphia"
}
```

Supported `op` values (all compile end-to-end):
`=`, `!=`, `<`, `<=`, `>`, `>=`, `IN`, `NOT IN`, `LIKE`, `NOT LIKE`,
`IS NULL`, `IS NOT NULL`.

`value` rules:

- Comparison and LIKE ops take a scalar (`string`, `number`, `boolean`).
  A list is rejected with `INVALID_QUERY` and a `USE_IN_FOR_LIST_VALUE`
  recovery hint: use `IN` / `NOT IN` to match several values.
- `IN` / `NOT IN` take a list of scalars. A bare scalar is accepted and
  treated as a one-element list (strings are never character-split). An
  empty list compiles to constant `FALSE` (`IN`) / `TRUE` (`NOT IN`)
  instead of erroring. `value: null` with `IN` / `NOT IN` is rejected
  with `INVALID_QUERY` + a `USE_LIST_VALUE_OR_NULL_TEST` recovery hint.
- `IS NULL` / `IS NOT NULL` ignore `value` entirely — omit it.
- `IS` / `IS NOT` accept only `null`, `true` or `false`. Other values are
  rejected before execution with `INVALID_QUERY` and a
  `USE_EQUALITY_FOR_SCALAR` recovery hint: use `=` / `!=` for scalar comparisons.
  This applies to plain dimensions, parent dimensions and metric filters
  on every backend.
- `value: null` with `=` (or `IS`) lowers to `field IS NULL`; with
  `!=` / `<>` / `IS NOT` it lowers to `field IS NOT NULL`. Ordering
  (`<`, `<=`, `>`, `>=`) and LIKE ops against `null` are rejected with a
  structured `INVALID_QUERY` and a recovery hint, since they would be
  always-UNKNOWN in SQL three-valued logic.
- A dimension looked up through a many-to-one or one-to-one relationship is
  NULL on a row whose lookup found no match, and a filter treats the row as
  any other NULL: `IS NULL` keeps it (an anti-join, such as boardings with no
  crew-roster row), while `=`, `!=`, `IN` and `NOT IN` exclude it. This holds
  wherever the dimension is read: a `group_by`, a `where`, a measure's own filter,
  a segment, an `aggregate_if` or a measure expression, with or without metric
  filters. A time role read through a lookup leaves such a row out, as before;
  so do a metric filter's own query and the entities its set is matched on, a
  distribution's per-entity values, a conversion, and a dimension any rollup of
  the measure's model holds, even at a grain that rollup can never answer (a
  rollup of another model, or any rollup in a query of dimensions alone, keeps
  the row).
  ClickHouse is the exception: its lookups
  stay inner joins, so it drops such a row from every query that reads the
  looked-up dimension.
- Objects are rejected — inline expression thresholds belong in
  `metric_filters` (`metric_predicate`).

A positive child-dimension filter on a parent-grain measure means "parents with at
least one matching child". It lowers to correlated `EXISTS`, so multiple matching
children never multiply a parent count or sum. This also applies to an aggregate's
own `filter`, and to non-temporal paths that look up a parent before reaching its
children or join on an alternate key. Each hop must declare `N:1`, `1:N` or `1:1`;
unknown, unsafe and temporal paths retain their refusals. A lookup-before-child
or alternate-key path is the route the route rule chose; when the rule can't
choose, the query is refused with `AMBIGUOUS_PATH`; a shorter route does not
establish which children the filter means. This also applies beside a lookup and to an
aggregate's own filter.
ClickHouse retains a deduplicated-parent leaf for servers without correlated
subqueries. Key-based descents retain their existing SQL shape, including
beside lookup selections, groupings and filters; those lookups remain inner
joins. It refuses paths that look up a parent before reaching children and
paths joined off the parent's declared key, including beside a lookup, with
`MIXED_GRAIN_INVALID`.

Every leaf of an expression is rewritten the same way. An `aggregate_if` keeps
the rows of its own entity that have a matching child, so a sum of order amounts
under a refund-type filter adds each order once, and each operand of a `ratio`
or arithmetic gets its own `EXISTS`. The route, negation, single-crossing,
row-policy and ClickHouse rules above apply to each leaf. Grouped by a child
dimension, an `aggregate_if` follows the grouped rule: only `count_distinct`.
Its rows have the grain the measures of its entity's model declare. When that
grain is finer than the entity's key, the rewrite refuses it with
`MIXED_GRAIN_INVALID`, as it refuses those measures. On ClickHouse, a model
without measures leaves the grain unknown, so only `count_distinct`, `min` and
`max` are answered there.

Grouped child dimensions retain their distinct-parent count rules; summing a
parent amount by a child dimension or reading a child measure expression at
parent grain remains refused. Under a row policy these queries are refused with
`POLICY_DENIED`, as before.

### Child groups

"Customers with an item that is a beverage and costs over 5" may mean one item that
is both, or a beverage and some item over 5. Only the question can say which, so the
query states it. A child group is a `where` item of its own:

```jsonc
{
  "child": "entity.shop_item",              // entity id of the child
  "match": "any",                           // "any" | "none"
  "where": [                                // filters, as above
    {"field": "dimension.shop_item_product_type", "op": "=", "value": "beverage"},
    {"field": "dimension.shop_item_price", "op": ">", "value": 5}
  ]
}
```

- `any` keeps a row of the measure's entity when at least one of its child rows meets
  every condition: a correlated `EXISTS` over the conjunction. `none` keeps it when no
  child row does: `NOT EXISTS`, so a row with no child rows at all is kept.
- Conditions are dimensions of the child, or of a declared many-to-one or one-to-one
  lookup from it. A lookup that finds no row reads NULL, and NULL fails a comparison:
  under `none`, a child row with a NULL value never excludes its parent.
- Several groups are separate subqueries, ANDed with the other `where` items. Two
  groups on one child mean separate child rows; one group with both conditions means
  the same row.
- One child scope per query. Beside a group, nothing else may cross a one-to-many hop: a
  plain filter (on that child or another), a grouping by a child dimension, or a measure's
  own `filter`. Groups on different children, or reaching one child by different routes,
  are refused too: an item and a payment of a customer's orders may mean one order or
  any, and neither group says which. Each is `MIXED_GRAIN_INVALID`.
- The child must sit across a one-to-many hop from each measure's entity. Its route
  follows [the route rule](PACKAGE_AUTHORING.md#the-route-rule): with several routes and
  no `graph.path_preferences` row recording one, it is refused with `AMBIGUOUS_PATH`.
- Refused with `INVALID_QUERY`: a child reached only through lookups, or the measure's
  own entity (use a plain filter); nested groups; a condition that is not on the child
  or a lookup from it; a condition whose lookup reads a table the route already reads;
  and a group in a query without a measure or beside a conversion. A segment's
  membership refuses a group with `INVALID_SEGMENT`.
- A group never reads a rollup, and the measure must meet the same rules as under a plain
  child filter (one value per row of its entity; no window, distribution or metric
  predicate across the hop).
  Under a row policy the query is refused with `POLICY_DENIED`.
  Under [restricted metric grants](QUERY_API.md#restricted-metric-grants), explicit
  groups are refused with `RESOURCE_ACCESS_DENIED`: metric and dimension grants do not
  authorize a caller-selected child entity scope.
- ClickHouse answers one `any` group on the child's own columns with its de-duplicated
  parent leaf. A `none` group (a NULL-safe anti-join is unproven there), several groups,
  or a lookup from the child are refused with `MIXED_GRAIN_INVALID`.

Plain filters on a child:

- One positive plain filter reaching a child keeps its meaning: an `any` group of one.
- `AMBIGUOUS_CHILD_SCOPE` is raised only for a query with no group whose only conditions
  across a one-to-many hop are plain `where` filters, all reaching one child entity by one
  route (a lookup from the child counts as that child), each with an operator a group can
  restate exactly (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `IN`, `NOT IN`, `LIKE`,
  `NOT LIKE`, `IS NULL`, `IS NOT NULL`). Then it takes one of two shapes:
  - two or more filters, none negated: `same_row` (one `any` group of all of them) or
    `separate_rows` (an `any` group each);
  - exactly one filter, negated (`!=`, `<>`, `NOT IN`, `NOT LIKE`, `IS NULL`, a null
    value, or on a boolean anything but `= true`): `any_not` (an `any` group with the
    condition as written, "has an item that is not a beverage") or `none` (a `none` group
    with its complement: `=` for `!=`, `IN` for `NOT IN`, `LIKE` for `NOT LIKE`,
    `IS NOT NULL` for `IS NULL`; "has no beverage item").
- Every other shape keeps `MIXED_GRAIN_INVALID` with no clarification: a negated filter
  beside another filter on the child, two negated filters, filters on different children,
  and `IS` / `IS NOT` with a boolean value, `IS [NOT] DISTINCT FROM`, `<=>`, `ILIKE` or
  `NOT ILIKE` on a child.
  Other non-null `IS` / `IS NOT` operands fail earlier with `INVALID_QUERY` and
  `USE_EQUALITY_FOR_SCALAR`, before child-scope analysis.
- The refusal carries `details.clarification`:

  ```jsonc
  {
    "kind": "child_scope",
    "apply": ["query"],
    "question": "Do Product type = \"beverage\" and Price > 5 apply to the same Order item or to separate ones?",
    "options": [
      {"id": "same_row", "meaning": "One Order item meets ...", "where": [/* whole rewritten where */]},
      {"id": "separate_rows", "meaning": "Each of ... may hold on a different Order item.", "where": [/* ... */]}
    ]
  }
  ```

  Each option's `where` is the query's whole `where`, with the other items unchanged;
  resend it as is. `recovery_hints` carry the same options.
- A clarification is offered only when every reading answers for this caller. The engine
  binds each option's `where` once (every measure, the warehouse's child-group rules, row
  policies) and runs the caller's semantic policies on it (metric constraints such as
  `required_where` and `allowed_where`, object access). If any option is refused (a
  measure whose own rows are the child, ClickHouse refusing a reading, a required filter
  that a group no longer meets as a plain filter), the query is `MIXED_GRAIN_INVALID`
  without a clarification, and its message names that option and its error code.
- A group on the child must take the plain filters' own route: the route from the
  measure's entity to the child, and from the child to a filter's lookup. When it would
  take another route, or none the package records, the query is refused with
  `MIXED_GRAIN_INVALID`, naming the `graph.path_preferences` row that records the filters'
  route.
- A measure's own `filter` keeps its rules: one positive condition across a hop means
  `EXISTS`, and a negated one is `MIXED_GRAIN_INVALID`.

## OrderBy

```jsonc
{
  "field": "<select_alias | group_by_dim_id | time_axis_alias | 'time'>",
  "direction": "ASC" | "DESC"  // default ASC
}
```

The runtime rejects any `field` that does not resolve, with
`INVALID_ORDER_BY` and a list of available aliases.

## TimeBlock

```jsonc
{
  "temporal_role": "temporal_role.jaffle_order_time",
  "grain": "month",        // "", day, week, month, quarter, year, hour, minute
  "start": "2024-01-01",   // optional ISO date/timestamp; INCLUSIVE (>=)
  "end":   "2025-01-01",   // optional ISO date/timestamp; EXCLUSIVE (<)
  "range": { "last": { "unit": "day", "value": 90 } },   // alternative to start/end (object only)
  "fill":  true,            // emit dense rows for grains with no data
  "calendar_id": "default"
}
```

**Bounds are half-open: `start` is inclusive (`>=`), `end` is exclusive
(`<`).** The window is `[start, end)`. To cover calendar year 2024, use
`start: "2024-01-01"`, `end: "2025-01-01"` — an `end` of `"2024-12-31"`
would silently exclude December 31. Half-open bounds make adjacent
windows compose without overlap or gaps.

**A window without a `grain` is one total.** With `start` and/or `end` (or `range`) and no `grain`,
the query returns one total over the window for each `group_by` group, with no time column, even
for a window inside one day. The response says so in `assumptions` and sets
`time_shape: "window_total"`. Set `grain` for one row per period. A `time` block with no bounds and
no grain still groups by the raw timestamp, and so do queries that need a time axis (rolling,
prior-period and similar expressions) and queries with a metric predicate, including one in an
aggregate's `filter` or a metric recipe.

Buckets and bounds are in the temporal role's `timezone` (UTC by default).
On DuckDB, MotherDuck, DuckLake and Postgres that holds for zone-aware
(`TIMESTAMP WITH TIME ZONE`) columns too: the query runs with the session time
zone set to the role's zone.
For other warehouses, see "`times:` — temporal roles" in
[PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md).

`fill: true` requires a `grain`. `range` is mutually exclusive with
`start`/`end`. `range.last` is strictly an object — the string shorthand
(`"90 days"`) is rejected with `INVALID_QUERY` + a `USE_OBJECT_SHAPE`
recovery hint. `unit` must be one of `day`, `week`, `month`, `quarter`,
`year` (sub-day relative ranges are not supported); `value` must be a
positive integer.

## PolicyContext

```jsonc
{
  "environment": "prod",
  "audience": "internal",
  "roles": ["sales", "csm"],
  "now": "2026-05-21T00:00:00Z"   // anchors relative time ranges
}
```

The default `HeaderPolicyContextResolver` lets callers self-assert roles
in headers or body `policy_context` — operators should swap in an
identity-derived resolver for production.

## Limits

```jsonc
{
  "statement_timeout_ms": 30000,
  "max_rows": 10000
}
```

Unknown keys are silently dropped so new limits can land without
breaking existing clients.

## Worked example — same-store 7d conversion

```jsonc
{
  "version": 1,
  "select": [
    {
      "expression": {
        "metric": "metric.jaffle.session_to_order_conversion_rate_7d_same_store"
      },
      "as": "same_store_conversion_rate_7d"
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_session_started_at",
    "grain": "month"
  },
  "order_by": [
    {
      "field": "temporal_role.jaffle_session_started_at__month",
      "direction": "ASC"
    }
  ]
}
```

This conversion metric is anchored on `jaffle_session_started_at`. Trying
to filter it on `jaffle_order_time` raises `INVALID_TEMPORAL_BINDING` at
validate (see `recovery_hints[0].kind == "filter_on_conversion_anchor"`).

## Worked example — `metric_filters` predicate

```jsonc
{
  "version": 1,
  "select": [
    { "expression": { "metric": "metric.jaffle.orders" }, "as": "filtered_orders" }
  ],
  "metric_filters": [
    {
      "expression": {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "scope_mode": "entity_only",
        "input": { "metric": "metric.jaffle.lifetime_spend" },
        "op": ">=",
        "value": 500
      },
      "op": "=",
      "value": true
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month"
  }
}
```

## Period shifts (`prior_period`)

Inline period-shifted projections — the "year-over-year revenue"
shape — are supported in two interchangeable forms:

### Shorthand (recommended for ad-hoc YoY/WoW/MoM)

```jsonc
{
  "version": 1,
  "select": [
    { "expression": { "measure": "measure.jaffle.revenue_usd" }, "as": "revenue_usd" },
    {
      "expression": {
        "kind": "prior_period",
        "measure": "measure.jaffle.revenue_usd",
        "offset": -1,
        "grain": "year"
      },
      "as": "revenue_prior_year"
    }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month"
  }
}
```

- `measure` — measure id; wrapped as an aggregate (default `sum`)
- `offset` — signed integer. `-1` = the immediately prior period at
  `grain`. The sign communicates direction; the magnitude is the
  number of `grain` steps.
- `grain` — one of `day`, `week`, `month`, `quarter`, `year`. The
  shorthand normalises to `offset.value = abs(offset)`,
  `offset.unit = grain` internally.
- `aggregation` (optional) — defaults to `sum`.

### Canonical IR form (what config recipes emit)

```jsonc
{
  "expression": {
    "kind": "prior_period",
    "input": {
      "kind": "aggregate",
      "measure": "measure.jaffle.revenue_usd",
      "aggregation": "sum"
    },
    "offset": { "unit": "year", "value": 1 }
  },
  "as": "revenue_prior_year"
}
```

### Lowering — LAG window over the time grain

Both shapes compile to a `LAG(<measure>, N) OVER (ORDER BY <time>)`
window where `N` is the offset expressed in grain rows. For a
`grain: "year"` shift against a query at `grain: "month"`,
`N = 12`. The query MUST set `time.grain`; the layer raises
`INVALID_TEMPORAL_ROLE` otherwise. This matches the curated
`metric.sales.prior_week_revenue_direct` lowering — see
`semantic_rails/compiler_parts/post_aggregation.py:_compile_offset_window_expr`.

The dense-fill machinery is engaged automatically when an offset
window appears, so rows missing in the source are filled before the
LAG runs. Additive measures (`sum`, `count`, `count_distinct` over an
additive, event-count or entity-count measure) fill with `0` while they
have data in scope (see "Empty groups" below); every other measure fills
with `NULL`, because the value of `AVG`, `MIN`, `MAX` or a semi-additive
snapshot over no rows is undefined, not zero.
The shift can therefore reach into months that have no orders, and a
gap there is reported as a gap rather than as a measurement.

### Worked example file

[`examples/inline_yoy.json`](../examples/inline_yoy.json) is the
committed worked example for inline YoY. The schema and runtime
regression tests under
`tests/semantic_rails/test_examples.py` round-trip every published
example file through `validate` and `compile`.

### Silent-drop guard

If any select expression with a recognised `kind` (e.g.
`prior_period`, `rolling`, `period_to_date`, `ratio`, …) does not
survive normalization into the compiled output — typically because
of a future bug in the parser or compiler — the layer emits a
`WARNING` with code `EXPRESSION_NORMALIZED_AWAY` that names the
position (`select`/`metric_filters`), the dropped expression
payload, and the kind it was normalized to. Never silently turn a
YoY projection into a duplicate of the current period.

## Empty groups: NULL or 0

Sometimes there is no data (NULL), and sometimes there is data of nothing (0). A group with
no rows reads one or the other, by one rule, in every query:

| Measure | An empty group reads | When nothing is in scope |
|---|---|---|
| `sum`, `count`, `count_distinct` over an additive, event-count or entity-count measure | `0` | `NULL` |
| `avg`, `min`, `max`, `median`, `percentile` | `NULL` | `NULL` |
| semi-additive measures (stocks), distinct populations, and measures with `additive: false` | `NULL` | `NULL` |

A measure has data in scope when at least one group of the answer holds a value: a sum with a
non-NULL amount, or a count above zero. The scope is the measure's own filters, the query's
`where` filters and policy row filters, before the `group_by`. Plain time leaves check
for data outside the query's time bounds (DuckDB and Postgres; see time coverage below).
Where a measure has data in scope,
a group with no rows reads `0`: a store with orders but no refunds has 0 refunds, and a
month whose orders all have a NULL amount has a revenue of 0. Where it has none, every group
reads `NULL`: with no refunds anywhere in scope, no store has "0 refunds", because nothing
says refunds were recorded. An average, minimum or maximum of nothing is undefined, and a
stock has no value for a period nobody observed, so neither is ever made zero.

- **Arithmetic** settles each operand first, then combines them, so `goods + shipping` by
  refund type returns numbers even where one column is NULL for a type. An operand with no
  data in scope stays `NULL` and so does the result: `revenue - refunds` is `NULL` if refunds
  were never recorded. Division by zero is `NULL`.
- **A `metric_predicate` applies the rule to every entity alike.** An operand reads `0` for an
  entity with no match where its measure has data somewhere in the predicate's scope, and
  `NULL` where it has none, whether that entity has rows or none at all. So
  `orders - returned_orders > 1` keeps a customer with 2 orders and no returns, as a
  `metric_filter` on the same expression does, and `large_orders = 0` ("customers with no
  large orders") keeps every customer without one when some order in scope is large, and
  keeps nobody when none is: with no large order anywhere in scope there is no data, not a
  count of zero. `NULL` fails every threshold, `= 0` and `< 1` included. Only a count or sum
  threshold that 0 passes reaches an entity with no rows at all, and a distinct count of a
  population is 0 for one whether or not the scope has data.
- **Filters narrow the scope.** With `where: store = 'x'`, a measure that has no rows at
  store x reads `NULL`, even though the same store reads `0` in a `group_by: store` answer. A
  filter value that matches nothing (a misspelled `product`) reads `NULL`, not a confident 0.
- **Time coverage bounds zero filling.** Bounded plain time leaves check for observation
  outside the query's window under the same authored, query and policy row filters. For
  fill, dense series and combined leaves, an empty bucket inside the base relation's loaded
  range reads `0`; an empty bucket before its first loaded timestamp or after its last
  reads `NULL`. Coverage uses the whole base relation under policy filters, ignoring
  measure and query filters, and excludes future timestamps from its upper edge. The
  cutoff compares UTC instants: timezone-aware columns preserve their instant, and
  naive columns use their declared storage zone (`column_timezone`, then `timezone`,
  defaulting to UTC). That cutoff is the only instant comparison: buckets, calendar joins
  and the window keep each leaf's own time frame, and the loaded range is the lowest and
  highest of the leaf's own bucket. Coverage gates only
  zero substitution: populated sums and positive counts always survive, including
  NULL time keys and future-dated rows.
  Filled, dense-series (rolling, prior-period) and combined plans, bounded or not, read
  the base relation even when rollups are available, so routing cannot change their
  coverage answers. Other routed
  aggregates, nested, fanout and predicate sources retain the window observation test.
  Coverage uses data alone. Performance guidance includes the emitted observation and
  coverage reads as scans without request-window bounds; narrowing the requested window
  does not bound those reads.
  Coverage and the outside-window check run only on DuckDB (with MotherDuck and DuckLake)
  and Postgres, whose execution is tested. On Snowflake, BigQuery, Databricks, Athena and
  ClickHouse an empty bucket reads `0` only while the measure has data inside the window,
  and rollups route as they would without coverage.
- **An ungrouped distinct-population count over nothing reads `0`, with no warning.** That is a
  known limitation: a count of distinct customers under a `where` that matches no rows returns
  `0`, not `NULL` with `NO_DATA_IN_SCOPE` as the rule says. An empty group of a grouped answer
  does read `NULL`
  ([issue #203](https://github.com/semantic-rails/semantic-rails/issues/203)).
- **An empty table has no data** to call zero: a measure over it reads `NULL`.
- A metric filter such as `item_count = 0` sees the settled value, so it keeps the orders
  with no items.

When an output that is a sum, count or distinct count (or a sum or difference of them) reads
`NULL` on every returned row, or nothing came back with no time bounds and no metric filter,
the response carries one `NO_DATA_IN_SCOPE` warning that names those outputs. A `prior_period`,
ratio or rolling output never gets it: it can be `NULL` while its measure has data. It costs
no extra query, and a clipped result (`truncated`) never gets it.

ClickHouse fills an unmatched outer-join field with a type default (0 or an empty string)
unless the join yields NULLs, so every ClickHouse statement ends with
`SETTINGS join_use_nulls = 1`.

## Dense fill (`time.fill`)

`time.fill` toggles dense-row emission for a grained query. The
field is documented on `TimeBlock` above; this section explains the
semantics in narrative.

### What it does

When `fill: false` (the default), the output contains one row per
grain bucket that actually carries data — sparse grains (months
with no orders, days with no sessions) are simply absent from the
result. This is the natural shape of a `GROUP BY` over the source
fact table.

When `fill: true`, the runtime joins the aggregated result against a
dense calendar spine generated for the requested `temporal_role` /
`grain` / time window. Buckets with no source rows still appear in
the output. What lands in the measure column depends on whether zero
is that measure's honest value for "no rows contributed":

| Measure | Filled with | Why |
|---|---|---|
| `sum` / `count` / `count_distinct` over an additive, event-count or entity-count measure | `0`, while the measure has data in scope; else `NULL` | Zero is the additive identity — summing no rows really is 0 — but only where the measure has data (see "Empty groups"). |
| `avg`, `min`, `max`, `median`, `percentile` | `NULL` | Undefined over no rows. A filled `0` would be a fabricated measurement — a `min` below every value actually observed. |
| semi-additive measures (snapshots, period-to-date, rolling balances) | `NULL` | A snapshot for a period that was never observed is unknown, not empty. |
| ratios, conversion rates and other null-preserving expressions | `NULL` | A period with no denominator has no rate; `0` would read as a 0% rate. |

A query with a `distribution` refuses `fill: true`; without fill, periods with no data are
omitted. A `distribution` is also refused when its input or a metric filter has a `rolling`
or `prior_period` window, whose dense per-entity series would count entities in periods where
they have no rows.

`fill: true` requires a `grain` — the calendar spine needs a step
size — and it is engaged automatically by features that depend on
dense rows (for example, the inline `prior_period` LAG window in the
"Period shifts" section above).

### Which calendar fills

- A calendar the package authors for the requested `calendar_id` always
  fills (the `default` one when the query names none).
- With no authored `default` calendar, the **implicit calendar** fills a
  `default` query: a Gregorian day spine the engine generates in SQL, bucketed
  with the same truncation as the query's time column (calendar months,
  quarters and years; Monday weeks), in the temporal role's time zone. It spans
  the window for a query with `start` and `end`, and otherwise the data's first
  to last bucket, so an outlying date (say `1900-01-01`) widens the series
  rather than being dropped (a `9999-12-31` placeholder makes it millions of days long). The
  rendered SQL names it `implicit_calendar`.
  (An authored calendar fills only the days it holds, so it must cover the data.)
- Any other `calendar_id` (for example a fiscal calendar) needs that calendar
  authored. Without it the query is refused; it never falls back to Gregorian
  periods, and a `default` query never borrows another calendar's periods.
- The implicit calendar is not available on ClickHouse (it has no generated day
  series there), and on Athena a series is capped at
  10,000 days (about 27 years); past that the warehouse refuses the query.
  A query whose parts compile as separate sub-queries (for example with a
  `distribution` expression) is refused too. Author a calendar for those, for
  Sunday weeks, and for holidays or business days.

Two consequences apply to any calendar. The first rows of a `rolling` window
cover only the periods the series has (a 3-month window at the first month
holds one month), and a `group_by` value (a store) is filled for periods
before its first row too, so an additive `prior_period` there compares with 0.

With an explicit `start` and `end` and a calendar `date_day` declared and stored
as `date`, the spine holds every bucket that contains a day of the window,
including buckets without source rows.
So the first bucket's label can come before `start`: a week
that begins on the Monday before a mid-week `start`, or the month of a
mid-month `start`. Only rows inside `[start, end)` count toward any
bucket. When `date_day` is declared as `timestamp` or absent, the calendar
retains its original bucket-start bounds; a bucket that starts before `start`
can therefore be absent even when it contains source rows. Timestamp metadata
does not distinguish timezone-aware from timezone-naive storage, so changing
its bounds without a storage-type contract could shift empty calendar days.
For the `date` expansion, offset-bearing bounds use the temporal role's zone.
The series also keeps any populated bucket selected by the source
filter, since packages do not distinguish physical `TIMESTAMP` from
`TIMESTAMPTZ` columns; an extra empty calendar bucket may appear when those
two interpretations cross midnight.
For `date` calendars, fractional-second bounds keep their full precision when
deciding whether the window is empty and whether an exclusive end just after
midnight includes that day.

### Worked example — monthly query against a sparse table

The jaffle source has orders in some months but not others. A
month-grain query without dense fill skips empty months:

```jsonc
// fill: false (default) — sparse output, only months with orders
{
  "version": 1,
  "select": [
    { "expression": { "measure": "measure.jaffle.revenue_usd" }, "as": "revenue_usd" }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2016-01-01",
    "end":   "2018-01-01"
  }
}
```

Switch dense fill on and every month in the range appears, with
zeros for the gaps in a table that has orders elsewhere in the range (`end` is
exclusive, so `2018-01-01` covers through December 2017 without touching 2018):

```jsonc
// fill: true — dense output, every month in [start, end) present
{
  "version": 1,
  "select": [
    { "expression": { "measure": "measure.jaffle.revenue_usd" }, "as": "revenue_usd" }
  ],
  "time": {
    "temporal_role": "temporal_role.jaffle_order_time",
    "grain": "month",
    "start": "2016-01-01",
    "end":   "2018-01-01",
    "fill":  true
  }
}
```

Visually:

| date (sparse, `fill: false`) | revenue_usd |
| --- | --- |
| 2016-09 | 1234.56 |
| 2016-11 | 987.65 |
| 2017-01 | 4567.89 |

| date (dense, `fill: true`) | revenue_usd |
| --- | --- |
| 2016-09 | 1234.56 |
| 2016-10 | 0.00 |
| 2016-11 | 987.65 |
| 2016-12 | 0.00 |
| 2017-01 | 4567.89 |

### Interaction with other features

- The inline `prior_period` shape (`{kind: "prior_period", ...}` in
  a select) engages dense fill automatically so the LAG window
  reaches a contiguous row sequence; you do not need to set
  `fill: true` explicitly when adding a YoY/WoW/MoM column. See
  "Period shifts" above for the canonical worked example.
- `time.calendar_id` controls which calendar the spine is generated
  against — use it to switch between the default Gregorian calendar
  and any package-authored fiscal calendar (`metric.sales.*` family
  has a fiscal example).
- `fill: true` is rejected with `INVALID_QUERY` (message
  `time.fill requires query.time.grain`) when no `grain` is present.

## Validating your own IR

```python
import json, pathlib, jsonschema

schema = json.loads(pathlib.Path("schemas/query_ir.v1.json").read_text())
validator = jsonschema.Draft202012Validator(schema)
errors = list(validator.iter_errors(my_ir))
for e in errors:
    print(list(e.absolute_path), e.message)
```

The repo's regression suite runs the same loop over every committed
IR; see `tests/semantic_rails/test_query_ir_schema.py`.

## Result values

HTTP query responses, MCP `execute`, the Python `Runtime.query` SDK, and CLI JSON
output share one result-value policy. Segment previews use the same policy.
Python callers receive JSON-ready values, including strings for dates and times.
`column_types` maps each result field to its observed logical type and survives
all verbosity levels and MCP record/column row formats. It is separate from
`output_columns`, which describes semantic lineage and authored types.

| Source value | JSON value | `column_types` metadata |
|---|---|---|
| Decimal column | Canonical decimal strings, without redundant fractional zeros, for every non-null cell | `{"type":"decimal"}` |
| Integer column | Integer JSON numbers, including values larger than binary64's exact integer range | `{"type":"integer"}` |
| Float/double column | Finite JSON numbers | `{"type":"float"}` |
| Aware timestamp | ISO 8601 string, normalized to the query time zone; UTC with `+00:00` when no zone is available | `{"type":"timestamp","timezone":"aware"}` |
| Naive timestamp | ISO 8601 string with `T` and no offset; no zone is inferred | `{"type":"timestamp","timezone":"naive"}` |
| Date | `YYYY-MM-DD` | `{"type":"date"}` |
| Time | ISO 8601 string; aware times normalized to UTC with offset, naive times without offset | `{"type":"time","timezone":"aware"}` or `"naive"` |
| Interval (`timedelta`) | Signed ISO 8601 duration, e.g. `P1DT0H0M2.000003S`; exact microseconds, days/hours/minutes/seconds | `{"type":"interval"}` |
| SQL NULL | `null` | Does not replace a column's non-null type |
| Text / boolean | String / boolean, unchanged | `{"type":"string"}` / `{"type":"boolean"}` |
| Binary | Base64 string | `{"type":"binary","encoding":"base64"}` |
| UUID | Lowercase, hyphenated string | `{"type":"uuid"}` |
| JSON array / object | JSON-native structure | `{"type":"array"}` / `{"type":"object"}` |

A column's JSON type follows the driver's result type: DECIMAL/NUMERIC becomes
canonical strings; FLOAT/DOUBLE and INTEGER become JSON numbers. Authored types
never convert, re-encode or refuse a value. For mixed numeric driver values,
Decimal takes precedence over float, then integer, for the entire column.
An integer mixed with floats converts only if `float(n) == n`; otherwise the
column refuses with `RESULT_VALUE_UNSUPPORTED` rather than rounding the integer.
`Decimal("0.10")` becomes `"0.1"` and `Decimal("9007199254740993")` becomes
`"9007199254740993"`; a native integer `9007199254740993` remains a JSON number.
Consumers that use binary64 must read integer JSON tokens without first rounding
them to float. Numeric-looking text remains text with type `string`, even when
authored as an integer dimension.

Inside arrays and objects, native integers remain exact JSON integer tokens,
including `9007199254740993`; they do not pass through binary64 conversion.
Booleans remain booleans, and nested decimal/float values retain their normalization.

The Postgres ADBC adapter accepts only Arrow scalar types with exact mappings:
integers, decimals (including PostgreSQL NUMERIC stored as text and converted
to `Decimal`), float32/float64, text, booleans, date32, microsecond times,
variable-size binary, microsecond timestamps with or without a time zone,
month-day-nanosecond intervals, and NULL. Variable-size binary remains binary,
including 16-byte BYTEA values; UUID-looking text remains text.
Before converting each bounded batch to Python values, microsecond times outside
`00:00:00` through `23:59:59.999999` refuse with `RESULT_TYPE_UNSUPPORTED`.
This includes PostgreSQL `TIME '24:00:00'`, which cannot be represented as an
exact Python `time` and must never wrap to midnight. Errors expose no raw values.
Other Arrow types, including lists, structs, maps, nested NUMERIC, JSON/JSONB,
UUID, fixed-size binary and unknown extensions, refuse with
`RESULT_TYPE_UNSUPPORTED` before rows are read, even for empty or all-null results. The error names the column and
Arrow type in `details.column` and `details.type`, without exposing values.
Intervals still refuse with `RESULT_VALUE_UNSUPPORTED` when their duration
cannot be represented as an exact Python `timedelta`.

The same aggregate can have a different SQL result type per warehouse: `AVG`
is DOUBLE on DuckDB and NUMERIC on Postgres. `column_types` reports the driver's
result type. Cross-warehouse conformance and package tests compare numeric
columns numerically using this metadata, preserving the distinction from text.

The engine-derived time-bucket hint (`semantic_id` starting `temporal_role.`)
is the only column hint used for encoding: a driver's DATE becomes a naive
midnight timestamp, and ISO timestamp text is parsed with its complete seconds
fraction, including precision beyond Python's microseconds. Aware values
preserve the instant in the query's time zone, so month buckets retain their
own first day. Other date/time values follow their driver types; authored
temporal types do not parse text. Nonstandard fractional clocks and fractional
zone offsets in bucket text that cannot be retained refuse with
`RESULT_VALUE_UNSUPPORTED` before parsing.
Intervals represent the duration provided by the driver; calendar months/years
are not inferred from a `timedelta`.

Metadata is inferred from returned values, not the warehouse catalog: an
all-null column has type `null`, and an empty result has `column_types: {}`.
Nulls do not erase observed types. Unsupported values, non-finite numbers,
conflicting non-null column types (including mixed timestamp awareness), and
nested values requiring typed string metadata inside arrays/objects refuse with
`RESULT_VALUE_UNSUPPORTED`; raw values never appear in the error. This guard
also applies to injected adapters, preventing transport stringification from
silently changing a result's meaning.
