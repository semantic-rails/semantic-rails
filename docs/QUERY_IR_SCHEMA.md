# Query IR JSON Schemas

The canonical, machine-readable contract for the Query IR payload accepted
by `/api/v1/{validate,compile,query}` and the equivalent MCP tools
lives at [`schemas/query_ir.v1.json`](../schemas/query_ir.v1.json). That stable
schema accepts `version: 1` only. It is a JSON Schema Draft 2020-12
document and ships inside the Python wheel under `semantic_rails.contracts`.
Query IR `version: 2` is refused with `INVALID_QUERY` and
`details.supported_versions: [1]`. To move a version 2 query to version 1,
change only the version number: the two schemas had identical query shapes.
This is a breaking change within 0.x; planner outputs also use version 1.

The schema is regression-tested against every IR in the benchmark corpus
and the comparison fixtures: see
[`tests/semantic_rails/test_query_ir_schema.py`](../tests/semantic_rails/test_query_ir_schema.py).

## Top-level payload

| Field | Type | Notes |
|---|---|---|
| `version` | `integer` | Pin the IR schema version. Only `1` is supported. |
| `select` | `array` of `SelectItem` | Projected outputs. |
| `group_by` | `array` of dimension ids | Grouping keys. |
| `where` | `array` of `WhereFilter` | Dimension-level filters with shape `{field, op, value}` where `field` is a dimension id. See "WhereFilter" below for the op list and null semantics. Expression-shaped filters belong in `metric_filters`. |
| `metric_filters` | `array` of `MetricFilter` | Post-aggregation predicates. **There is no top-level `having` key.** |
| `order_by` | `array` of `OrderBy` | Final-select ordering. Uses `{field, direction}` — not a select-style expression. |
| `limit` | `integer` (or `null`) | Optional row cap. |
| `time` | `TimeBlock` (or `null`) | Query-level time anchor: temporal_role + grain + bounds. `start` is inclusive, `end` is exclusive. |
| `temporal_role_overrides` | `object<measure_id, temporal_role_id>` | Per-measure clock bindings. They do not apply inside a metric predicate input (they only choose or check its window clock); an input that reads an overridden measure through a conversion, a time window or a nested predicate refuses with `INVALID_TEMPORAL_BINDING` at every nesting depth, including predicates in scoped aggregates and aggregate filters. Filter values remain data. |
| `route_decisions` | `array` of `RouteDecision` | This query's own route for an entity pair: the `decision` of an `AMBIGUOUS_PATH` option. See [`route_decisions`](#route_decisions). |
| `observation_scope` | `"dataset"\|"query"` | Whether a sum or count with no rows in a group reads 0 when its measure has data anywhere (`dataset`, the default) or only inside the query's filters (`query`). See "Empty groups" below. |
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

Canonical query items also reject unknown keys with `INVALID_QUERY`, returning
the item `details.path`, `unsupported_keys`, `supported_keys`, and
`closest_matches`. The closed shapes are `select[]: {expression, as}`,
plain `where[]: {field, op, value}`, child groups: `{child, match, where}`,
and `metric_filters[]: {expression, op, value}`. Child-group conditions use the
same plain-filter shape. This applies to full queries and partial queries used
by the planner and builder; annotations such as `_note` are allowed only at
the top level. An `entity` key on a metric filter is refused with a hint to use
`expression.kind: "metric_predicate"` and put `entity` inside that expression.
The documented select shorthands below still normalize to canonical items.

### Per-entity value filters

In a `distribution` expression, `entity_value.where` filters the computed
value for each entity. Each item accepts only `op`, `value`, and an optional
`kind: "value_filter"`; omitting `op` defaults to `=`, and omitting `value`
defaults to null. An item with `field` or another unsupported key is refused
with `INVALID_QUERY`, which names its index. Put dimension filters in the
query's top-level `where` so they apply before the per-entity aggregation.

### Removed: `path_policy`

