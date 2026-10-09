- **Breaking:** the query call has one new shape, and the previous one is refused. A call has
  nine fields: `select`, `group_by`, `where`, `having`, `time`, `order_by`, `limit`, `routes`
  and `observation_scope`. `schemas/query.v2.json` replaces `query_ir.v1.json`; it is generated
  from the parser's own tables, and MCP `execute` embeds it. `version` is ignored. A call in the
  previous shape is refused at its first retired key, with the path and the shape to send. See
  [The query call](docs/QUERY_CALL.md) and its table of
  [retired keys](docs/QUERY_CALL.md#retired-keys).
- **Breaking:** a reference is the id string itself (`"metric.sales.aov_usd"`); a select item
  is an id or `{expression, as}`. `{measure: ...}` and `{metric: ...}` objects, `kind: metric`,
  `measure_ref` and the `prior_period` shorthand are refused; `{kind: measure, measure,
  aggregation}` overrides a measure's statistic.
- **Breaking:** filters are `{field, op, value}` with the words `is`, `not`, `gt`, `gte`, `lt`,
  `lte`, `contains` and `missing`. SQL spellings (`=`, `IN`, `!=`, `NOT IN`, `>`, ...) are read
  as their word, with its meaning. `IS NULL`, `IS NOT NULL`, `LIKE`, `IS DISTINCT FROM` and
  `BETWEEN` are refused with the word form; a `null` value or an empty list is refused with
  the `missing` form. `contains` matches literal text, ignoring case. Package `filter:`
  conditions read the same words.
- **Breaking:** `metric_filters` is gone. A result threshold is `having` (on a select alias or
  a measure or metric id). Which entities count is an entity filter in `where`:
  `{entity, any | none}` replaces the child group `{child, match, where}`, and
  `{entity, having, over}` replaces a metric predicate's `scope_mode`, `time_grain` and
  `time_alignment`. `over` (`all_time`, `window` or `period`) is required; a call without it is
  refused with `ENTITY_FILTER_PERIOD_UNSTATED`, and the answer states it in `assumptions`.
  `route_decisions` is now `routes: [{from, to, via}]`, and each `AMBIGUOUS_PATH` option carries
  the call to send. `temporal_role_overrides` is removed: pin a clock in a package metric.
- **Breaking:** `time` takes `last: {unit, value}`, `this: <unit>`, or `start`/`end`, plus
  `grain`, `temporal_role` and `fill`. `range` (use `last`), `calendar_id` and the grains `""`,
  `hour` and `minute` are refused, and so is `time` with only a clock. Measures on different
  default clocks are refused with `MIXED_TIME_ROLES`, one option per clock. `order_by` names
  `period` for the time axis and takes `asc`/`desc`; `limit` is a positive integer.
- **Breaking:** `now` and identity left the call. `now` is an argument: MCP `now` on `execute`
  and `plan`, REST `options.now`, CLI `--now`, `Runtime.query(..., now=)`, and `now:` on a
  package example or test or at the top of its file. Identity comes only from credentials:
  `policy_context` in a body is refused, with `IDENTITY_CLAIM_REFUSED` over HTTP and remote
  MCP; local callers pass `context`. `verbosity`, `sql_profile`, `limits`, `debug` and
  `request_id` are arguments beside the call, `explain` is `execute` mode `sql`, and `export`
  is removed. A REST body is `{"query": <call>, "options": {...}}`.
- **Breaking:** the answer says what it used. The time axis is the `period` column, an ISO
  date (`2017-08-28`, not a timestamp), and each series row carries `period_partial`. Every
  answer that resolved a window or series carries `window` (`start`, `end`, `now`, `partial`,
  `data_through`), plus `clock` and `observation_scope`, at every verbosity. A `this` window
  states how much of the period it covers (`PARTIAL_PERIOD`). Responses echo the call they
  answered in the call's own shape.
- Numbers that change: a named window (`last`, `this`, or `start` with `end`) answers every
  period. An empty period inside the data reads 0, one after the last loaded row reads `NULL`
  with `NO_DATA_YET`, and one before the first row reads `NULL`; before, such periods were
  missing or the answer was empty. `not` (and `!=`, `NOT IN`) now keeps rows whose value is
  unknown and counts them in `UNKNOWN_ROWS_KEPT`; add `{"op": "missing", "value": false}` to
  leave them out. A dimension may declare `unknown_values` that read as unknown. A balance reads
  each period's closing day (its last day with a snapshot) and no longer carries an earlier
  snapshot forward; a window total over a balance reads the window's closing day instead of
  adding days.
- A metric constraint's allowed filters now check everything the caller adds: `where`, entity
  filters and the conditions inside inline expressions. A package metric's own `filter:` is
  its governed definition and is not checked against them; to keep a caller from a metric, deny
  the metric.
