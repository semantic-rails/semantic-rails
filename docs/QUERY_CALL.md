# The query call

A query call asks one business question of a package: what to measure, how to split it,
which rows and entities count, and over which time. The package holds the definitions; the
call only names them. The same call goes to every surface (MCP `execute`, the HTTP API, the
Python `Runtime`, the CLI and package examples and tests), and the answer says back what it
used.

The machine-readable contract is [`schemas/query.v2.json`](../schemas/query.v2.json)
(JSON Schema Draft 2020-12, `$id` `https://semantic-rails.com/schemas/query.v2.json`). It is
generated from the parser's own tables, so a call the schema accepts is a call the engine reads;
the MCP `execute` tool embeds it. Rules a schema cannot say (one window spelling, `fill` only
on an open window, `over` beside `having`) are parser rules with named refusals, described
below.

Every example on this page runs on the bundled `jaffle_shop` package, sent with
`now = "2017-09-04T12:00:00Z"` (a Monday; jaffle's orders run from 2016-09-01 through
2017-08-31). The outputs are real, at `verbosity: "minimal"` (the MCP default); they show the
rows and the answer fields each section discusses, and leave informational warnings out. Every
`measure.jaffle.order_count` answer also states `GOVERNED_METRIC_EXISTS` naming the two metrics
that filter that measure ([The `aggregation` override](#the-aggregation-override)); where a
section shows `assumptions`, it shows them all.

## The call at a glance

Ten fields, all optional, no other keys:

| Field | Shape | Meaning | When omitted |
|---|---|---|---|
| [`select`](#select) | list of ids or `{expression, as}` | what to measure | a list of `group_by` rows |
| [`group_by`](#group_by) | list of dimension ids | how to split | one row |
| [`where`](#where) | list of conditions and entity filters | which rows and entities count | every row |
| [`having`](#having) | list of conditions on results | which result rows to keep | every result row |
| [`time`](#time) | `{temporal_role, grain, last \| this \| start/end, fill}` | the clock, the window, the grain | all time, one total |
| [`order_by`](#order_by) | list of `{field, direction}` | ordering | engine order, NULLs last |
| [`limit`](#limit) | integer ≥ 1 | the question's top N | every row |
| [`routes`](#routes) | list of `{from, to, via}` | the join route this question means | the package's routes |
| [`observation_scope`](#observation_scope) | `dataset` \| `query` | which rows count as "has data" | the package default |
| [`totals`](#totals) | list of `group_by` ids, outermost first | subtotal and grand-total rows | no total rows |

A call needs at least one of `select`, `group_by` or `time`. `version` is ignored, whatever
its value. Keys starting with `_` (such as `_note`) are annotations and are ignored. Any other
key is refused with its path and the key it most likely meant (`filters` → `where`, `sort` →
`order_by`, `measures` → `select`); a retired key is refused with where it went (see
[Retired keys](#retired-keys)).

```json
{
  "select": ["measure.jaffle.revenue_usd"],
  "group_by": ["dimension.jaffle_store_name"],
  "where": [{"field": "dimension.jaffle_store_name", "op": "not", "value": "Brooklyn"}]
}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Philadelphia", "revenue_usd": 486468.1799998507}],
 "clock": "temporal_role.jaffle_order_time", "observation_scope": "dataset"}
```

## Sending a call

The call is only the question. Who is asking, when "now" is, and how to render the answer are
arguments beside it:

| Surface | The call | Beside it |
|---|---|---|
| MCP `execute` | `query` | `now`, `mode` (`run` \| `validate` \| `sql`), `max_rows`, `row_format`, `verbosity`, `sql_profile` |
| MCP `plan` | `query` (a partial call to start from) | `intent`, `now`, `detail`, `limit` |
| HTTP `POST /api/v1/{validate,compile,query}` | `query` | `options: {now, verbosity, sql_profile}`; the `X-Request-Id` header |
| Python | `Runtime.query(call, ...)`, `.validate`, `.compile` | `now=`, `context=`, `verbosity=`, `sql_profile=`, `limits=`, `request_id=` |
| CLI | `semantic-rails query --query-json '<call>'` | `--now`, `--environment`, `--audience`, `--verbosity`, `--sql-profile` |
| Package examples and tests | `query:` | `now:` (per entry or at the file's top level), `context:` |

An HTTP body is `{"query": <call>, "options": {...}}`, or the call itself; any other key
beside `query` and `options` is refused. A tool argument sent inside the call (`"now"`,
`"max_rows"`, `"verbosity"`, ...) is refused with "pass it beside `query`", and MCP refuses a
key of the call sent beside `query` (`"limit"`, ...) with the hint to move it inside. MCP also
reads a `query` sent as a JSON string.

MCP `row_format` shapes the rows: `records` (objects, the default) or `columns` (`columns` once,
each row an array). When a call sends no `row_format` and its rows as records exceed the MCP
response budget but fit as columns, `execute` returns columns with the note
`ROW_FORMAT_COLUMNS`, from the one warehouse run, so read `row_format` before `rows`
([MCP interface](MCP_INTERFACE.md#query-verbosity-tiers-execute-modes)).

```bash
uv run semantic-rails query --package jaffle_shop --now 2017-09-04T12:00:00Z \
  --query-json '{"select": ["measure.jaffle.order_count"], "time": {"last": {"unit": "week", "value": 1}}}'
```

### `now`

`now` is the instant relative windows (`last`, `this`) resolve against: an ISO date (its first
instant, UTC) or an ISO date-time (without an offset, UTC). It defaults to the server clock.
The window resolves in the clock's own time zone, and the answer echoes the instant as
`window.now`. Pin it whenever a result must be reproducible: a scheduled report, a package test,
a reference answer. A package example or test that sets its own `now:` keeps it; a file-level
`now:` applies to every entry of that file that does not set one; `--now` on `run-examples`,
`test-package` and `check` covers the rest.

```yaml
now: "2017-09-04T12:00:00Z"   # every test in this file
tests:
  last_week_orders:
    kind: query_matches_snapshot
    query:
      select: [measure.jaffle.order_count]
      time: {last: {unit: week, value: 1}}
    expected_rows:
      - order_count: 1470
```

### Identity

Identity (environment, audience, roles) comes from credentials, never from the call. Over
HTTP and the remote MCP endpoint, a `policy_context` anywhere in the request (beside `query`, in
it, or nested deeper) is refused with `IDENTITY_CLAIM_REFUSED`; it is never stripped
silently. A trusted local caller states it
beside the call: `context=` in Python, `--environment`/`--audience` on the CLI, `context:` in a
package example or test.

## `select`

Each item is a measure or metric id, or `{"expression": <id or inline kind>, "as": <alias>}`:

```json
{
  "select": [
    "measure.jaffle.order_count",
    {"expression": "metric.sales.aov_usd", "as": "aov"},
    {"expression": {"kind": "measure", "measure": "measure.jaffle.revenue_usd", "aggregation": "median"}, "as": "median_order"}
  ],
  "group_by": ["dimension.jaffle_store_name"]
}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "order_count": 21465, "aov": 12.085946890286502, "median_order": 6.24},
          {"dimension.jaffle_store_name": "Philadelphia", "order_count": 38187, "aov": 12.739104407255105, "median_order": 6.36}]}
```

- A reference is the id string itself. `{"metric": ...}`, `{"measure": ...}` and
  `{"kind": "metric", ...}` objects are refused with the string to write.
- A measure uses its package aggregation. `{"kind": "measure", "measure", "aggregation"}`
  overrides it (see [The `aggregation` override](#the-aggregation-override)).
- The column is the id's last segment (`order_count`), or `as`. An inline kind without `as`
  gets the engine's name (`expr_<n>`, its position in `select`), so give every inline kind an
  `as`. A name used twice is refused with `DUPLICATE_OUTPUT_ALIAS` at the repeating item's
  path, with the `as` that makes it unique.
- An `as` that is a governed metric's or measure's id, name, column or authored synonym
  (`revenue_usd`, `measure.jaffle.revenue_usd`), in any case (`Revenue_USD`), must name that
  object. Another calculation under it (an order
  count, the `max` of the revenue measure, a filtered revenue) is refused with
  `OUTPUT_NAME_GOVERNED`, naming the governed object and a name to use instead, so its numbers
  never read as the governed one's. The object itself answers under its name however it is
  written: by id, as its default aggregate, or rounded with `ROUND`.
- Each `dimension.` id in `select` is moved to `group_by` when the call's `group_by` is empty
  or already lists it (one `QUERY_SHORTHAND_NORMALIZED` note lists every id moved); beside a
  `group_by` that lists other dimensions it is refused.
- Without `select`, a call with `group_by` lists the distinct rows:

```json
{"group_by": ["dimension.jaffle_store_name", "dimension.jaffle_store_tax_rate"],
 "order_by": [{"field": "dimension.jaffle_store_name", "direction": "asc"}]}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "dimension.jaffle_store_tax_rate": 0.04},
          {"dimension.jaffle_store_name": "Chicago", "dimension.jaffle_store_tax_rate": 0.0625},
          {"dimension.jaffle_store_name": "New Orleans", "dimension.jaffle_store_tax_rate": 0.04},
          {"dimension.jaffle_store_name": "Philadelphia", "dimension.jaffle_store_tax_rate": 0.06},
          {"dimension.jaffle_store_name": "San Francisco", "dimension.jaffle_store_tax_rate": 0.075}]}
```

## `group_by`

Dimension ids. Each becomes a column named by its full id, so a reader keys on it without
guessing. Time is never a `group_by` item: ask for `time.grain` and the answer has a `period`
column. Groups with no rows stay absent: the engine never cross-joins dimension values. A
policy's `required_group_by` on the clock's own date dimension is met by any `time.grain` at or
below that dimension's grain. A call that meets no required cut is refused with `POLICY_DENIED`
naming it ("measure.x can only be read by day: group by dimension.d, or set time.grain to
day."), and the one hint's `patch.merge` is the call fragment that meets it
(`{"time": {"grain": "day"}}`), built from the policy's own cuts.

A dimension the package declares as an entity's `display` (the name of one row, see
[PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md)) groups by the entity's key too: the key column
comes just before the name, so two customers named Ann stay two rows, and the answer's
`assumptions` carries `GROUPED_BY_KEY {dimension, key, message}`. A call grouped by an entity's
key, when the entity declares no `display` and the call groups by none of its other dimensions,
carries `SHOWN_BY_KEY {entity, dimension, message}`: its rows show only the key.

### Groups across a one-to-many hop

A dimension of a child entity (an order's items or tags, a department's courses) holds several
values per row of the measure's entity. Grouped by one, each row of the measure's entity counts
once, with its whole value, in every group it has a child row in: departments by course level
counts a department under each level it teaches, and order revenue by item product type is the
revenue of the orders that included each type.

```json
{"select": ["measure.jaffle.revenue_usd"], "group_by": ["dimension.jaffle_product_type"]}
```

An order holding a beverage and a jaffle counts its whole total under both, so the two groups
add up to more than all revenue (691,101.53 and 485,267.73 of 745,893.03). Item revenue
(`measure.jaffle.item_revenue_usd`) splits the totals over the items instead: the call says
which measure it reads.

- A sum, count, average or distinct count of the entity's own measure is over one row per
  (entity key, group). An average is over the rows in each group, and a ratio of two measures of
  the entity divides each group's own totals.
- The hops must be one-to-many from the measure's entity, each joined on the declared key of its
  one side, before any lookup: orders to their items, then each item's product, is answered;
  orders to their customer, then the customer's sessions, is many-to-many and refused.
- A row with no child row is in no group, and a group no row reaches is absent; a child row
  with no value is the NULL group.
- The groups can overlap, so their sum can exceed the total. The answer's `REWRITE_APPLIED`
  (`fanout_dedup`) says so and names the hop: `details.overlapping_groups` holds the `group_by`
  ids and the one-to-many `relationships`, and `whole_amounts: true` marks a sum or average of a
  value, which `plan` repeats in its `warnings` when it drafts one.
- A [total](#coarser-totals) or [`totals`](#totals) row is the call without the grouping, so it
  counts each row once (all revenue above). A share over such a grouping is refused with
  `MIXED_GRAIN_INVALID`: its total would add up the overlapping groups. A share over a column of
  the entity's own within each group (each coupon's share within a product type) stays.
- Still refused with `MIXED_GRAIN_INVALID`, with the message they had: a median, minimum,
  maximum or other statistic; a non-additive measure, a stock, or a measure whose rows are finer
  than its entity's key; a window, per-entity value, distribution or metric predicate over it; an
  item that combines it with a measure of another entity (order revenue per item revenue); and a
  second grouping or filter across a one-to-many hop beside it.
- A relationship authored `allowed_directions: [forward]` is never walked from its one side, so
  such a grouping is refused with `PATH_NOT_FOUND` naming it.

### Groups across a link

A [link](PACKAGE_AUTHORING.md#one-row-per-key) reaches the columns that can hold several values
per key: a person's departments, read from the rows of the person's own relation. Grouped by
one, each entity counts once in every group it has a row in.

- A measure of the person, or of a fact that looks the person up (people in rooms, rooms in
  departments), is a sum, count, average or distinct count over one row per (entity, group):
  an average is over the entities in the group, a distinct count over their values, and a
  ratio divides each group's own totals. A row with no value there, or that finds no row, is
  the NULL group (ClickHouse joins a link as it joins lookups, inner, and drops a row that finds
  none).
- The groups can overlap, so their sum can exceed the total; the answer's `REWRITE_APPLIED`
  says so. A call with no such grouping crosses no link, so its total counts each entity once.
- A filter on such a column keeps the entities with a row that meets it (`EXISTS`), each
  counted once. Beside a grouping on the same link, the filter and the grouping read the same
  stored row, and two groupings are the values one row holds together, never every pairing of
  an entity's values. A `not` or `missing` test there asks which reading is meant, or is refused
  beside a grouping, as across any one-to-many hop.
- A share, a window (`cumulative`, `rolling`, ...), a per-entity value or a distribution across
  a link is refused with `MIXED_GRAIN_INVALID`: it would add overlapping groups. Ask for the
  total in its own query, or select it beside the groups as a [total](#coarser-totals), which
  is the call without the grouping and counts each entity once.
- A model with no key reaching a link answers a distinct count only; its sums and row counts are
  refused, since nothing tells its rows apart.

Their limits are listed under [Known limitations](#links-and-one-per-key-models).

## `where`

Items are ANDed. Each is a condition on a dimension or an [entity filter](#entity-filters).
"Or" is a list value.

```json
{"field": "dimension.jaffle_store_name", "op": "is", "value": ["Brooklyn", "Philadelphia"]}
```

A measure or metric in `where` is refused with the entity filter that says it ("customers with
20 or more orders" is an entity filter on the customer), or `having` for a result threshold. A
condition on a date dimension is legal; for a time window, use `time`.
The warehouse parses date and timestamp filter values; validation rejects only obvious
mistakes such as text without digits or a month outside 1–12. DuckDB also accepts forms
such as `2017-09-04 12:00:00 UTC` and `2017-09-04T24:00:00`. If DuckDB rejects a temporal
cast and its reported value identifies one date filter, the redacted
`QUERY_EXECUTION_ERROR` names that filter's `where[i].value` path.

### Filter words

| `op` | `value` | A row whose value is unknown | Means |
|---|---|---|---|
| `is` | one value or a list | is dropped | "only X" |
| `not` | one value or a list | **is kept** | "excluding X", "other than X" |
| `gt` `gte` `lt` `lte` | one number, string, ISO date or date-time | is dropped | "over", "at least", "under", "at most" |
| `contains` | text | is dropped | "containing"; case-insensitive and literal (no wildcards) |
| `starts_with` | text | is dropped | "starting with", "beginning with"; case-insensitive and literal |
| `ends_with` | text | is dropped | "ending with", "ending in"; case-insensitive and literal |
| `not_contains` `not_starts_with` `not_ends_with` | text | **is kept** | "not containing", "not starting with", "not ending with" |
| `missing` | `true` | only unknown rows match | "with no X" |
| `missing` | `false` | only known rows match | "only where X is known" |

- Words are read in any case and spacing (`GT`, `not  in`), and SQL spellings as their word:
  `=`, `==`, `eq`, `IN` → `is`;
  `!=`, `<>`, `neq`, `NOT IN` → `not`; `>`, `>=`, `<`, `<=` → `gt`, `gte`, `lt`, `lte`. A
  spelling takes its word's meaning, so `!=` keeps unknown rows too. Answers, refusals and
  clarifications always write the word.
- For `is` and `not`, a one-element list is the same value as the scalar; the ranges, the text
  words and `missing` take one value, never a list. A `null` value, a `null` inside a list
  and an empty list are refused with the `missing` form. There is no `between`: a range is two
  conditions, `gte` and `lt`. `IS NULL`, `IS NOT NULL`, `LIKE` and `IS DISTINCT FROM` are refused
  with the word to write.
- `LIKE`, `NOT LIKE` and `ILIKE`, in any case, are refused the same way here, in an entity
  filter, in a condition inside a kind and in a `comparison`, with the text word for the
  pattern's shape: `'X%'` is `starts_with`, `'%X'` is `ends_with` and `'%X%'` is `contains`
  (`NOT LIKE` takes the `not_` form), each ignoring case. A pattern with `_` or an inner `%`
  has no word; the hint is `is` (or `not`) with the exact values, which `valid-values` lists.
  In a query call, on a dimension declared as an id or another non-text type, the hint uses exact
  values instead of a text word, even when the stored codes contain letters. In a
  `comparison`, use `=` or `<>` with an exact value.
  A package's own `comparison` (a measure's `expr`) refuses `LIKE` the same way.
- A text word ignores case by lowering the value and the text with the same function, so it
  gives the same rows on every warehouse. A column's own collation is set aside first: Postgres
  lowers both under its ICU root collation (`und-x-icu`), and Snowflake, BigQuery and
  Databricks drop the column's collation. ClickHouse reads the value as plain text first, so a
  FixedString or Enum column works. `É` matches `é`; lowering never expands a letter, so `ss`
  never matches `ß`.
- Text holding a letter that some warehouse lowers differently is refused at its path, on every
  warehouse, with the hint to match exact values with `is` (`valid-values` lists them). See
  [Known limitations](#known-limitations).
- A string in a call never holds NUL or a lone surrogate (`\ud800`): no warehouse reads them as
  text, so they are refused at their path, naming the code point.
- A text word reads text: on an id, number, date, timestamp or boolean dimension it is refused
  at its path with the words that compare one (`is` or `not` with the exact ids, the ranges for
  a number, `time` or the ranges for a date). A result (`having`), an entity filter's `having`
  and a per-entity value compare a number, so a text word or a `LIKE` there is refused with
  the words that compare a number.
- Two `is` conditions on one field are an AND (usually no row); a list is the OR.

### Unknown values

"Unknown" is SQL NULL, or a value the package declares as unknown for that dimension
(`unknown_values`, see [Package authoring](PACKAGE_AUTHORING.md#dimensions)). `not` keeps
unknown rows, because "sessions other than these two orders" includes the sessions that never
converted; so do `not_contains`, `not_starts_with` and `not_ends_with`. When a `where` condition
with one of them kept any, the answer counts them in `UNKNOWN_ROWS_KEPT`, naming the word, and
says how to leave them out:

```json
{
  "select": ["measure.jaffle.session_starts"],
  "where": [{"field": "dimension.jaffle_storefront_session_converted_order_id", "op": "not",
             "value": ["2c54134f-2322-4704-9f82-e2e9b77bcb19", "3d12eb16-1bbb-47ad-a732-bf156c322fa6"]}]
}
```

```jsonc
{"rows": [{"session_starts": 8}],
 "warnings": [{"code": "UNKNOWN_ROWS_KEPT",
   "message": "'not' keeps rows whose dimension.jaffle_storefront_session_converted_order_id is unknown: the answer includes 2 with no value. Add {\"field\": \"dimension.jaffle_storefront_session_converted_order_id\", \"op\": \"missing\", \"value\": false} to leave them out.",
   "details": {"field": "dimension.jaffle_storefront_session_converted_order_id", "rows": 2, "values": [null]}}]}
```

`missing` asks for the unknown rows themselves. "Sessions that never converted":

```json
{
  "select": ["measure.jaffle.session_starts"],
  "where": [{"field": "dimension.jaffle_session_converted_at", "op": "missing", "value": true}]
}
```

```jsonc
{"rows": [{"session_starts": 2}]}
```

A value listed in `unknown_values` is refused in `is` and `not` with the `missing` form.
Normalising true unknowns to NULL in the warehouse models is the first choice;
`unknown_values` is for sources you cannot change.

`contains` matches a substring, `starts_with` a prefix and `ends_with` a suffix, each ignoring
case; `%`, `_` and `\` are literal characters:

```json
{
  "select": ["measure.jaffle.item_count"],
  "group_by": ["dimension.jaffle_item_product_name"],
  "where": [{"field": "dimension.jaffle_item_product_name", "op": "contains", "value": "Chai"}]
}
```

```jsonc
{"rows": [{"dimension.jaffle_item_product_name": "chai and mighty", "item_count": 13576}]}
```

"Products starting with T" is `starts_with`. `contains` with `"T"` would also count "chai and
mighty", "nutellaphone who dis?" and "doctor stew":

```json
{
  "select": ["measure.jaffle.item_count"],
  "group_by": ["dimension.jaffle_item_product_name"],
  "where": [{"field": "dimension.jaffle_item_product_name", "op": "starts_with", "value": "T"}]
}
```

```jsonc
{"rows": [{"dimension.jaffle_item_product_name": "the krautback", "item_count": 5403},
          {"dimension.jaffle_item_product_name": "tangaroo", "item_count": 13794}]}
```

### Entity filters

An entity filter names the entity whose rows are tested, and keeps the measure's rows whose
entity passes. It has exactly one of `any`, `none` or `having`:

```json
{"select": ["measure.jaffle.order_count"],
 "where": [{"entity": "entity.jaffle_item", "any": [{"field": "dimension.jaffle_item_product_type", "op": "is", "value": "beverage"}]}]}
```

```jsonc
{"rows": [{"order_count": 55974}]}   // orders with at least one beverage
```

```json
{"select": ["measure.jaffle.order_count"],
 "where": [{"entity": "entity.jaffle_item", "none": [{"field": "dimension.jaffle_item_product_type", "op": "is", "value": "beverage"}]}]}
```

```jsonc
{"rows": [{"order_count": 3678}]}    // orders with no beverage
```

- `any`: at least one of the entity's rows meets every condition (an `EXISTS`, so many
  matching items never multiply an order count). `none`: no row does (a `NOT EXISTS`; an order
  with no items is kept). Conditions are on the entity's own dimensions or its many-to-one
  lookups. Entity filters do not nest.
- `having` + `over`: conditions on the entity's own aggregates (a measure or metric id, compared
  with `is`, `not`, `gt`, `gte`, `lt` or `lte`). `over` says which of the entity's rows the
  aggregate reads, per entity and never split by the call's other dimensions:
  - `all_time`: every row of the entity, ignoring the call's window ("customers who have ever
    ordered 20 times");
  - `window`: the entity's rows inside the call's window ("customers who ordered 20 times last
    month");
  - `period`: per entity per output period (needs `time.grain`).

  An entity whose aggregate is unknown (`NULL`: no data, or amounts that are all NULL) fails
  every threshold, `not` included.

```json
{"select": ["measure.jaffle.order_count"],
 "time": {"last": {"unit": "month", "value": 1}},
 "where": [{"entity": "entity.jaffle_customer", "having": [{"field": "measure.jaffle.order_count", "op": "gte", "value": 20}], "over": "all_time"}]}
```

```jsonc
{"rows": [{"order_count": 7956}],
 "window": {"start": "2017-08-01", "end": "2017-09-01", "now": "2017-09-04T12:00:00Z", "partial": false, "data_through": "2017-08-31"},
 "assumptions": [{"code": "WINDOW_TOTAL",
                  "message": "The time window had no grain, so it was aggregated as one total over the window with no time column. Set time.grain to get one row per period."},
                 {"code": "ENTITY_FILTER_OVER", "entity": "entity.jaffle_customer", "over": "all_time",
                  "message": "Each entity.jaffle_customer's aggregate was taken over all of its rows, ignoring the query window."},
                 {"code": "GOVERNED_METRIC_EXISTS", "measure": "measure.jaffle.order_count",
                  "metrics": ["metric.sales.high_value_customer_orders", "metric.sales.repeat_customer_orders"], "count": 2,
                  "message": "measure.jaffle.order_count is also defined as a governed metric (metric.sales.high_value_customer_orders, metric.sales.repeat_customer_orders). Select one to use the package's definition."}]}
```

With `"over": "window"` the same call keeps the customers with 20 orders in August and reads
`1790`. A `having` without `over` is never guessed: it is refused with
`ENTITY_FILTER_PERIOD_UNSTATED`, whose `details.options` are the three `over` words.

When the filter keeps no entity, the measure has no rows to count. Under the default
`observation_scope: "dataset"` that reads 0 inside the loaded data; under `"query"` it reads
`NULL`. No customer placed 80 orders in a day of the last week:

```json
{"select": ["measure.jaffle.order_count"],
 "time": {"last": {"unit": "week", "value": 1}, "grain": "day"},
 "where": [{"entity": "entity.jaffle_customer", "having": [{"field": "measure.jaffle.order_count", "op": "gte", "value": 80}], "over": "window"}]}
```

```jsonc
{"rows": [{"period": "2017-08-28", "period_partial": false, "order_count": 0},
          {"period": "2017-08-29", "period_partial": false, "order_count": 0},
          {"period": "2017-08-30", "period_partial": false, "order_count": 0},
          {"period": "2017-08-31", "period_partial": false, "order_count": 0},
          {"period": "2017-09-01", "period_partial": false, "order_count": null},
          {"period": "2017-09-02", "period_partial": false, "order_count": null},
          {"period": "2017-09-03", "period_partial": false, "order_count": null}],
 "observation_scope": "dataset"}
```

With `"observation_scope": "query"` every day reads `null`. Under `dataset`, an entity filter
beside a `where` condition, or in a call whose `distribution` sits beside a sum or count, can't
tell no data from data of nothing: it is refused with `EMPTY_GROUPS_UNSETTLED` and the hint to
send `observation_scope: "query"`.

### Conditions on a child entity

A plain condition on a dimension across a one-to-many hop (an item's product type, beside an
order count) keeps its one reading: one positive condition is an `any` of one. Two conditions on
one child, or one `not` or `missing` condition, can mean different things ("an item that is a
beverage and contains chai", or "a beverage and some item with chai"), so the call is refused
with `AMBIGUOUS_CHILD_SCOPE`. Each option in `details.clarification.options` carries the whole
`where` to resend:

```json
{"select": ["measure.jaffle.order_count"],
 "where": [{"field": "dimension.jaffle_item_product_type", "op": "not", "value": "beverage"}]}
```

```jsonc
// AMBIGUOUS_CHILD_SCOPE: "has an item that is not a beverage" or "has no beverage item"?
{"options": [
  {"id": "any_not", "where": [{"entity": "entity.jaffle_item", "any": [{"field": "dimension.jaffle_item_product_type", "op": "not", "value": "beverage"}]}]},
  {"id": "none", "where": [{"entity": "entity.jaffle_item", "none": [{"field": "dimension.jaffle_item_product_type", "op": "is", "value": "beverage"}]}]}]}
```

The complements are exact: `is` ↔ `not`, `missing: true` ↔ `missing: false`. See
[Child entities in depth](#child-entities-in-depth) for the rules.

## `having`

Conditions on result rows, after aggregation. `field` is a select alias, or a measure or metric
id (computed for the test, not returned). Same words; `{"op": "missing", "value": true}` keeps
results that are NULL. To filter on a composed aggregate expression, add it to `select`
with an alias and use that alias as `having.field`; an unselected expression is refused
with this hint. "Which X did Y" is `group_by` X plus a `having`:

```json
{"select": ["measure.jaffle.revenue_usd"],
 "group_by": ["dimension.jaffle_customer_name"],
 "having": [{"field": "revenue_usd", "op": "gte", "value": 2700}],
 "order_by": [{"field": "revenue_usd", "direction": "desc"}]}
```

```jsonc
{"rows": [{"dimension.jaffle_customer_name": "Gerald Odom", "revenue_usd": 3029.390000000001},
          {"dimension.jaffle_customer_name": "Jeremy Hernandez", "revenue_usd": 2753.8499999999995},
          {"dimension.jaffle_customer_name": "Harold Jacobson", "revenue_usd": 2718.800000000001},
          {"dimension.jaffle_customer_name": "Richard Holloway", "revenue_usd": 2714.580000000001}]}
```

A result threshold filters result rows; to filter which entities count toward a measure, use an
[entity filter](#entity-filters).

## `time`

`time` takes `temporal_role`, `grain`, one window spelling and `fill`; nothing else.

| Spelling | Window | Example |
|---|---|---|
| `"last": {"unit", "value"}` | the last N complete periods before `now` | `{"last": {"unit": "week", "value": 2}, "grain": "week"}` |
| `"this": "<unit>"` | from the start of the current period to `now` (partial) | `{"this": "month"}` |
| `"start"` and `"end"` | `[start, end)`: `start` inclusive, `end` exclusive | `{"start": "2017-01-01", "end": "2017-04-01", "grain": "month"}` |
| `"start"` alone / `"end"` alone | since (up to `now`, partial) / until | `{"start": "2017-06-01", "grain": "month"}` |
| no window | all time | `{"grain": "year"}` |

- Units and grains: `day`, `week`, `month`, `quarter`, `year`. Weeks start on Monday (ISO).
  Bounds are ISO dates or date-times; a date-time bound may cut inside a day.
- `grain` gives one row per period in a `period` column, an ISO date (the period's first day).
  Without `grain` the answer is one total per group over the window, with no time column; the
  answer says so in `assumptions` (`WINDOW_TOTAL`). A call with only `group_by` lists each
  group with rows in the window once. A select that is read per period (`cumulative`,
  `prior_period`, `rolling`, `period_to_date`, a conversion, a distribution) has no one total,
  so beside a window without a grain it is refused with `WINDOW_TOTAL_UNSUPPORTED` at
  `time.grain`; the hint is the call's time block with a grain it answers at.
- One window spelling per call. `time` with only `temporal_role` is refused ("say a grain or
  a window").
- A relative phrase is `last` or `this`; a dated phrase is `start`/`end` ("in Q3" is
  `start: 2017-07-01, end: 2017-10-01`; "on May 3" is `start` and `end` a day apart); "since
  launch" or "all time" is no window.

```json
{"select": ["measure.jaffle.order_count", "measure.jaffle.revenue_usd"],
 "time": {"last": {"unit": "week", "value": 2}, "grain": "week"}}
```

```jsonc
{"rows": [{"period": "2017-08-21", "period_partial": false, "order_count": 1961, "revenue_usd": 24249.450000000146},
          {"period": "2017-08-28", "period_partial": false, "order_count": 1470, "revenue_usd": 16381.80000000001}],
 "window": {"start": "2017-08-21", "end": "2017-09-04", "now": "2017-09-04T12:00:00Z", "partial": false, "data_through": "2017-08-31"},
 "clock": "temporal_role.jaffle_order_time",
 "warnings": [{"code": "PARTIAL_DATA",
   "message": "Data runs through 2017-08-31, so the week of 2017-08-28 covers 4 of its 7 days.",
   "details": {"through": "2017-08-31", "days_covered": 4, "days": 7, "period": "2017-08-28"}}]}
```

### The clock

`temporal_role` names the clock: which date a row is counted on. Omitted, it is the selected
measures' default clock. When they read different clocks the call is refused with
`MIXED_TIME_ROLES`, and each of `details.options` names one clock and the selections it reads:

```json
{"select": ["measure.jaffle.order_count", "measure.jaffle.session_starts"], "time": {"grain": "month"}}
```

```jsonc
// MIXED_TIME_ROLES at time.temporal_role: "the selected measures read different clocks; say which one"
{"options": [{"temporal_role": "temporal_role.jaffle_order_time", "reads": ["measure.jaffle.order_count"]},
             {"temporal_role": "temporal_role.jaffle_session_started_at", "reads": ["measure.jaffle.session_starts"]}]}
```

The answer always reports the `clock` it read. Bounds and buckets are in the clock's time zone
(UTC unless the temporal role declares one). A package metric pinned to a clock keeps it; a
measure's clock is pinned inline with `{"kind": "measure", "measure", "temporal_role"}`.
Fiscal calendars are not read in this release: weeks, months, quarters and years are Gregorian.

### Named and open windows

A **named** window (`last`, `this`, or `start` with `end`) always answers with its periods:
one row per period under a grain, one total without. An **open** window (no window, or one
bound) answers the periods that have rows; `fill: true` on an open window with a grain also
returns the empty periods between the first and the last period the answer has. `fill` on a
named window is refused, because a named window already returns every period. The
exception is a `group_by` on a date or time dimension: each of its values sits in one period,
so filling would repeat every value in every period. Such an answer keeps the rows that hold
data and says so in `assumptions` (`OBSERVED_PERIODS_ONLY`), and `fill: true` beside such a
group is refused at `time.fill`. The same assumption, with its `reason`, marks a named window
that can't list its empty periods: on ClickHouse, which has no day spine, or on a clock whose
zone is not a known time zone. Its series keeps the periods that have rows, never a made-up 0.

What an empty period reads ([Empty groups](#empty-groups-null-or-0) has the full rule):

- **0** inside the data: Brooklyn opened in March 2017, so January and February read 0, not
  missing rows.

```json
{"select": ["measure.jaffle.order_count"],
 "where": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "Brooklyn"}],
 "time": {"start": "2017-01-01", "end": "2017-04-01", "grain": "month"}}
```

```jsonc
{"rows": [{"period": "2017-01-01", "period_partial": false, "order_count": 0},
          {"period": "2017-02-01", "period_partial": false, "order_count": 0},
          {"period": "2017-03-01", "period_partial": false, "order_count": 2092}],
 "window": {"start": "2017-01-01", "end": "2017-04-01", "now": "2017-09-04T12:00:00Z", "partial": false, "data_through": "2017-08-31"}}
```

- **NULL with `NO_DATA_YET`** after the data's edge: the last week has orders through Thursday
  2017-08-31 and nothing says the data is loaded past that last row, so Friday to Sunday read
  NULL, never 0. `window.data_through` is that edge: the date, in the clock's zone, of the last
  row at or before `now`. A package that records how far its load ran says so with
  `zero_through` (["Empty periods"](PACKAGE_AUTHORING.md#empty-periods)): a quiet week the load
  covers then reads 0.

```json
{"select": ["measure.jaffle.order_count"], "time": {"last": {"unit": "week", "value": 1}, "grain": "day"}}
```

```jsonc
{"rows": [{"period": "2017-08-28", "period_partial": false, "order_count": 379},
          {"period": "2017-08-29", "period_partial": false, "order_count": 364},
          {"period": "2017-08-30", "period_partial": false, "order_count": 373},
          {"period": "2017-08-31", "period_partial": false, "order_count": 354},
          {"period": "2017-09-01", "period_partial": false, "order_count": null},
          {"period": "2017-09-02", "period_partial": false, "order_count": null},
          {"period": "2017-09-03", "period_partial": false, "order_count": null}],
 "window": {"start": "2017-08-28", "end": "2017-09-04", "now": "2017-09-04T12:00:00Z", "partial": false, "data_through": "2017-08-31"},
 "warnings": [{"code": "NO_DATA_YET",
   "message": "No data yet for measure.jaffle.order_count: its last row is dated 2017-08-31. Nothing says this data is loaded past it, so later days read NULL, not 0.",
   "details": {"outputs": ["order_count"], "measures": [{"id": "measure.jaffle.order_count", "edge": "2017-08-31", "edge_source": "last_row"}]}}]}
```

- **NULL** before the first row: the days before 2016-09-01 read NULL.

```json
{"select": ["measure.jaffle.order_count"], "time": {"start": "2016-08-30", "end": "2016-09-02", "grain": "day"}}
```

```jsonc
{"rows": [{"period": "2016-08-30", "period_partial": false, "order_count": null},
          {"period": "2016-08-31", "period_partial": false, "order_count": null},
          {"period": "2016-09-01", "period_partial": false, "order_count": 47}]}
```

The same call with `"last": {"unit": "week", "value": 1}` and no grain reads one total,
`1470`, over the four loaded days, and says so (`PARTIAL_DATA`, below).

### Partial periods

A period that ends after `now` is partial. Each row of a grain series carries
`period_partial`; `window.partial` is true when the window reaches past `now`. A `this` window
is partial by definition and says how much of the period it covers:

```json
{"select": ["measure.jaffle.order_count"], "time": {"this": "week", "grain": "day"}}
```

```jsonc
// sent with now = "2017-08-30T15:00:00Z", a Wednesday afternoon
{"rows": [{"period": "2017-08-28", "period_partial": false, "order_count": 379},
          {"period": "2017-08-29", "period_partial": false, "order_count": 364},
          {"period": "2017-08-30", "period_partial": true, "order_count": 328}],
 "window": {"start": "2017-08-28", "end": "2017-08-30T15:00:00Z", "now": "2017-08-30T15:00:00Z", "partial": true, "data_through": "2017-08-30"},
 "assumptions": [{"code": "PARTIAL_PERIOD", "message": "the week of 2017-08-28 so far (3 of 7 days)"},
                 {"code": "GOVERNED_METRIC_EXISTS", "measure": "measure.jaffle.order_count",
                  "metrics": ["metric.sales.high_value_customer_orders", "metric.sales.repeat_customer_orders"], "count": 2,
                  "message": "measure.jaffle.order_count is also defined as a governed metric (metric.sales.high_value_customer_orders, metric.sales.repeat_customer_orders). Select one to use the package's definition."}]}
```

`last` counts only complete periods, so it is never partial. A window with only `start` runs to
`now`, so it is partial. Both `this` and a `start`-only window end at the request's `now`
exactly, to the microsecond: a row stamped after `now` is not counted.

A complete period can still hold days the data doesn't reach. When a window total, or a row of
a day-or-coarser series, spans a whole day (in the clock's zone) after `window.data_through`
and before `now`'s day, the answer carries `PARTIAL_DATA`: "Data runs through 2017-08-31, so
this total covers 4 of the window's 7 days" (for a series row, "the week of 2017-08-28 covers 4
of its 7 days"), with `details` `{through, days_covered, days, period}` (`period` only for a
series row). The number is what the loaded days hold; `partial` keeps its meaning (the period
ends after `now`). A nightly load whose only gap is today's unfinished day says nothing, and a
period wholly after the edge is `NO_DATA_YET`'s to explain.

### Balances

A balance (a measure declared `snapshot: end_of_period`, such as inventory on hand) on an
`as_of_time` clock reads each period's **closing day**: the period's last day with a snapshot.
A series with no snapshot on that day is not carried forward from an earlier one. A month of
inventory is its last snapshot in that month; "inventory right now" is the last closing day:

```json
{"select": ["measure.jaffle.inventory_on_hand_eop"],
 "time": {"start": "2017-03-01", "end": "2017-05-01", "grain": "month"}}
```

```jsonc
{"rows": [{"period": "2017-03-01", "period_partial": false, "inventory_on_hand_eop": 870},
          {"period": "2017-04-01", "period_partial": false, "inventory_on_hand_eop": 860}],
 "window": {"start": "2017-03-01", "end": "2017-05-01", "now": "2017-09-04T12:00:00Z", "partial": false, "data_through": "2017-04-01"},
 "clock": "temporal_role.jaffle_inventory_day"}
```

```json
{"select": ["measure.jaffle.inventory_on_hand_eop"], "time": {"last": {"unit": "day", "value": 1}, "grain": "day"}}
```

```jsonc
// sent with now = "2017-04-02T09:00:00Z"
{"rows": [{"period": "2017-04-01", "period_partial": false, "inventory_on_hand_eop": 860}],
 "window": {"start": "2017-04-01", "end": "2017-04-02", "now": "2017-04-02T09:00:00Z", "partial": false, "data_through": "2017-04-01"}}
```

Without a grain, a window over a balance reads the window's closing day (`860` for March and
April together), never the sum of its days; with no `time` at all, it reads the last day with
a snapshot. A period after the last snapshot reads `NULL` with `NO_DATA_YET`, as any measure's
does.

The closing day is the day bucket `time.grain: day` puts a snapshot in. A row whose clock is
NULL is never a snapshot, and rows of a series tied on its last clock value are all read. A
balance read inside an entity filter or a metric predicate reads the same way. Two shapes are
refused rather than risk another day: a share of such a balance filtered by entity sets
(`REWRITE_NOT_SUPPORTED`, `details.reason: entity_set_ratio_closing_day`; ask for the part and
the whole as separate measures), and, on DuckDB and Postgres, a balance on a timestamp clock
whose zone isn't the one the call runs in, UTC without a `time` block (`MIXED_TIME_ROLES`,
`details.reason: closing_day_zone`; give the call a window on the balance's own clock). A
DATE clock is never refused.

Every such answer names the day each read used in `assumptions`: `BALANCE_CLOSING_DAY` with
`measure`, `outputs` (the columns it counts for; empty for the whole answer, as an entity
filter's read; `null` when the engine can't tell, and then the disclosure is dropped beside
any withheld column), `nested`, `closing_day` (the latest, when there are several periods) and
`periods`. When series seen earlier in a period had no snapshot on its closing day, it also
carries the warning `BALANCE_SERIES_MISSING_ON_CLOSING_DAY`: "1 of 2 series of ... had a
snapshot earlier in the period but none on its closing day, 2020-12-20, so they are not
counted", with `details` `{measure, outputs, nested, closing_day, periods, checked, series,
series_missing, periods_missing}`. Those series may have stopped, which is right to leave
out, or take their snapshots on other days, or the table may record only changes; then the
balance is undercounted. Group by the clock's day to see each day's snapshots. A caller
granted a metric but not the measure behind it gets both with the day and counts, named by
the granted metric that reads it instead of the measure; a read the engine can't tie to a
granted column of the call says nothing to such a caller.

A stock on an event- or state-time clock is a current-state table (a customer's lifetime
spend, on the customers table) and is read as keyed: each series' last row in the period,
whatever its day. Lifetime spend for the first quarter of 2017 on the clock
`temporal_role.jaffle_customer_first_order_at` adds up all 341 customers who first ordered then.

## `order_by`

`{"field": <select alias | group_by id | "period">, "direction": "asc" | "desc"}`; direction
defaults to `asc` and is lowercase. `period` is the time axis (`"time"` is refused with
`period`). NULLs sort last in both directions. A field that names no column is refused with
`INVALID_ORDER_BY`, whose details list the columns it can name.
A selected measure or metric id orders by the one select item that reads it as itself (no
`temporal_role` or `parameters`, and no `aggregation` but its own); read so twice, or only with an
override, it is refused with those items' columns in `closest_matches`.

A ranking is `group_by` + `order_by` + `limit`. "Top 3 customers by revenue last month":

```json
{"select": ["measure.jaffle.revenue_usd"],
 "group_by": ["dimension.jaffle_customer_name"],
 "time": {"last": {"unit": "month", "value": 1}},
 "order_by": [{"field": "revenue_usd", "direction": "desc"}],
 "limit": 3}
```

```jsonc
{"rows": [{"dimension.jaffle_customer_name": "Natalie Hatfield", "revenue_usd": 444.08000000000004},
          {"dimension.jaffle_customer_name": "Chelsey Ramirez", "revenue_usd": 408.7199999999999},
          {"dimension.jaffle_customer_name": "Steven Williams", "revenue_usd": 383.08}]}
```

## `limit`

A positive integer: the question's top N. `0`, `null` and non-integers are refused. The row cap
of a transport is a separate tool argument (MCP `max_rows`).

With a `limit`, rows tied on every `order_by` term at the cut-off are broken by the remaining
`group_by` columns, then `period`, ascending with NULLs last (never by a measure), so the same
call keeps the same rows, and the answer says ties were cut:

```json
{"group_by": ["dimension.jaffle_store_name", "dimension.jaffle_store_tax_rate"],
 "order_by": [{"field": "dimension.jaffle_store_tax_rate", "direction": "asc"}],
 "limit": 1}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "dimension.jaffle_store_tax_rate": 0.04}],
 "warnings": [{"code": "TIES_AT_LIMIT",
   "message": "At least 2 rows tie on the requested order at the limit of 1; the group_by columns, then the period, ascending, chose which of them are returned.",
   "details": {"at": 1, "tied": 2}}]}
```

Execution fetches one row past the limit to see the tie, so `tied` is a lower bound. A
transport cap at or below `limit` takes precedence, and then ties cannot be reported.

With no `order_by`, a `limit` keeps the first rows of the call's own order, so the same call
keeps the same rows. Without `time.grain` that order is the `group_by` columns, ascending with
NULLs last. With `time.grain` it is the series order the answer always has: `period` first,
then the `group_by` columns, ascending, with a NULL group placed by the warehouse's default
(a monthly series by store with `limit: 2` keeps the first month's two stores).

## `totals`

Subtotal and grand-total rows after the call's own rows. `totals` lists the `group_by` ids the
subtotal rows keep, outermost first; each shorter prefix of it is one level of total rows, down
to the grand total, and `[]` asks for the grand total alone. A level that keeps every `group_by`
column is the call's own grain and adds nothing. With `time.grain`, every total row keeps its
period: total rows add up the groups within each period.

Each total row is the call itself at its level's grain, from the rows the call keeps (its
`where`, window, entity filters, routes, row filters and policies), settled as that call is on
its own (data of nothing reads `0`, a period not loaded yet `null`), so every column is computed
from its own rows there, never added up from the call's rows: a distinct count counts distinct
values, an average averages every row, a ratio divides the totals, and across a
[link](#groups-across-a-link) each entity counts once. A rolled-up column reads `null` on a
total row, and the `total_over` column names the `group_by` ids the row adds up (`null` on the
call's own rows), so a total row never reads as a NULL group:

```json
{"select": ["measure.jaffle.order_count", "measure.jaffle.ordering_customer_count"],
 "group_by": ["dimension.jaffle_store_name"],
 "time": {"start": "2017-01-01", "end": "2017-04-01"},
 "totals": []}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "order_count": 2092, "ordering_customer_count": 252, "total_over": null},
          {"dimension.jaffle_store_name": "Philadelphia", "order_count": 10753, "ordering_customer_count": 366, "total_over": null},
          {"dimension.jaffle_store_name": null, "order_count": 12845, "ordering_customer_count": 618, "total_over": "dimension.jaffle_store_name"}],
 "assumptions": [{"code": "TOTALS", "levels": ["dimension.jaffle_store_name"],
                  "message": "Rows whose total_over names columns add those columns up: each is the call at that grain, computed from the rows the call keeps before having and limit, never added up from the returned rows."}]}
```

`total_over` lists every rolled-up id, joined with `, `, in `group_by` order. A level keeps the
entity key of each name it keeps, as any call does, and a key `totals` names stays on its level
while its name rolls up. The total rows come after the call's own rows, level by level from the finest to the grand
total. The call's own rows, and each level's, come in the call's `order_by` (by default the
period first), then the period and group keys ascending with NULLs last. `having` and `limit`
cut only the call's own rows: total rows always come back, and they count the rows `having` and
`limit` leave out (ties at a `limit` are broken and reported as usual). A transport's row cap
counts total rows too, so a truncated answer can end before them; a returned total row shows
every row of the call's own came back. Every warning about the call's rows (`NO_DATA_YET`,
`NO_DATA_IN_SCOPE`, `EMPTY_RESULT_WINDOW`, `NULL_PRESERVING_HISTORY`, `TIES_AT_LIMIT`) reads
those rows only, as without `totals`.

`totals` is refused at its path when an item is not a `group_by` id of the call (once each),
when the call has no `group_by`, when a select item is named `total_over`, and beside a `share`
or a [`total`](#coarser-totals), which have no reading on a total row. A policy that requires a
grouping refuses `totals` (the grand total groups by nothing) with `POLICY_DENIED`, as do
withheld values.

## `routes`

When two join routes between two entities would answer differently, the package's
`graph.path_preferences` decide, or the entity's own key does, or, when exactly one route is a
lookup (one row at every hop), that lookup does, naming itself and the other routes in the
answer's `routes`; otherwise validation refuses with `AMBIGUOUS_PATH` and asks which route the
question means. A call's `routes` entry overrides a default as it overrides any other rung. Each option in
`details.clarification.options` carries a ready-to-send `query`: the same call with that
option's route in `routes`.

```json
{"select": ["measure.jaffle.customer_count"],
 "group_by": ["dimension.jaffle_customer_history_segment"],
 "time": {"grain": "year"},
 "routes": [{"from": "entity.jaffle_customer", "to": "entity.jaffle_customer_history",
             "via": ["relationship.orders_customer", "relationship.jaffle_order_customer_history"]}]}
```

```jsonc
{"rows": [{"dimension.jaffle_customer_history_segment": "new", "period": "2016-01-01", "period_partial": false, "customer_count": 4},
          {"dimension.jaffle_customer_history_segment": null, "period": "2016-01-01", "period_partial": false, "customer_count": 273},
          {"dimension.jaffle_customer_history_segment": null, "period": "2017-01-01", "period_partial": true, "customer_count": 662}],
 "routes": [{"from": "entity.jaffle_customer", "to": "entity.jaffle_customer_history",
             "via": ["relationship.orders_customer", "relationship.jaffle_order_customer_history"],
             "other_routes": [{"from": "entity.jaffle_customer", "to": "entity.jaffle_customer_history",
                               "via": ["relationship.storefront_sessions_customer", "relationship.storefront_sessions_order",
                                       "relationship.jaffle_order_customer_history"]}]},
            {"from": "entity.jaffle_order", "to": "entity.jaffle_customer", "via": ["relationship.orders_customer"]}]}
```

- One route per entity pair, for this call only; it overrides the package's own row for the
  pair and is never cached as a default. To make it the default, record a
  `graph.path_preferences` row (`record_route_decision`).
- The route must be one the engine offers for the pair; anything else is refused
  (`details.reason`: `route_not_offered`, `invalid_route_decision`, `route_decision_unused`).
  A `via` hop that names no relationship gets the relationships that join `from` to `to` in one
  hop (`closest_matches`), else up to three of the pair's routes, ready to send (`other_routes`,
  `more_routes`), else those touching either entity. Under a row filter that reads
  an entity on any of the pair's routes, a call route is refused with `POLICY_DENIED`
  (`route_override_under_row_policy`).
- The answer's `routes` names every route chosen among several, the call's and the package's
  (above, an order reaches its customer by its own key), at every verbosity. A call route the
  refusal offered lists up to three of the other options in `other_routes`, each ready to send
  as `routes`, with `more_routes` counting the rest: one-step switches. No alternative is
  executed: a different route is a different question. A compact or full answer adds how each
  route was chosen (`basis`: `routes` with the package basis it `replaced`, `own_key` with the
  rows that would make another route the default, or `recorded`).

## `observation_scope`

Whether a sum or count with no rows in a group reads 0 or NULL depends on whether its measure
has data "in scope". `dataset` (the default, or the package's `defaults.observation_scope`)
judges that before the call's `where`, entity filters included; `query` judges it inside the
call's filters. Chicago has no orders:

```json
{"select": ["measure.jaffle.order_count"],
 "where": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "Chicago"}]}
```

```jsonc
{"rows": [{"order_count": 0}], "observation_scope": "dataset"}
```

With `"observation_scope": "query"` the same call reads `[{"order_count": null}]` with
`NO_DATA_IN_SCOPE`: nothing in the call's filters holds a value. The answer always states the
scope it used.

## Inline expression kinds

Anywhere an expression goes (`select[].expression`, and inside a kind: `input`, `left`,
`right`, `numerator`, `denominator`, `base`, `converted`, `args`, ...) a reference is the id
string. Each kind has one spelling; [`schemas/query.v2.json`](../schemas/query.v2.json) lists
every kind with its keys (`$defs/Kind_<kind>`).

| Kind | Spelling |
|---|---|
| `measure` | `{kind: measure, measure, aggregation?, temporal_role?, parameters?}`: only to override the package's aggregation or clock |
| `prior_period` | `{kind: prior_period, input, offset: {unit, value}}` |
| `rolling` | `{kind: rolling, input, window: {unit, value}, partition_by?}` |
| `period_to_date` | `{kind: period_to_date, input, period, partition_by?}` |
| `cumulative` | `{kind: cumulative, input, partition_by?, window_scope?}` |
| `ratio` | `{kind: ratio, numerator, denominator}` |
| `share` | `{kind: share, input: <id>, over: [<group_by id> \| "period"]}` |
| `total` | `{kind: total, input: <id>, over: [<group_by id> \| "period"]}`: a select item of its own |
| `arithmetic` | `{kind: arithmetic, op, left, right}` |
| `aggregate` (`semi_additive`) | `{kind: aggregate, measure, aggregation?, filter?: {all: [conditions]}, window?, ...}` |
| `aggregate_if` | `{kind: aggregate_if, aggregation, condition, value?}` |
| `scoped_aggregate` | `{kind: scoped_aggregate, measure, where?, predicates?, anchor?, window?, ...}`; each predicate carries `over` |
| `metric_predicate` | `{kind: metric_predicate, entity, input, op, value, over, window?}` |
| `entity_value` | `{kind: entity_value, entity, input, where?}` |
| `distribution` | `{kind: distribution, function, over, p?, parameters?}` |
| `conversion` | `{kind: conversion, base, converted, entity, window, matching_mode, ...}` |
| `comparison`, `boolean`, `between` (`not_between`), `in` (`not_in`), `case`, `literal`, `call`, `column`, `nullif`, `date_add` | scalar expression parts |

A part that reads row values (`column`, `in`, `date_add`) is not a query value on its own: it is
refused at its path with the kinds a select item can be (`supported_kinds`), and a raw `column`
with the dimensions on it in `closest_matches`.

Conditions inside a kind (`aggregate.filter.all`, `scoped_aggregate.where`, ...) use the
[filter words](#filter-words); `over` uses the [entity filter](#entity-filters) words. Every
inline condition passes the caller's allowed filters, as a `where` condition does. Comparison
expressions (`kind: comparison`, `in`, `between`) keep SQL's own semantics: a NULL compares
unknown. A `comparison` also takes a text word as its `op`, with a text literal `right`
(`{"kind": "comparison", "op": "starts_with", "left": <column>, "right": {"kind": "literal",
"value": "K"}}`). Its `left` is a column that exactly one dimension declares, named as the
warehouse names columns (the exact name first, then an unquoted name in any ASCII case on every
warehouse but ClickHouse; never a Unicode casefold, so `straße` is not `strasse`). The word
reads it as a filter on that dimension does: its `unknown_values` are unknown, and a `not_` form
keeps unknown rows. A column that no dimension, or several, declares is refused.
`LIKE` is refused there as in `where`. An `arithmetic` op other than `add`, `subtract`,
`multiply` and `divide` (or `+`, `-`, `*`, `/`) is refused at its path with the closest op.

```json
{"select": ["measure.jaffle.revenue_usd",
            {"expression": {"kind": "prior_period", "input": "measure.jaffle.revenue_usd", "offset": {"unit": "month", "value": 1}}, "as": "month_before"}],
 "time": {"end": "2016-12-01", "grain": "month"}}
```

```jsonc
{"rows": [{"period": "2016-09-01", "period_partial": false, "revenue_usd": 18053.279999999995, "month_before": null},
          {"period": "2016-10-01", "period_partial": false, "revenue_usd": 21923.949999999997, "month_before": 18053.279999999995},
          {"period": "2016-11-01", "period_partial": false, "revenue_usd": 27916.819999999992, "month_before": 21923.949999999997}]}
```

`prior_period`, `rolling`, `period_to_date` and `cumulative` need `time.grain`. They read periods before the
ones they return, so a window that cuts their lookback is refused
([details](#windows-that-read-earlier-periods)); beside a named window a comparison is refused
with `WINDOWED_TIME_FILTER_UNSUPPORTED` ("ask for a series with a grain and read the periods
you need from it").

### The `aggregation` override

`{"kind": "measure", "measure": <id>, "aggregation": <statistic>}` (and the same key on
`aggregate` and `scoped_aggregate`) reads a statistic the measure allows (its
`allowed_aggregations`); any other is refused with `UNSUPPORTED_AGGREGATION` at
`...aggregation`, listing the allowed ones and saying to omit `aggregation` for the default. A
time-ordered statistic (`first_value`, `last_value`) on a measure with no clock is refused by
the clock rule instead. An override equal to the default changes nothing and is not stated.
Any other override is stated in `assumptions`, with the source rows the returned numbers
aggregated (counted beside the measure in the same scan; a rollup holds no source rows, so an
answer a rollup serves states none):

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "median_order": 6.24},
          {"dimension.jaffle_store_name": "Philadelphia", "median_order": 6.36}],
 "assumptions": [{"code": "AGGREGATION_OVERRIDE", "measure": "measure.jaffle.revenue_usd",
                  "aggregation": "median", "default": "sum", "rows": 59652,
                  "message": "measure.jaffle.revenue_usd is read as median, not its default sum, over 59652 source rows."}]}
```

Prefer a governed metric when the package has one: the metric is the package's definition,
and the raw measure may count rows the metric leaves out. Selecting a measure that a governed
metric filters adds one `GOVERNED_METRIC_EXISTS {measure, metrics, count, message}` per measure:
the first three such metrics the caller sees and doesn't select, and how many there are. When a metric hidden from the caller governs the measure, the raw
number is refused (`POLICY_DENIED`, naming neither the metric nor what it filters).

### Share of a total

`{"kind": "share", "input": <id>, "over": [<group_by id> | "period"]}` is the fraction (0 to 1)
of its input's total over the `over` columns, within each combination of the other `group_by`
columns (and `period`, unless `over` names it), among the rows the call keeps: after `where`,
`time`, entity filters and the caller's policies, before `limit`; `having` filters after it. A
zero or NULL total reads NULL. The input is one measure or metric id whose values add up across
rows (a sum or count of a flow, a count of distinct row keys, or sums and differences of
them); a ratio, an average or other statistic, or a stock is refused with `ROLLUP_UNSAFE`. Each `over` item is a `group_by` id of
the call, or `period` when `time` has a grain, or it is refused at its path. The denominator is
held to the caller's policies as a call of its own: its input grouped as the total is taken,
without the `over` columns and the key beside each `over` name. A share over a grouping a policy
requires is refused with `POLICY_DENIED` ("... and the share in 'store_share' reads it across
them"), as a [total](#coarser-totals) over it is, bare or inside another expression, and a share
of a withheld value is refused. In a call with an entity filter or `routes`, the denominator is
not checked on its own (see [Known limitations](#shares-and-totals-under-a-required-grouping)).
The answer names the denominator and the unit in `assumptions`, with how many rows the total
spans when every returned row's total spans the same number:

```json
{"select": ["measure.jaffle.revenue_usd",
            {"expression": {"kind": "share", "input": "measure.jaffle.revenue_usd", "over": ["dimension.jaffle_store_name"]}, "as": "store_share"}],
 "group_by": ["dimension.jaffle_store_name"]}
```

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Brooklyn", "revenue_usd": 259424.84999999977, "store_share": 0.34780436277846943},
          {"dimension.jaffle_store_name": "Philadelphia", "revenue_usd": 486468.1799998507, "store_share": 0.6521956372215306}],
 "assumptions": [{"code": "SHARE_OF_TOTAL", "column": "store_share", "input": "measure.jaffle.revenue_usd",
                  "over": ["dimension.jaffle_store_name"], "unit": "fraction", "rows": 2,
                  "message": "store_share is a fraction of the total of measure.jaffle.revenue_usd across dimension.jaffle_store_name in the rows the call keeps (2 rows per total)."}]}
```

`SHARE_OF_TOTAL` also accompanies a share nested inside `round(...)`, arithmetic or any
other select expression, for every share it contains. The disclosure describes the nested
share's denominator; the enclosing expression can change the output's unit (for example,
multiplying by 100 returns a percentage). Nested shares omit the optional row count.

### Coarser totals

`{"kind": "total", "input": <id>, "over": [<group_by id> | "period"]}` is its input at a
coarser grain beside each row: the call itself without the `over` columns, joined back on the
columns that remain (within each combination of the other `group_by` columns, and `period`
unless `over` names it; a NULL group meets its own total). The coarser call keeps the call's
`where`, `time` window, entity filters, routes, row filters and policies, so the total reads
the rows the call keeps; `having`, `order_by` and `limit` act on the call's rows after it, as
for a share. A `having` may filter on a total's column.

Every measure kind is computed at that grain from its own rows, never added up from the rows:
a distinct count counts distinct values, an average averages every row, a ratio divides the
totals, and across a [link](#groups-across-a-link) each entity counts once. Several totals over
the same columns share one coarser call, and the call stays one statement. The coarser call
selects everything the call does (a share or total as its input), so it has a row wherever the
call has one, and it settles its [empty groups](#empty-groups-null-or-0) as that call would on
its own: data of nothing reads `0`, a period not loaded yet `null`, and a group it has no row
for `null` (a stock over the period whose closing day has no snapshot). Rolled up across the
period, a window over earlier periods (`prior_period`, `cumulative`, ...) reads as its input
there. A name the coarser call keeps groups by its entity's key beside it, as any call's does,
so a total over a customer key that keeps the customer's name keeps namesakes apart; a key the
call groups by stays unless `over` names it, and the key the engine adds beside a name rolls up
with the name. A call that can't be answered without the `over` columns (a conversion read per
period) refuses the total with `REWRITE_NOT_SUPPORTED` at its path, naming the item.

```json
{"select": ["measure.jaffle.ordering_customer_count",
            {"expression": {"kind": "total", "input": "measure.jaffle.ordering_customer_count", "over": ["period"]}, "as": "store_customers"}],
 "group_by": ["dimension.jaffle_store_name"],
 "time": {"grain": "month", "start": "2017-01-01", "end": "2017-04-01"},
 "where": [{"field": "dimension.jaffle_store_name", "op": "is", "value": "Philadelphia"}]}
```

`store_customers` is the store's distinct customers over the whole window on each of its
monthly rows: 366, where adding the months up would count 1012, a returning customer once a
month. The answer states each total in `assumptions`:

```jsonc
{"rows": [{"dimension.jaffle_store_name": "Philadelphia", "period": "2017-01-01", "period_partial": false, "ordering_customer_count": 312, "store_customers": 366},
          {"dimension.jaffle_store_name": "Philadelphia", "period": "2017-02-01", "period_partial": false, "ordering_customer_count": 337, "store_customers": 366},
          {"dimension.jaffle_store_name": "Philadelphia", "period": "2017-03-01", "period_partial": false, "ordering_customer_count": 363, "store_customers": 366}],
 "assumptions": [{"code": "TOTAL_OVER", "column": "store_customers", "input": "measure.jaffle.ordering_customer_count", "over": ["period"],
                  "message": "store_customers is measure.jaffle.ordering_customer_count across period in the rows the call keeps, computed at that grain, not added up from the rows."}]}
```

The input is one measure or metric id, and each `over` item a `group_by` id of the call, or
`period` when `time` has a grain, or it is refused at its path; `over` may name every column, for
the grand total on every row. A total is a select item of its own: inside another expression it
is refused at its path (compute with its column after the call). A total of a distribution, or
beside one, is refused with `REWRITE_NOT_SUPPORTED`. The coarser call is held to the caller's
policies as a call of its own, for every select item it reads: a total that would read a
governed item across a grouping its policy requires is refused with `POLICY_DENIED`, and a
total beside withheld values is refused.

### Per-entity value filters

In a `distribution`, `entity_value.where` filters each entity's computed value. Each item takes
`op` and `value` (and optionally `kind: "value_filter"`); a `field` or any other key is refused
with its index. The value is a number, so `op` is `is`, `not`, a range word or `missing`; a text
word or a `LIKE` is refused at its path. Put dimension conditions in the call's `where`, so they
apply before the per-entity aggregation.

## The answer

```jsonc
{"status": "ok",
 "rows": [...],                                       // one column per group_by id, then period, period_partial, then select columns
 "window": {"start", "end", "now", "partial", "data_through", "data_through_source"},  // whenever a window or a grain series was resolved
 "clock": "temporal_role.<id>",                       // whenever a time-bearing measure was read
 "observation_scope": "dataset",                      // always
 "routes": [{"from", "to", "via"}],                   // only when a route was chosen among several
 "assumptions": [...],                                // only when the answer assumed something: a window total, a partial
                                                      // period, an entity filter's over, a statistic override, a governed
                                                      // metric, a share's denominator, a key beside a name,
                                                      // a series that keeps only its observed periods
 "warnings": [...]}                                   // only when there are some: NO_DATA_YET, UNKNOWN_ROWS_KEPT, TIES_AT_LIMIT, ...
```

- Columns: a `group_by` dimension keeps its full id; a measure or metric is its id's last
  segment; `as` wins; the time axis is `period` (an ISO date), followed by `period_partial`.
- `window` holds only the keys that apply: `start`/`end` as resolved (`last` becomes dates;
  `this` starts on a date and ends at `now`, stated with the clock zone's offset), `now` as
  used, `partial`, and `data_through` once loaded coverage has read the edge (DuckDB,
  MotherDuck, DuckLake and Postgres; on Snowflake, BigQuery, Databricks and Athena, for a named
  window's series). `data_through_source` says where a declared edge comes from: the load log
  as the package declares it (`export_runs.through_day`; `loaded` when the caller can't see
  that model), or `now`; it is left out when the edge is the last row.
- No field is empty: `routes`, `assumptions`, `warnings` and `errors` appear only when they
  have entries, and a list, object or string with nothing in it is left out (`rows` stays: an
  empty result is an answer).
- Each assumption is `{code, message, ...}`; any code the engine registers as an assumption
  is stated here, never among the warnings.
- Verbosity trims only after the answer is built: `window`, `clock`, `observation_scope`,
  `routes` (each `{from, to, via}`, with a call's `other_routes`), every assumption and every
  warning survive every verbosity, because they change what the numbers mean.
- Each warning and each error has a stable `code` and a `message`, and like the answer leaves
  out its empty fields (an empty `path`, no `details`, no hints), on every transport. Codes,
  `status` and `routes` are server-authored, so a host can record them per call without the
  query text, SQL or rows.
- The echoed `query` and `normalized_query` (compact and full) are the call, never the
  engine form with its identity or `now`.
- Value types are in [Result values](#result-values).

## Refusals

A refused call returns one error object with the path it failed at and one hint, the
corrected shape to send:

```json
{"select": ["measure.jaffle.session_starts"],
 "where": [{"field": "dimension.jaffle_session_converted_at", "op": "missing", "value": true}]}
```

```jsonc
// The same call written with "op": "IS NULL" is refused:
{"code": "INVALID_QUERY",
 "message": "where[0].op must be one of ['is', 'not', 'gt', 'gte', 'lt', 'lte', 'contains', 'starts_with', 'ends_with', 'not_contains', 'not_starts_with', 'not_ends_with', 'missing']; write {\"op\": \"missing\", \"value\": true}",
 "details": {"path": "where[0].op", "received_value": "IS NULL"},
 "recovery_hints": [{"code": "USE_FILTER_WORD", "message": "Write {\"op\": \"missing\", \"value\": true}."}]}
```

A clarification (`AMBIGUOUS_PATH`, `AMBIGUOUS_CHILD_SCOPE`, `MIXED_TIME_ROLES`,
`ENTITY_FILTER_PERIOD_UNSTATED`) is a refusal whose options are ready to send: the engine
never guesses which one the question meant.

A [row-grain model](PACKAGE_AUTHORING.md#row-grain-models) has no key, and every stored row
counts: its measures answer by its own, its parents' and their parents' columns, and a parent
can be filtered by its rows. What has to tell its rows apart (an entity filter or a per-entity
value on it, a segment, a conversion, a sum of an `additive: false` measure) is refused with
`ROW_GRAIN_UNSUPPORTED`, whose message says what works instead.

## Retired keys

A call written for the previous query shape is refused at the first retired key, with where it
went. Where the rewrite is mechanical the hint carries the caller's own values in the new shape:
each `metric_filters` item as its `having` row or entity filter, `time.range` as the `time` to
send, `route_decisions` as `routes`, a reference object as its id, `LIKE 'text%'` as
`starts_with`, `'%text'` as `ends_with` and `'%text%'` as `contains`, `BETWEEN` as `gte` and
`lte` (it includes both ends). An item with no mechanical rewrite (a predicate grouped by the
query's dimensions, say) gets the general hint. `semantic-rails project upgrade` rewrites a
package's example and test queries written in the previous shape, including both sides of a
comparison test ([rule `query-call-v2`](PACKAGE_AUTHORING.md#upgrading-a-package)). Refusal tests
receive a hand-edit hint naming the expected code to verify. Translations that can change an
answer, such as `LIKE` to a case-insensitive text word or `!=` to a `not` that keeps unknown
rows, contextual predicates that inherit query grouping and filters, start-only time
windows that would gain an upper bound at `now`, and windows with a grain that set
`fill: false` or carry a `limit` (a named window answers every period, and one with no data
answers 0), stop for a hand edit; the upgrade writes nothing until those sites are resolved. Rewritten calls are reported as
`translated`: the staged package loads and current calls compile with entry `now` and `context`,
but no data is executed and legacy answers are not compared. Review the diff.

| Retired | Now |
|---|---|
| `version` | ignored |
| `policy_context` | identity from credentials (`IDENTITY_CLAIM_REFUSED` over HTTP and remote MCP); `now` is an argument |
| `limits`, `verbosity`, `sql_profile`, `debug` | tool arguments (`max_rows`, `verbosity`, `sql_profile`); refused inside the call |
| `explain` | MCP `execute` with mode `sql` |
| `export` | removed |
| `request_id` | the `X-Request-Id` header (HTTP) or MCP `_meta` |
| `route_decisions` (`source_entity`, `target_entity`, `relationship_path`) | `routes` (`from`, `to`, `via`) |
| `temporal_role_overrides` | a package metric pinned with `temporal_role:` |
| `metric_filters` | `having` (a result threshold) or an entity filter's `having` (which entities count) |
| `time.range` (`{"last": ...}`) | `time.last`, or `start`/`end` |
| `time.calendar_id` | removed: the clock's own calendar applies |
| grain `""`, `hour`, `minute` | omit `grain` for one total |
| `time.fill` on a named window | removed: a named window returns every period |
| `order_by.field: "time"` or a clock id (`temporal_role.<id>__month`), `"ASC"`/`"DESC"` | `"period"`, `"asc"`/`"desc"` |
| `where[].op` `IS NULL`, `IS NOT NULL`, `IS DISTINCT FROM`, `BETWEEN` | `missing: true`, `missing: false`, `not`, two conditions |
| `LIKE`, `NOT LIKE`, `ILIKE` (in `where`, a kind's conditions or a `comparison`) | `starts_with` (`'X%'`), `ends_with` (`'%X'`), `contains` (`'%X%'`), or their `not_` forms; any other pattern: `is` with the exact values |
| child group `{child, match, where}` | entity filter `{entity, any \| none}` |
| `scope_mode`, `time_grain`, `time_alignment` on a predicate | `over` |
| `{measure: ...}` / `{metric: ...}` objects, `kind: metric`, `measure_ref` | the id string; `{kind: measure, ...}` to override |
| `kind: binary`; a conversion's `matching`; an `in`/`not_in` `left` | `kind: arithmetic`; `matching_mode`; `expr` |
| `prior_period` with `measure`, an integer `offset` and `grain` | `{kind: prior_period, input, offset: {unit, value}}` |
| `limit: 0`, `limit: null` | omit `limit` |

## Checking a call yourself

```python
import json, pathlib, jsonschema

schema = json.loads(pathlib.Path("schemas/query.v2.json").read_text())
for error in jsonschema.Draft202012Validator(schema).iter_errors(call):
    print(list(error.absolute_path), error.message)
```

`validate` (MCP `execute` with mode `validate`, `POST /api/v1/validate`, `Runtime.validate`,
`semantic-rails validate`) checks a call against the package and its policies without reading
the warehouse. It uses the same route/path resolution and SQL compilation as execution;
a missing path fails validation with the execution diagnostic. The fenced JSON calls in this
page and the other query docs are validated against the schema and the `jaffle_shop` package
by the test suite.

## Known limitations

The engine trusts the data as it is stored in three cases:

- **Orphan foreign keys.** A child row whose parent id is missing from the parent table counts
  in entity filters: the engine trusts the child's id.
- **Lossy joins.** Joining columns of different types or collations (a text id to an integer
  key, or a case-insensitive collation) can match several parent rows, so totals can double.
  Keep join columns the same type.
- **Many-valued groups.** A parent grouped by a value that only its child rows carry (orders by
  their items' product type: 55,974 and 18,261 of 59,652 orders) counts under each value its
  children carry, with its whole value ([groups across a one-to-many
  hop](#groups-across-a-one-to-many-hop)), so the totals across
  groups can exceed the whole, and a parent with no child rows is left out of that grouping.
  Combined labels ("Lab + Office") are a possible future option.

### Links and one-per-key models

- An entity counts under each of its groups, so groups across a link don't add up to the
  total.
- A one-per-key model's column with two values in one key reads NULL, so a sum of it leaves that
  key out.
- A model with no key that reaches a link answers distinct counts only; its sums and row counts
  are refused.
- On Postgres, a key with very many distinct values in a one-per-key column can fail the query
  (out of memory or array size) instead of reading NULL.

### Shares and totals under a required grouping

- In a call with an entity filter or `routes`, a share's denominator is held to the caller's
  policies only by the call's own check, so a share over a grouping a policy requires
  (`required_group_by`) can answer there, and its denominator is the total the policy refuses.
- A total in a call with `routes` is not checked on its own when the call without the `over`
  columns no longer walks the route.

### Text words

The text words (`contains`, `starts_with`, `ends_with` and their `not_` forms) give the same rows
on every warehouse by refusing what the warehouses lower differently:

- Search text holding one of these letters is refused, because some warehouse lowers it
  differently from the rest: Greek sigma (`Σ σ ς`, whose lower case depends on the next
  letter), sharp s (`ß ẞ`), dotted and dotless i (`İ ı`, and a combining dot above), the
  Kelvin, Ångström and Ohm signs, rare Latin letters with a stroke, hook or tail, the cased
  letters of U+2000 to U+2FFF (Roman numerals, circled letters, Glagolitic, Coptic, Georgian
  Nuskhuri and its capitals), and letters whose case pair is newer than Unicode 13. Match such
  values exactly with `is`.
- A stored value holding one of these letters is lowered as its warehouse lowers it, so text
  that lines up with that letter can match on one warehouse and not another (`is` against a
  stored `İs`, or `k` against a stored Kelvin sign).
- A letter that a later Unicode version gives a case pair can lower differently by warehouse
  until the engine adds it to the list.
- On Postgres the text words need ICU (the `und-x-icu` collation, built in wherever Postgres
  has ICU); without it the query fails rather than answering differently.
- On ClickHouse a `FixedString` value with a zero byte inside it, not just as trailing
  padding, is compared only up to that first zero byte.
- Text holding a NUL character or an unpaired surrogate is refused in the query call, but
  not yet in package YAML: an expression literal or `unknown_values` entry holding one can
  stop the package loading or make its queries fail.

## Known limitations

The engine trusts the data as it is stored in three cases:

- **Orphan foreign keys.** A child row whose parent id is missing from the parent table counts
  in entity filters: the engine trusts the child's id.
- **Lossy joins.** Joining columns of different types or collations (a text id to an integer
  key, or a case-insensitive collation) can match several parent rows, so totals can double.
  Keep join columns the same type.
- **Many-valued groups.** A parent grouped by a value that only its child rows carry (orders by
  their items' product type: 55,974 and 18,261 of 59,652 orders) counts under each value its
  children carry, with its whole value ([groups across a one-to-many
  hop](#groups-across-a-one-to-many-hop)), so the totals across
  groups can exceed the whole, and a parent with no child rows is left out of that grouping.
  Combined labels ("Lab + Office") are a possible future option.

### Text words

The text words (`contains`, `starts_with`, `ends_with` and their `not_` forms) give the same rows
on every warehouse by refusing what the warehouses lower differently:

- Search text holding one of these letters is refused, because some warehouse lowers it
  differently from the rest: Greek sigma (`Σ σ ς`, whose lower case depends on the next
  letter), sharp s (`ß ẞ`), dotted and dotless i (`İ ı`, and a combining dot above), the
  Kelvin, Ångström and Ohm signs, rare Latin letters with a stroke, hook or tail, the cased
  letters of U+2000 to U+2FFF (Roman numerals, circled letters, Glagolitic, Coptic, Georgian
  Nuskhuri and its capitals), and letters whose case pair is newer than Unicode 13. Match such
  values exactly with `is`.
- A stored value holding one of these letters is lowered as its warehouse lowers it, so text
  that lines up with that letter can match on one warehouse and not another (`is` against a
  stored `İs`, or `k` against a stored Kelvin sign).
- A letter that a later Unicode version gives a case pair can lower differently by warehouse
  until the engine adds it to the list.
- On Postgres the text words need ICU (the `und-x-icu` collation, built in wherever Postgres
  has ICU); without it the query fails rather than answering differently.
- On ClickHouse a `FixedString` value with a zero byte inside it, not just as trailing
  padding, is compared only up to that first zero byte.
- Text holding a NUL character or an unpaired surrogate is refused in the query call, but
  not yet in package YAML: an expression literal or `unknown_values` entry holding one can
  stop the package loading or make its queries fail.

---

# Engine rules in depth

The sections below hold the detailed rules behind the fields above.

## Empty groups: NULL or 0

Sometimes there is no data (NULL), and sometimes there is data of nothing (0). A group or
period with no rows reads one or the other, by one rule, in every call:

| Measure | Empty, observed in scope, inside the data | Empty, after the data's edge | Nothing in scope |
|---|---|---|---|
| `sum`, `count`, `count_distinct` over an additive, event-count or entity-count measure | `0` | `NULL` + `NO_DATA_YET` | `NULL` + `NO_DATA_IN_SCOPE` |
| a balance (`snapshot: end_of_period`) grouped only by time | the closing day's value; `0` if snapshots exist and none pass the filters | `NULL` + `NO_DATA_YET` | `NULL` |
| `avg`, `min`, `max`, `median`, `percentile`, distinct populations, `additive: false` | `NULL` | `NULL` | `NULL` |
| a metric (ratio, arithmetic) | each operand settles by its own rule, then the formula; ÷0 → `NULL`; a `NULL` operand makes the result `NULL` | | |

A measure has data in scope when at least one of its rows in scope holds a value: a sum with a
non-NULL amount, or a count above zero. The scope is the measure's own authored conditions (its
`filter`, an `aggregate_if` condition or a `CASE`) and policy row filters, before the
`group_by`; [`observation_scope`](#observation_scope) says whether the call's `where` counts:

- **`dataset`**: it doesn't. "Apples at store 5" reads `0` when store 5 sold no apples, as store
  5 does in a by-store answer. An entity filter is part of the `where`: a period in which it
  keeps no entity reads `0` inside the loaded data. A string `is` value that matches no row of
  its dimension (under the caller's row filters) adds `FILTER_VALUE_NOT_FOUND`, naming the value
  and the closest one, so a misspelled value isn't read as a confident 0; when the check cannot
  run, `FILTER_VALUE_UNVERIFIED` names what was not verified.
- **`query`**: it does. A measure with no value inside the call's filters reads `NULL` in every
  group, with `NO_DATA_IN_SCOPE`.

A `dataset` call with a `where` beside an entity filter on aggregates can't judge one apart
from the other, so it is refused with `EMPTY_GROUPS_UNSETTLED`; send
`observation_scope: "query"`.

**All NULL reads NULL.** A group whose rows exist but whose amounts are all NULL is not empty:
its sum reads `NULL` (as SQL's `SUM` does) and its count still counts the rows. A conditional
sum (`aggregate_if`, or an aggregate with a `filter`) reads only the rows that meet its
condition: a group whose rows all fail it reads `0`, one whose matching rows all have a NULL
amount reads `NULL`. A sum of a `CASE` with no `ELSE` follows that rule; an explicit non-NULL
`ELSE` (including `ELSE 0`) contributes on every row.

**Metrics.** Arithmetic settles each operand first, then combines them: an operand that is
unknown or has no data in scope is `NULL`, and so is the result (`revenue - refunds` is `NULL`
when the refunds relation has no rows in scope). A never-matched conditional operand reads `0`
before arithmetic where its source rows settle it. Division by zero is `NULL`; a rate with no
trials reads `NULL`, never `0.0`. A metric-level `COALESCE(..., 0)` neither adds nor removes a
row.

**Entity filters.** An entity filter's aggregate applies the rule to every entity alike: an
operand reads `0` for an entity with no match where its measure has data somewhere in the
filter's scope, and `NULL` where it has none. So `large_order_count is 0` ("customers with no
large orders") keeps every customer without one when some order in scope is large, and keeps
nobody when none is. `NULL` fails every threshold, `is 0`, `lt 1` and `not 0` included. Only a count or
sum threshold that 0 passes reaches an entity with no rows at all.

**The data's edge.** For a series or a window total on DuckDB, MotherDuck, DuckLake and
Postgres, and for a named window's series on Snowflake, BigQuery, Databricks and Athena, an
empty period inside the base relation's loaded range reads `0`; one before its first loaded
row or after its last row at or before `now` reads `NULL`. Coverage reads the
whole base relation under policy row filters, ignoring measure and call filters, and is
reported as `window.data_through`. Populated sums and positive counts always survive, including
future-dated rows. `NO_DATA_YET` names each affected output and measure with its `edge`, the
date of the measure's last row, and `edge_source` `last_row`: nothing says the data is loaded
past that row. A table whose package declares its edge (`zero_through`, see
["Empty periods"](PACKAGE_AUTHORING.md#empty-periods)) reads `0` in an empty period only when
the period ends by that edge (or, after a load that reached the start of today, starts before
it): a load log's loaded-through date (`edge_source` `loaded`; `edge`
is null and the message says "loaded-through date unavailable" when the log holds none) or
`now` (`edge_source` `now`, with the note `ZERO_AFTER_LAST_ROW` when the answer reaches past the
last row). Otherwise, on Snowflake, BigQuery, Databricks, Athena and ClickHouse, an
empty period reads `0` only while the measure has data inside the window. Those warehouses
read the stored timestamps as naive values in the clock's `column_timezone` (UTC by default);
see "`times:` — temporal roles" in [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md).

**Warnings.** An output that is a sum, count or distinct count (or a sum or difference of them)
reading `NULL` on every returned row carries one `NO_DATA_IN_SCOPE` naming those outputs. A
`prior_period`, ratio or rolling output never does, since it can be `NULL` while its measure
has data, and neither does a truncated answer. An ungrouped distinct-population count over
nothing reads `0` with no warning, a known limitation
([issue #203](https://github.com/semantic-rails/semantic-rails/issues/203)).

ClickHouse fills an unmatched outer-join field with a type default unless the join yields
NULLs, so every ClickHouse statement ends with `SETTINGS join_use_nulls = 1`.

## Time windows in depth

**Bounds** are half-open: `start` inclusive, `end` exclusive. "Calendar year 2017" is
`start: "2017-01-01", end: "2018-01-01"`; an `end` of `"2017-12-31"` would leave out December
31. Adjacent windows compose without overlap or gaps. A clock declared `kind: date` applies the
whole-day rule: `start` includes its local day, and an exclusive `end` after midnight includes
its local day.

**Time zones.** `last` and `this` resolve against `now` in the clock's zone. On DuckDB,
MotherDuck, DuckLake and Postgres the query runs with the session time zone set to the clock's
zone, so zone-aware columns bucket correctly too; for other warehouses, see "`times:` —
temporal roles" in [PACKAGE_AUTHORING.md](PACKAGE_AUTHORING.md). A role that converts its
column (`column_timezone` different from `timezone`) is converted before its bounds and
buckets; shapes that would read the stored values instead (an entity filter `over: window`, a
conversion metric on that clock) are refused with `WINDOWED_TIME_FILTER_UNSUPPORTED`, naming
the shape in `details.path`. A clock whose zone is not a known zone refuses relative windows.

**Windows that end at now.** `this` and a `start`-only window end at `now` itself. On DuckDB,
MotherDuck, DuckLake and Postgres a zone-aware column is cut at `now`'s instant and a plain
timestamp at `now`'s wall time in the clock's zone. Snowflake, BigQuery, ClickHouse,
Databricks and Athena don't run the query in the clock's zone, so they can't tell the two
apart: on a clock in a zone other than UTC that declares no `column_timezone`, such a window is
refused (`INVALID_QUERY` at `time.this` or `time.start`, with the hint to declare
`column_timezone` or send a dated window). Dated windows (`last`, `start` with `end`) answer
there as usual.

**Fill and the calendar.** The engine generates a Gregorian day spine in SQL, bucketed like
the `period` column (calendar months, quarters and years; Monday weeks) in the clock's zone. A
named window's spine spans the window; an open window with `fill: true` spans the answer's first
to last period. An authored `default` calendar takes no part in bucketing; its columns stay
usable as ordinary dimensions. The spine is not available on ClickHouse, so `fill`, `rolling`
and `prior_period` refuse there; on Athena a series is capped at 10,000 days.

## Windows that read earlier periods

`prior_period`, `rolling`, `period_to_date` and `cumulative` read periods before the ones they
return, so a cut from below would silently change them. With one in `select` or `having`, a
`time.start` is refused, and so is a `where` condition (inside an entity filter too) on any
temporal or calendar dimension, whether or not it is the clock. Only an upper bound (`lt`,
`lte`) on a date or timestamp dimension runs, as `time.end` does. The refusal is
`WINDOWED_TIME_FILTER_UNSUPPORTED` or `CUMULATIVE_TIME_FILTER_UNSUPPORTED`; for a `where`
condition, `details.where_path` names it. A dimension is temporal when it is a time role, has a
`date`, `timestamp`, `datetime` or `time` kind, or shares such a column's name; a calendar
dimension is any dimension of a `kind: time` entity.

The same rule applies to conditions bound to the window's own inputs, including conditions a
package metric authors (`details.filter_source: "measure"`), and to a row policy that keeps a
single value of a temporal column (`details.filter_source: "policy"`). These refusals offer no
patch that removes a policy or an authored filter; query an unwindowed measure or ask the
package author for a supported metric.

A call runs the periods it asks for, including one still in progress. `plan` calls a period
comparison ready only when `time.end` closes its last bucket no later than `now`; otherwise it
holds the draft with `PERIOD_COMPARISON_INCOMPLETE` (see [MCP_INTERFACE.md](MCP_INTERFACE.md)),
so a month so far never reads as a change from the whole previous month.

Summing windows (`rolling`, `cumulative`, `period_to_date`) need values that add up across
periods: additive flows (`sum`, `count`), event counts (`count_distinct` of a complete row key),
and sums, differences and literal multiples of them. A ratio windows its parts:
`SUM(numerator) OVER w / NULLIF(SUM(denominator) OVER w, 0)`. Inputs using `avg`, `min`,
`max`, `median` or `percentile`, balances, distinct populations, distributions, products of
measures, nested windows and ratios inside other arithmetic are refused with `ROLLUP_UNSAFE`
before SQL runs, through derived metrics too.

## Child entities in depth

- `any` keeps a row of the measure's entity when at least one child row meets every condition
  (a correlated `EXISTS`); `none` keeps it when no child row does (`NOT EXISTS`). A lookup that
  finds no row reads NULL, and NULL fails a comparison: under `none`, a child row with a NULL
  value never excludes its parent.
- Several entity filters are separate subqueries, ANDed with the other `where` items. Two
  filters on one child mean possibly different child rows; one filter with both conditions
  means the same row.
- One child scope per call. Beside an `any`/`none` filter nothing else may cross a one-to-many
  hop (a plain condition, a grouping by a child dimension, or a measure's own filter); filters
  on different children, or one child by different routes, are refused too. Each is
  `MIXED_GRAIN_INVALID`.
- The child must sit across a one-to-many hop from each measure's entity; its route follows
  [the route rule](PACKAGE_AUTHORING.md#the-route-rule) (several routes and no recorded
  preference → `AMBIGUOUS_PATH`). A child reached only through lookups, or the measure's own
  entity, is refused with `INVALID_QUERY` (use a plain condition). Under a row policy the call
  is refused with `POLICY_DENIED`; under restricted metric grants, with
  `RESOURCE_ACCESS_DENIED`.
- `AMBIGUOUS_CHILD_SCOPE` is raised only when a call's only conditions across a one-to-many hop
  are plain `where` conditions on one child entity by one route. Two or more positive ones offer
  `same_row` (one `any` of all) or `separate_rows` (an `any` each); exactly one `not` or
  `missing` offers `any_not` (an `any` with the condition as written) or `none` (a `none` with its
  complement). A `not` beside another condition on the child, or conditions on different
  children, are `MIXED_GRAIN_INVALID` with no clarification. An option is offered only when it
  answers for this caller; otherwise the call is `MIXED_GRAIN_INVALID` naming the option.
- ClickHouse answers one `any` filter on the child's own columns; a `none` filter, several
  filters, or a lookup from the child are refused with `MIXED_GRAIN_INVALID`.

A lookup dimension (many-to-one) is NULL on a row whose lookup found no match: `missing: true`
keeps it (an anti-join), `is` drops it and `not` keeps it, wherever the dimension is read.
ClickHouse keeps lookups as inner joins, so it drops such a row from every call that reads the
looked-up dimension.

## Scalar `call` expressions

`{"kind": "call", "name": "ROUND", "args": [<expression>, {"kind": "literal", "value": 1}]}`
applies a scalar function. Names are case-insensitive. Aggregate, window and table functions
use their semantic kinds instead. An unsupported name returns `INVALID_EXPRESSION_AST` with
`details.allowed` listing the warehouse's accepted set.

Common names: `ABS`, `CAST`, `CEIL`, `CEILING`, `COALESCE`, `CONCAT`, `DATE_DIFF`, `EXP`,
`FLOOR`, `LENGTH`, `LN`, `LOG`, `LOWER`, `NULLIF`, `POWER`, `REPLACE`, `ROUND`, `SQRT`,
`SUBSTR`, `SUBSTRING`, `TRIM`, `UPPER`.

| Warehouse | Additions or exceptions |
| --- | --- |
| DuckDB, MotherDuck, DuckLake | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `JSON_EXTRACT`, `JSON_EXTRACT_STRING`, `SPLIT`, `STRING_SPLIT`, `STR_SPLIT` |
| Postgres | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT` |
| Snowflake | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `SPLIT` |
| BigQuery | `LEFT`, `RIGHT`, `JSON_EXTRACT`, `SPLIT` |
| Databricks | `DATE_PART`, `DATE_TRUNC`, `LEFT`, `RIGHT`, `SPLIT` |
| Athena | `DATE_TRUNC`, `JSON_EXTRACT`, `SPLIT` |
| ClickHouse | No additions; `TRIM` is excluded |

`DATE_DIFF` takes exactly three arguments: a string literal unit (`minute`, `hour`, `day`,
`week`, `month`, `quarter`, `year`), then start and end. It counts calendar unit boundaries
(end minus start); `week` is the day difference divided by seven, truncated. Snowflake,
BigQuery and ClickHouse refuse `week`; Athena refuses every unit. A NULL endpoint gives NULL.

`CAST` takes exactly two arguments: the value and a string literal type, `DOUBLE`,
`DECIMAL(p,s)` (precision 1–38), `INTEGER`, `BIGINT` or `VARCHAR`. BigQuery refuses `DECIMAL`
targets; ClickHouse renders nullable targets. Invalid conversions fail execution; `CAST` never
silently returns NULL. Argument types are checked by the warehouse at execution.

A `comparison` with a literal `null` lowers `=` to `IS NULL` and `!=`/`<>` to `IS NOT NULL`;
ordering comparisons against `null` are refused (`USE_NULL_TEST_OR_SCALAR`).
`and`/`or` need at least two arguments. A `metric_predicate` whose `value` is null is refused
with `INVALID_METRIC_PREDICATE`: its input reads `0` for an entity with no rows, so a count of
none is `is 0`. A select of literals only, with no `group_by`, `where`, `time` or `having`, is
refused (`details.reason: "literal_only_select"`).

## Result values

HTTP responses, MCP `execute`, `Runtime.query` and CLI JSON output share one result-value
policy; segment previews use it too. `column_types` maps each column to its observed logical
type and survives every verbosity and MCP row format. It is separate from `output_columns`,
which describes semantic lineage and authored types.

| Source value | JSON value | `column_types` |
|---|---|---|
| Decimal | canonical decimal string, without redundant fractional zeros | `{"type":"decimal"}` |
| Integer | integer JSON number, exact beyond binary64's integer range | `{"type":"integer"}` |
| Float/double | finite JSON number | `{"type":"float"}` |
| Aware timestamp | ISO 8601 string in the query's time zone | `{"type":"timestamp","timezone":"aware"}` |
| Naive timestamp | ISO 8601 string with `T` and no offset | `{"type":"timestamp","timezone":"naive"}` |
| Date, and `period` at a day or coarser grain | `YYYY-MM-DD` | `{"type":"date"}` |
| Time | ISO 8601 string | `{"type":"time","timezone":"aware"}` or `"naive"` |
| Interval | signed ISO 8601 duration, exact microseconds | `{"type":"interval"}` |
| SQL NULL | `null` | does not replace a column's non-null type |
| Text / boolean | string / boolean | `{"type":"string"}` / `{"type":"boolean"}` |
| Binary | base64 string | `{"type":"binary","encoding":"base64"}` |
| UUID | lowercase hyphenated string | `{"type":"uuid"}` |
| JSON array / object | JSON-native | `{"type":"array"}` / `{"type":"object"}` |

A column's JSON type follows the driver's result type, never the authored type: DECIMAL and
NUMERIC become canonical strings (`Decimal("0.10")` → `"0.1"`), FLOAT, DOUBLE and INTEGER
become numbers. The one exception comes from the query itself: a count is an integer even
where the driver widens it to a decimal, as Postgres does when it sums bigint counts (an
aggregate table's counts, a running total) and Snowflake's high-precision Arrow results do for
every `NUMBER`. A count is a `count` or `count_distinct`, a sum of a whole-number literal (a row
count, `expr: 1`, or a CASE of whole numbers), or a sum, difference, product, running total,
period-to-date or prior value of counts, directly or through a metric. The engine decides it from
the query, never from `value_type`: a sum of a stored count column (`lifetime_order_count`, a
snapshot's `seats`) keeps the driver's type, decimal there. A count the warehouse returns with a
fraction keeps its decimal string. A count beyond 2^53 is an exact JSON integer, which a client
that reads JSON numbers as doubles (JavaScript's `JSON.parse`) rounds; no realistic count is
that large. Mixed numeric values take Decimal over float over integer for the whole column;
an integer that a float cannot hold exactly refuses with `RESULT_VALUE_UNSUPPORTED` rather than
rounding. Numeric-looking text stays text. The same aggregate can have a different type per
warehouse (`AVG` is DOUBLE on DuckDB and NUMERIC on Postgres); `column_types` reports what the
driver returned. An all-null column has type `null`; an empty result has `column_types: {}`.

The Postgres adapter accepts only Arrow scalar types with exact mappings (integers, decimals,
floats, text, booleans, dates, microsecond times and timestamps, binary, intervals, NULL); other
types refuse with `RESULT_TYPE_UNSUPPORTED` before rows are read, naming the column and type
without exposing values. Unsupported values, non-finite numbers and conflicting non-null column
types refuse with `RESULT_VALUE_UNSUPPORTED`; raw values never appear in the error.