`path_policy` (`preference`, `ask_if_ambiguous`) is no longer a Query IR key,
in v1. This is a breaking change made within v1: before 1.0 the
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
  Only an `undecided` pair gets `details.meaning` and
  `details.route_alternatives`, using the package's own refusal options when they
  include the chosen route. Alternatives are at most three ready decision rows;
  each row's `label` is its meaning. Routes through hidden entities or
  relationships are excluded under the query's policy context, including from
  messages and counts. Unknown visibility under an `object_visibility` policy
  withholds alternatives. `details.more_alternatives` counts any remaining
  visible alternatives; validating the query without `route_decisions` returns
  every clarification option without a warehouse query. State the meaning used
  and offer the listed rows as one-step switches: resend the query with an
  alternative row in `route_decisions`. No alternative is executed. The warning
  mentions once that a reviewed package default using `details.row` would
  remove the question; this advice is not repeated in `recovery_hints`.
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
| `{ "dimension": "..." }` as the whole select item (no `as`, optional `kind: dimension\|group\|ref`) | that id added to `group_by[]`, whatever it already holds |
| `{ "expression": { "dimension": "..." } }` as the whole select item (no `as`, optional `kind: dimension\|group\|ref` inside `expression`), when `group_by` is empty or already lists it | that id on `group_by[]` |

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

