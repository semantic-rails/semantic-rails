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
- **Breaking:** answers that change. Each move below is checked against independent
  reference SQL; nothing was set to whatever the engine returns.
  - A named window (`last`, `this`, or `start` with `end`) answers every period, on every
    warehouse but ClickHouse. An empty period inside the loaded data reads 0, one after the last
    loaded row reads `NULL` with `NO_DATA_YET` and `window.data_through`, and one before the
    first row reads `NULL`; before, such periods were missing or the answer was empty. A month
    of days now returns all its days (31 rows where 2 came back), and a month with no orders
    inside the data reads 0 instead of leaving a gap.
  - `this` and a window with only `start` end at the request's `now`, to the microsecond: a row
    stamped after `now` no longer counts ("this month" with a later-dated row: 3 → 2).
  - `not` (and `!=`, `NOT IN`) keeps rows whose value is unknown and counts them in
    `UNKNOWN_ROWS_KEPT`; add `{"op": "missing", "value": false}` to leave them out. "Signups
    excluding web" now includes the signups with no channel, and a `not` through a lookup keeps
    the rows the lookup can't match (a "not operating" count through a lookup: 1 → 10). A dimension may declare
    `unknown_values` that read as unknown.
  - A `LIKE` filter is refused with the text word its pattern says (`'%x%'` is `contains`, `'x%'`
    `starts_with`, `'%x'` `ends_with`). The text words ignore case where `LIKE` matched it on
    most warehouses, so a call moved to the word can count more rows: a search for `"shop"`
    also matches `"Shop"`.
  - A balance on an `as_of_time` clock reads each period's closing day (its last day with a
    snapshot; with no `time`, the last day with a snapshot at all) and no longer carries an
    earlier snapshot forward (a weekly stock 1,122 → 1,022); a window total over a balance
    reads the window's closing day instead of adding days (the bundled `jaffle_shop` inventory
    for March and April 2017: 1,945 → 860). The answer names the day (`BALANCE_CLOSING_DAY` in
    `assumptions`) and warns with `BALANCE_SERIES_MISSING_ON_CLOSING_DAY`, with their count,
    when series seen earlier in a period had no snapshot that day. The closing day is the day
    bucket `time.grain: day` puts a snapshot in; a row with no clock is never a snapshot; rows
    tied on a series' last clock value are all read; a balance read inside an entity filter
    or metric predicate reads and discloses the same way. A share of such a balance filtered
    by entity sets is refused (`REWRITE_NOT_SUPPORTED`), and so, on DuckDB and Postgres, is
    a balance on a timestamp clock in another zone than the call runs in
    (`MIXED_TIME_ROLES`). A stock on an event- or state-time clock is still read as keyed,
    each series' last row in the period.
  - An entity filter's unknown aggregate fails every threshold, `not` included (before, `not 0`
    kept entities whose aggregate was unknown). Under `observation_scope: "dataset"` a period
    in which the filter keeps no entity reads 0 inside the data (before `NULL`); under
    `"query"` it stays `NULL` with `NO_DATA_IN_SCOPE`. A group whose value is unknown (no
    history version at the row's time) is kept beside an entity filter on aggregates; before, it
    was dropped.
  - Grouping by an entity's display name keys the rows by the entity, so namesakes are two rows
    (`GROUPED_BY_KEY`), and a share over that name spans every namesake (1 and 1 → 0.5 and 0.5).
  - A window with no grain is one total: a `group_by`-only call lists each group once (3 rows,
    one per raw timestamp → 2), and a running total there is refused with
    `WINDOW_TOTAL_UNSUPPORTED` and the grain to add.
  - The time column is `period`, an ISO date; the numbers in it don't change.
- **Breaking:** `plan` answers more and guesses less. It reads relative phrases against the
  request's `now` (before, the wall clock), answers exclusions with `not`, and drafts the
  governed metric a question names, so those questions move from `low_confidence` to `ok`.
  Two periods side by side return `needs_clarification`
  with `PERIOD_COMPARISON` and a call that reads them as one series, and a comparison whose
  period isn't over is held (`PERIOD_COMPARISON_INCOMPLETE`). Fiscal periods are held, never
  drafted on Gregorian quarters. A question asking "how many distinct orders" counts orders, not
  customers. An entity filter with no stated period asks `ENTITY_FILTER_PERIOD_UNSTATED`.
- A metric constraint's allowed filters check everything the caller adds: `where`, entity
  filters and the conditions inside inline expressions. A package metric's own `filter:` is its
  governed definition: it passes `allowed_where` and the cut allowlists
  (`allow_metric_filters`, `allowed_metric_filter_*`), while the caller's `having`, entity
  filters or inline copies of it are still refused. To keep a caller from a metric, deny the
  metric. A raw measure that a metric hidden from the caller filters is refused in every read
  (`POLICY_DENIED`, naming nothing hidden), and `plan` holds it; a caller who sees every metric
  gets the raw number as before.
