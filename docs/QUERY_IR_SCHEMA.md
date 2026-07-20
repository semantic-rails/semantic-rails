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
| `path_policy` | `object` | Path resolution preferences. |
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
| Aggregate-if | `{ "kind": "aggregate_if", "aggregation": "count", "condition": {...} }` or with `"value": {...}` for sum/avg/min/max. Compiles to `COUNT_IF` / `SUM_IF` on Snowflake, portable `<AGG>(CASE WHEN cond THEN value END)` elsewhere. Column refs inside `condition` / `value` must specify `entity` or `table` (no surrounding measure to inherit from). |
| Between | `{ "kind": "between", "expr": {...}, "low": {...}, "high": {...} }` — sugar for `expr >= low AND expr <= high`. Use `kind: "not_between"` or `negated: true` for the inverted form (`expr < low OR expr > high`). Desugared at parse time; the kind does not appear in the lowered IR. |
| Literal | `{ "kind": "literal", "value": 0 }` |
| Prior period | `{ "kind": "prior_period", "input": {...}, "offset": {"unit": "month", "value": 1} }` |
| Rolling | `{ "kind": "rolling", "input": {...}, "window": {"unit": "day", "value": 28} }` |
| Cumulative | `{ "kind": "cumulative", "input": {...} }` |
| Period-to-date | `{ "kind": "period_to_date", "input": {...}, "period": "month" }` |
| Conversion | `{ "kind": "conversion", "base": {...}, "converted": {...}, "entity": "...", "window": {"unit": "day", "value": 7}, "matching_mode": "first_converted_after_base" }` |

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
- `IN` / `NOT IN` take a list of scalars. A bare scalar is accepted and
  treated as a one-element list (strings are never character-split). An
  empty list compiles to constant `FALSE` (`IN`) / `TRUE` (`NOT IN`)
  instead of erroring. `value: null` with `IN` / `NOT IN` is rejected
  with `INVALID_QUERY` + a `USE_LIST_VALUE_OR_NULL_TEST` recovery hint.
- `IS NULL` / `IS NOT NULL` ignore `value` entirely — omit it.
- `value: null` with `=` (or `IS`) lowers to `field IS NULL`; with
  `!=` / `<>` / `IS NOT` it lowers to `field IS NOT NULL`. Ordering
  (`<`, `<=`, `>`, `>=`) and LIKE ops against `null` are rejected with a
  structured `INVALID_QUERY` and a recovery hint, since they would be
  always-UNKNOWN in SQL three-valued logic.
- Objects are rejected — inline expression thresholds belong in
  `metric_filters` (`metric_predicate`).

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
window appears, so rows missing in the source are filled with zero
before the LAG runs. The shift can therefore reach into months that
have no orders without producing NULL gaps (it produces NULL only
when the LAG reaches before the available data window).

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
the output with the measure column set to `0` (or `NULL` for ratios
and other null-preserving expressions). `fill: true` requires a
`grain` — the calendar spine needs a step size — and it is engaged
automatically by features that depend on dense rows (for example,
the inline `prior_period` LAG window in the "Period shifts" section
above).

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
zeros for the gaps (`end` is exclusive, so `2018-01-01` covers
through December 2017 without touching 2018):

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