**Summing windows require values that add up across periods.** `rolling`, `cumulative`,
and `period_to_date` accept additive flows using `sum` or `count`, event counts using
`count_distinct` of a complete single-column source-row key (the measure's row grain
or its model entity's full key). The entity key qualifies only when the measure reads
the entity's table and its row grain is absent or matches that key.
Sums, differences, or multiplication/division by numeric literals of those inputs
are also accepted.
A ratio (including arithmetic `divide` with a nonliteral denominator and metric recipes
that resolve to a ratio) computes the ratio of its windowed
parts: `SUM(numerator) OVER w / NULLIF(SUM(denominator) OVER w, 0)`. Each part uses the
same partition and frame, after the ordinary empty-group settlement; a zero denominator
returns `NULL`. It does not sum each period's ratio.

Inputs using `avg`, `min`, `max`, `median`, or `percentile`, stocks (semi-additive
measures), distinct populations, distinct counts of non-key columns or individual
components of composite keys, distributions,
products of measures, nested windows, and ratios inside other arithmetic or inside
another ratio refuse with `ROLLUP_UNSAFE`
before SQL executes. Ask for a ratio of windowed additive parts, or query the measure's
own aggregation without a summing window. This rule also applies through derived
metrics, metric filters, and every execution transport. `prior_period` reads one
period with `LAG` and keeps its existing input semantics.

These windows read periods before the ones they return, so a cut from below would drop rows
they need. With a `prior_period`, `rolling`, `period_to_date` or `cumulative` window in `select`
or `metric_filters`, a bounded `time.start` refuses, and so does a `where` filter, child groups
included, on any temporal or calendar dimension, whether or not it is the query's clock. A
dimension is temporal when it is a time role, has a `date`, `timestamp`, `datetime` or `time`
kind, or is on a column of the same name, compared without case, on any table, as a temporal
or calendar dimension or a column a relationship pairs with one, at any depth; a calendar
dimension is any dimension of a `kind: time` entity. The rule follows types and column names,
not the query's clock, so it may refuse a date that does not cut the window or a column of
the same name on an unrelated table. Only an upper bound (`<`, `<=`) on a
`date` or `timestamp` dimension runs, as `time.end` does. The refusal is
`WINDOWED_TIME_FILTER_UNSUPPORTED` or `CUMULATIVE_TIME_FILTER_UNSUPPORTED`; for a `where`
filter, `details.where_path` names it.

The same rule applies to dimension conditions bound to the window's own aggregate
inputs, including conditions authored inside metric recipes
(`details.filter_source: "measure"`). A separately filtered aggregate in another
select, in `metric_filters`, or beside the window in arithmetic does not cut that
window's input. A row policy applied to a scan in this statement that keeps a
single value of a temporal column also refuses a full-history window
(`details.filter_source: "policy"`, `details.policy_id`); unsupported row-policy scan
shapes retain `POLICY_DENIED`. Policies on unread tables do not trigger this guard.
Source refusal details include only `filter_source`, `policy_id` for a policy, and
the caller's expression (plus window lookback when applicable). They omit authored
measure IDs, conditions and values. Object authorization runs before a source
refusal is returned: denied callers receive `POLICY_DENIED`, with policy details
omitted when they would name hidden blocked objects.
A policy restricts the caller's readable rows, and an authored filter restricts the
measure's population. Neither promises complete lookback history. The engine refuses
these combinations rather than widening the readable population or reporting a
truncated window. Non-temporal filters and aggregate date/timestamp upper bounds
keep their existing behavior. These refusals offer no patch to remove a policy or
an authored filter; query an unwindowed measure or ask the package author for a
supported metric.

A separately stored month or date must declare a temporal kind or another structural
link described above. A categorical label on a different column, with no declared
relationship to a temporal column, carries no temporal semantics: the engine cannot
infer that filtering it removes lookback history. Declare a physical date as
`kind: date`; categorical period labels need a package contract before they can be
used safely to bound a full-history window.

`period_to_date` currently supports only the default calendar. A non-default
`time.calendar_id`, or a time role bound to a non-default calendar, refuses with
`REWRITE_NOT_SUPPORTED`; it cannot silently reset on Gregorian periods. Query the
authored calendar's period as exact start/end dates without `period_to_date` instead.
Default-calendar resets are unchanged.

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
For either entity-only window alignment, an incompatible query clock requires
exactly one compatible input clock. Multiple candidates refuse with
`INVALID_TEMPORAL_BINDING`, listing the clocks. A measure's `default_temporal_role`
does not choose among them. For a direct measure input, pin its `temporal_role` or
set `temporal_role_overrides` for the measure. For a metric input advertising several
`compatible_temporal_roles`, pins and measure overrides inside it do not narrow
those clocks; set `query.time.temporal_role` to one of the listed clocks. For either
input, omit `time_alignment` to apply the predicate over all time. A compatible
query clock is retained. A model's default time supplies the clock only when the
measure does not declare its own `times` list.
A window-aligned metric predicate refuses with `INVALID_TEMPORAL_BINDING` if the
chosen window clock is excluded by any measure inside the input, by its pin, an
override or its declared clocks; for a conversion, its base measure (the period
filters base events; converted events match each base event's window).

For a metric selected as an output, its expression's pinned clock takes precedence
over its advertised compatible clocks. If the query would filter or bucket a leaf
on another clock that the measure advertises, the request refuses with
`INVALID_TEMPORAL_BINDING` and a `CHOOSE_OUTPUT_CLOCK` hint to query the bound clock.
This check also applies to composed outputs and ordinary metric filters. Unpinned
metrics keep the requested compatible clock; a pinned metric queried on its bound
clock keeps its answer. Nested scoped predicates also refuse an ambiguous window
clock rather than choosing one by declaration order.

Ordinary `metric_filters` evaluate aggregated expressions at the grain the query
returns, after grouping. A `metric_predicate` instead evaluates its input at its
declared entity within that scope. A contextual predicate inherits the query's
time and grouped context. When a grouped dimension belongs to the input's own
row entity, the predicate groups by that dimension's values, including NULL,
rather than by each row's entity key. A `where` filter is inherited before this
aggregation; `entity_only` omits grouped context and compatible `where` filters.

Comparison and other post-aggregation `metric_filters` beside a `distribution`
refuse with `REWRITE_NOT_SUPPORTED`: branch lowering cannot apply them once at
the returned group's grain. This includes distributions reached through derived
metrics. Run the group-level filter without the distribution first. A contextual
`metric_predicate` on an entity different from the distribution's per-entity
grain refuses with `PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE`; use `entity_only` or
a `where` filter. This refusal also covers predicates inside the distribution's
input, including scoped aggregates and inputs reached through metric recipes.
A distribution nested inside another expression also refuses
with `REWRITE_NOT_SUPPORTED`; select the distribution separately.

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

On a stock, a filter on an attribute reads each series' snapshot chosen for the period, as
a `group_by` on it does, so `plan = basic` equals the basic row of the by-plan breakdown;
this includes attributes reached through a key that changes between the series'
snapshots. A filter on the stock's clock (the same entity and column) or a calendar
(`kind: time`) dimension bounds time and applies before the choice. Other date or
timestamp attributes refuse, as in `group_by`: `REWRITE_NOT_SUPPORTED`, with
`details.reason: stock_filtered_by_date_attribute` and `details.dimension`. Conditions
inside child groups and a measure's own filters follow the same rule
(see [Measures](PACKAGE_AUTHORING.md#measures)). An entity-set share (a ratio of one stock or
distinct count whose numerator alone adds metric predicates) keeps one snapshot per series
per time bucket, so a `group_by` on the stock's clock, a calendar dimension or another date
or timestamp dimension refuses: `REWRITE_NOT_SUPPORTED`, with
`details.reason: entity_set_ratio_grouped_by_period` and `details.dimension`. Choose the
period with `time.grain` instead.

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

With `limit`, the engine preserves these sort terms and appends every remaining
output column in output order, ascending with NULLs last. Identical output rows
are interchangeable. Ordering without `limit` is unchanged.

Execution fetches at most `limit + 1` rows in the same warehouse statement and
returns only `limit` rows. If the boundary row shares all requested sort keys
with the last returned row, `TIES_AT_LIMIT` reports `details.tie_count`, the
number of observed rows sharing that key, and `tie_count_is_lower_bound: true`.
The full tie group may be larger than this bounded sample. Compile SQL retains
the requested limit; the internal execution probe uses one extra row. A
`limits.max_rows` fence at or below `limit` takes precedence: no extra row is
fetched and cutoff ties cannot be reported.

Cutoff comparisons use the returned row's exact column key when present;
otherwise they require one case-insensitive match, supporting warehouse alias
case folding. Missing or ambiguous matches fail with `QUERY_EXECUTION_ERROR`.

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

`range.last` selects the last complete periods before **today in the temporal
role's local zone**. An offset-bearing `policy_context.now` is an instant: it
is converted to that zone before taking its date. Equivalent UTC and offset
spellings produce the same bounds. Without `now`, the current instant is used;
an ISO date or a datetime without an offset is read as local wall time.
For a non-default calendar, `range.last` supports only `unit: "day"`.
Coarser units are refused with `INVALID_QUERY`; supply exact `start` and `end`
dates for that calendar's periods.

A clock declared as `kind: date` applies the calendar spine's whole-day rule
to its source rows too: `start` includes its local day, and an exclusive `end`
after midnight includes its local day. An end at exact midnight excludes that
day. Offset-bearing bounds use the role's zone. An empty or reversed interval
includes no days. On DuckDB, MotherDuck, DuckLake and Postgres, a DATE clock
enters `column_timezone` conversion as a naive midnight timestamp in that
storage zone, independent of the session zone; these are the warehouses whose
converted DATE clocks are tested. Other warehouses pass the DATE to their own
conversion function unchanged. The comparison then uses the converted local
date, including an anchored population or snapshot on a different query time
axis. Timestamp clocks retain precise half-open bounds.

A role requiring timezone conversion (`column_timezone` set and different from
`timezone`) is converted before its bounds and buckets. Shapes that would read
its stored values instead are refused with `WINDOWED_TIME_FILTER_UNSUPPORTED`,
and `details.path` names the shape:

- `entity_only_predicate_window`: an entity-only metric predicate aligned to the
  query window;
- `predicate_period_join`: a contextual metric predicate in a query with `time`,
  which joins on the query's time period;
- `conversion_metric`: a conversion metric whose query time role converts.

Use an unconverted role for those queries.

## PolicyContext

```jsonc
{
  "environment": "production",
  "audience": "internal",
  "roles": ["sales", "csm"],
  "now": "2026-05-21T00:00:00Z"   // anchors relative time ranges
}
```

The default `HeaderPolicyContextResolver` lets callers self-assert roles
in headers or body `policy_context` — operators should swap in an
identity-derived resolver for production.

A request naming an environment the package does not declare is refused with
`INVALID_QUERY`, and `details.allowed_environments` lists the declared environments.

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
| a stock that adds up its series (`last_value`, `first_value` or `sum`) grouped only by time | `0` in a period with snapshots, none passing the filters | `NULL` in a period with no snapshot |
| other semi-additive measures (stocks), distinct populations, and measures with `additive: false` | `NULL` | `NULL` |

A measure has data in scope when at least one of its rows in scope holds a value: a sum with a
non-NULL amount, or a count above zero. The scope is the measure's own authored conditions
(its `filter`, an `aggregate_if` condition or a `CASE`) and policy row filters, before the
`group_by`, and the query's `observation_scope` says whether its `where` filters count:

- **`dataset`** (the default, or the package's `defaults.observation_scope`): they don't. When
  the query has a `where` filter, the guard reads the measure's first row under its authored
  conditions and row filters, on every leaf, route and warehouse. If
  store 5 sold no apples, "apples at store 5" reads `0`, as store 5 does in a `group_by:
  store` answer. A string `=` or `IN` `where` value that matches no row of its dimension
  (under the caller's row filters) adds one `FILTER_VALUE_NOT_FOUND` warning naming each such
  value and the closest one, so a misspelled `product = 'appels'` isn't read as a confident 0.
  If an existence probe fails or cannot group the dimension, `FILTER_VALUE_UNVERIFIED` names
  the dimension and literals that could not be verified; a failed suggestion read only omits
  the suggestion.
- **`query`**: they do. A measure with no value inside the query's filters reads `NULL` in
  every group with `NO_DATA_IN_SCOPE`: "apples at store 5" reads `NULL`, and a misspelled
  filter value reads `NULL`, not a confident 0.

Either way a metric predicate, the query's or a measure's own, selects the population that is
measured, and a time window is judged as below: plain time leaves check for data outside the
query's time bounds (DuckDB and Postgres; see time coverage below). A `dataset` query with a
`where` filter beside a metric predicate can't judge one apart from the other, so it is refused
with `EMPTY_GROUPS_UNSETTLED`; send `observation_scope: "query"`.
Where a measure has data in scope,
a group with no rows reads `0`: a store with orders but no refunds has 0 refunds. A conditional
additive sum or count with a supported source probe also reads `0` when its condition never
matches but its source relation has rows in scope, for an output whose time bucket is checked
against the loaded range or that has no time bucket (see time coverage below). No source rows
means `NULL` with `NO_DATA_IN_SCOPE`; matching rows whose amounts are all NULL still sum to `NULL`.
An average, minimum or maximum of nothing is
undefined, and a stock has no value for a period nobody observed, so neither is ever made zero.
A stock that adds up its series is judged by period instead, where it chooses each series'
snapshot: a period whose chosen snapshots all fail the `where` filters or the measure's own
`filter` reads `0` ("paying workspaces on the enterprise plan today", with today's snapshots
loaded and none on enterprise), and a period with no snapshot reads `NULL`. A metric
predicate chooses the series measured, so a period where it keeps none reads `NULL`, as does
one under `observation_scope: "query"` with no snapshot passing the filters.

A group whose rows exist but whose amounts are all NULL is not empty: its amounts are unknown,
so its sum reads `NULL`, as SQL's `SUM` does, while its count still counts the rows. A month
whose orders all have a NULL amount has a revenue of `NULL` and an order count above 0; a
month that mixes NULL and known amounts sums the known ones. A conditional sum
(`aggregate_if`, or an aggregate with a `filter`) reads only the rows that meet its condition: a group
whose rows all fail it has none and reads `0`, and one whose matching rows all have a NULL
amount reads `NULL`. Filled or not, a group reads the same.
For a plain additive time series with an authored dimension filter, on a warehouse that
checks loaded coverage (DuckDB and Postgres), the source rows retain every observed bucket
before that filter: a week with rows but no matches reads `0` only inside the loaded range,
with or without `fill`, in both observation scopes. A bucket after the last loaded timestamp,
such as one held only by a future-dated row, stays `NULL` under the coverage rules below, so
filled and unfilled output agree. String `=` and `IN` literals in its aggregate
filter use the same misspelling guard in both scopes: `FILTER_VALUE_NOT_FOUND` names values
absent from their dimension, and `FILTER_VALUE_UNVERIFIED` names values whose existence
cannot be checked under the caller's policies. Query `where` filters and policy row filters
still restrict those source rows. A period with no source rows stays absent without `fill`;
no calendar is generated. This applies to sums and counts on a local clock with no rewrite,
using local dimensions or single-hop lookups, with only additive outputs, and preserves matching NULL amounts.
Other shapes retain their existing lowering: warehouses without loaded coverage, rollups,
fanout and parent-lookup rewrites, nonlocal clocks, predicate populations, non-additive
sibling outputs, distribution branches, and conditional operands.
For an additive filtered series on those paths, `FILTERED_SERIES_BUCKETS_DROPPED` names
missing observed bucket/group keys found by a separately authorized source query. If that
query is denied, fails, reaches its 1,001-row cap, or the answer has a limit or population
filter, `FILTERED_SERIES_BUCKETS_UNVERIFIED` reports the reason without guessing the buckets.
Non-additive metrics, including averages and ratios, keep their existing rows and NULL behavior.
A window of a sum or difference windows each operand first, so an unknown goods amount
drops only the goods, not that month's revenue.
A summing window refuses a metric referenced inside its input, such as `net * 2` where `net`
is a metric, with `ROLLUP_UNSAFE` (`details.unsupported_construct: nested_metric_window_input`);
write that metric's expression inline instead, or make the metric the window's whole input.
A sum of a `CASE` with no `ELSE` or `ELSE NULL` follows that conditional rule, with one
branch or several: a group none of whose rows meets a branch reads `0`, and such a sum is
never answered from a rollup. An explicit non-NULL `ELSE`, including `ELSE 0`, contributes
on nonmatching rows, so every row is read: a matching NULL amount plus a nonmatching zero
sums to `0`, while a group with only matching NULL amounts remains `NULL`.
Explicit `ELSE` contributions are preserved inside distributions and metric predicates too.

A measure with a `CASE` below its expression's top level, such as
`CASE WHEN store_id = 'a' THEN amount END / 100.0`, keeps the earlier settlement
on the base table and never reads a rollup. Its sum is `0` for a no-match group
when its measure has a known amount elsewhere in scope. Under this fallback,
a matched-unknown group also reads `0` when another group has a known amount;
if no amount is known anywhere in scope, it stays `NULL`. Other measures in the
query keep their own settlement rule. Under `dataset`, a query with a `where` filter over such
a measure is refused with `EMPTY_GROUPS_UNSETTLED`, since its unknown amounts would read `0`;
send `observation_scope: "query"`.

A query with a `distribution` output keeps the earlier settlement in every output, which reads
a group's unknown amounts like no rows: there a sum is `0` in a group whose amounts are all
NULL, wherever its measure has data in scope, and arithmetic settles each operand that way, so
`goods + shipping` beside a median is `0` for a store with no refunds and a number for one
whose refunds leave a column NULL. Its combined outputs have no probe of their own, so under
`dataset` such a query with a `where` filter is refused the same way. So is one whose measure's authored
condition reads a fan-out or a hop valid over time.
A metric predicate's own per-entity values, a lookup's source and a distribution's branches
are internal: they settle inside their own scope in both modes.

- **Arithmetic** settles each operand first, then combines them. An operand that is unknown
  or has no data in scope is `NULL`, and so is the result: `goods + shipping` by refund type
  is `NULL` for a type whose rows leave one of the columns NULL, and `revenue - refunds` is
  `NULL` if the refunds relation has no rows in scope. Where source rows settle it (below), a
  never-matched conditional operand instead reads `0` before arithmetic, so a window total's
  `3 - 0` reads `3` without `NO_DATA_IN_SCOPE`. A ratio over an unknown numerator is `NULL`, which no
  `metric_filters` threshold keeps. Division by zero is `NULL`.
- **A `metric_predicate` applies the rule to every entity alike.** An operand reads `0` for an
  entity with no match where its measure has data somewhere in the predicate's scope, and
  `NULL` where it has none, whether that entity has rows or none at all. So
  `orders - returned_orders > 1` keeps a customer with 2 orders and no returns, as a
  `metric_filter` on the same expression does, and `large_orders = 0` ("customers with no
  large orders") keeps every customer without one when some order in scope is large, and
  keeps nobody when none is: with no large order anywhere in scope there is no data, not a
  count of zero. `NULL` fails every threshold, `= 0` and `< 1` included, so an entity whose
  rows all have a NULL amount meets none of them. Only a count or sum threshold that 0 passes
  reaches an entity with no rows at all, and a distinct count of a population is 0 for one
  whether or not the scope has data. Such a threshold on an add or subtract of measures
  still reads an operand's unknown amounts as `0` where its measure has data in scope: an
  entity with rows can be `NULL` because one operand is unknown, which can't show whether the
  other measures have data, so `goods + shipping = 0` keeps the orders with no refunds even
  where every refunded order has goods or shipping amounts but never both. In a query with a
  `distribution` output, every predicate reads unknown amounts that way. A measure with a
  nested `CASE` keeps its earlier settlement inside a predicate too.
- **Time coverage bounds zero filling.** Bounded plain time leaves check for observation
  outside the query's window under the same authored, query and policy row filters. For
  fill, dense series, combined leaves and retained filtered series (filled or not), an empty
  bucket inside the base relation's loaded range reads `0`; an empty bucket before its first
  loaded timestamp or after its last reads `NULL`. Coverage uses the whole base relation under policy filters, ignoring
  measure and query filters, and excludes future timestamps from its upper edge. The
  cutoff compares UTC instants: timezone-aware columns preserve their instant, and
  naive columns use their declared storage zone (`column_timezone`, then `timezone`,
  defaulting to UTC). That cutoff is the only instant comparison: buckets, calendar joins
  and the window keep each leaf's own time frame, and the loaded range is the lowest and
  highest of the leaf's own bucket. Coverage gates only
  zero substitution: populated sums and positive counts always survive, including
  NULL time keys and future-dated rows.
  A window total without a grain records the whole half-open `[start, end)` interval as one
  bucket, with the same outside-window observation and loaded-range check. Conditional
  sums and counts of a leaf that reads its clock from its own relation therefore read `0` in
  a loaded window where they have no matches, whether or not their condition matched
  elsewhere: the probe checks for a source row under the same scope filters and policy row
  filters. A sum whose matching amounts are all NULL remains `NULL`. An empty source relation
  or a window outside its loaded range remains `NULL` with `NO_DATA_IN_SCOPE`; under `dataset`
  with a `where` filter, the warning follows the probe read described below, so a window
  outside the loaded range reads `NULL` without it once the probe finds source rows. Relative
  windows use their resolved bounds.
  Window totals, filled, dense-series (rolling, prior-period) and combined plans, bounded or
  not, read the base relation even when rollups are available, so routing cannot change their
  coverage answers. Other routed
  aggregates, nested, fanout and predicate sources retain the window observation test,
  except that a `dataset` query with a `where` filter probes each
  measure's rows untimed.
  Source-row observation is supported for top-level conditional `CASE` operands (including
  `aggregate_if`) in these bounded base leaves and in `dataset` probes. It settles a
  never-matched operand to `0` only for an output whose bucket the same guard checks against
  the loaded range, or for an output with no time bucket (one total over its scope). The
  loaded-range check covers window totals, filled and dense series, combined leaves and
  retained filtered series, each for a leaf that reads its clock from its own relation. Every
  other output keeps value-based observation, so a never-matched operand reads `NULL` with
  `NO_DATA_IN_SCOPE`:
  - a grained series of a single leaf without a fill, dense series or retained filter, bounded
    or not: a month or week of a window, including a future month held only by a placeholder
    row;
  - a leaf that reads its clock through a join, such as refunds on the order time, even in a
    window total or a filled series;
  - the grained buckets of a `dataset` query with a `where` filter that get no loaded-range
    check, since its probe reads the measure's rows outside the window as well.

  A condition lowered into a leaf's `WHERE` still restricts its probe. Paths without a source
  probe, including predicate operands, lookup sources and distribution branches, and the
  earlier settlement retain their existing value-based observation: a never-matched operand
  stays `NULL`.
  Coverage uses data alone. Performance guidance includes the emitted observation and
  coverage reads as scans without request-window bounds; narrowing the requested window
  does not bound those reads.
  Coverage and the outside-window check run only on DuckDB (with MotherDuck and DuckLake)
  and Postgres, whose execution is tested. On Snowflake, BigQuery, Databricks, Athena and
  ClickHouse an empty bucket reads `0` only while the measure has data inside the window
  (a `dataset` query with a `where` filter probes untimed there too), and rollups route as
  they would without coverage.
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
ratio or rolling output never gets it: it can be `NULL` while its measure has data. A clipped
result (`truncated`) never gets it. Under `dataset`, an empty answer to a query with a `where`
filter never gets it either: its filters kept no row, which says nothing of the measure's
data elsewhere. For a non-empty all-`NULL` output under `dataset`, a bounded read of the
settlement's observation probes checks whether its measures have data elsewhere; unknown
amounts alone do not trigger the warning. Under `query`, no extra read is needed.

ClickHouse fills an unmatched outer-join field with a type default (0 or an empty string)
unless the join yields NULLs, so every ClickHouse statement ends with
`SETTINGS join_use_nulls = 1`.

## What an answer covers

Some answers are right but easy to misread, so the response says what they cover. This never
changes the SQL or the rows.

**Facts on different clocks.** With no `time` block, selects that read measures of different
entities or governed metrics with differing sets of real time roles, mixing at least two
distinct roles, carry one `MIXED_TIME_ROLES` warning that names each measure's role: orders by
order time and storefront
sessions by session start, grouped by customer, each read a period on their own role's clock.
Measure-level filters can bound those periods, even without a `time` block; the warning makes
no claim about how much history is covered. Undated measures are ignored. A `time` block,
including a role without bounds or a grain, suppresses this warning. `details.clocks` lists each
dated measure's `subject` and `temporal_roles`.
A dated measure inside an expression (`ratio`, arithmetic, `case`, `aggregate_if`) counts like
a bare one; one inside a conversion or a metric predicate keeps that expression's own time rules. A
metric counts as one clock, with every role it combines: alone it never warns, since the
package defined it, and beside a dated measure or metric with a different role set it does.
Measures that share a role, and bare measures of one entity, never warn. A governed metric
is a distinct source even when its measures belong to that same entity.

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
| `sum` / `count` / `count_distinct` over an additive, event-count or entity-count measure | `0`, while the measure has data in scope; else `NULL` | Zero is the additive identity — summing no rows really is 0 — but only where the measure has data (see "Empty groups"). A bucket whose rows all have a NULL amount is not empty: its sum reads `NULL`, filled or not. |
| `avg`, `min`, `max`, `median`, `percentile` | `NULL` | Undefined over no rows. A filled `0` would be a fabricated measurement — a `min` below every value actually observed. |
| semi-additive measures (snapshots, period-to-date, rolling balances) | `NULL` | A snapshot for a period that was never observed is unknown, not empty. A period with snapshots that all fail the filters isn't filled: a stock that adds up its series reads `0` there (see "Empty groups"). |
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

The base DuckDB installation includes support for fetching raw `TIMESTAMPTZ`
values in dimension groups, ungrained time roles, and Architect column profiles.

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
