# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

Pending changes live as fragments in [`changelog.d/`](changelog.d/) until the next release.

## 0.3.2rc3 — 2026-10-03 — Governed joins, faithful plans and exact results

**Pre-release.** Install it with `pip install semantic-rails==0.3.2rc3`.

**Upgrading from 0.3.2rc2** (from earlier versions, read the preceding release notes below
first): review the entries below, especially these changes to packages and embedding hosts.

- **Remove retired declarations.** Delete `null_behavior` from metrics and expressions,
  `subject_entity` and `aggregation_entity` from measures and their defaults, forward rollup
  hints, relationship `path_preference` weights, and the query key `path_policy`, including
  in segment membership queries. Unsupported declarations now refuse loading or validation.
  Reverse population-count permissions remain under `graph.relationships` with
  `rollup_safe.reverse`; model joins and relationship defaults cannot declare `rollup_safe`.
- **Record ambiguous join routes.** Routes are no longer chosen by hop count or weight.
  Resolve `AMBIGUOUS_PATH` with a package `graph.path_preferences` row or the query's
  `route_decisions`, following the clarification options. A lookup into temporal history
  needs query time to select the valid version.
- **Check empty-group answers.** Additive sums and counts read `0` when their measure has
  data and `NULL` when it has none. Observation now defaults to the dataset after authored
  conditions and policy row filters; use `observation_scope: "query"` to observe within the
  query's filters, including when `EMPTY_GROUPS_UNSETTLED` asks for it. String filter values
  that match no rows carry `FILTER_VALUE_NOT_FOUND`; failed checks carry
  `FILTER_VALUE_UNVERIFIED`.
- **Check stocks and windows.** Key snapshot stocks by their series and snapshot clock;
  malformed as-of stocks now refuse instead of summing snapshots. Declare `additive: false`
  for pre-counted values that must never be summed, and follow the window and clock-alignment
  refusals described below.
- **Read canonical response fields.** A plan's draft lives in `best.query_ir`. Full errors
  live in `errors`, with recovery hints on each issue; the MCP `error` field carries only the
  first issue's code and message. Empty optional fields and duplicate payloads are omitted.
  Natural-language drafts that drop a requested grouping, filter or time now remain held;
  check readiness before executing them.
- **Postgres hosts:** the `postgres` extra now uses ADBC and PyArrow instead of psycopg,
  preserving exact typed results. Review the connection, result-type and bounded-wait
  entries below. Snowflake ADBC remains an opt-in connector experiment.

### Added

- An `aggregate_if` whose condition reads an entity that its value's entity reaches over
  declared many-to-one relationships now compiles instead of returning
  `UNSUPPORTED_CONDITIONAL_AGGREGATE`: order revenue where the order's customer is in a
  segment, or item quantity where the item's order belongs to a customer in a region (two
  hops). It aggregates each value row once, on the route a `where` filter on that entity
  takes. A value row with no match there never satisfies the condition: a condition such a
  row could satisfy (`IS NULL` on that entity, or an `or` with the value's own column) is
  refused with the same code, as is a condition across a one-to-many, many-to-many, bridge
  or time-valid hop, over two routes with no path preference, or in a count with no value
  column whose condition reads several entities. The error names the entities and the
  failing hop or condition, with a hint. Object policies on the dimensions over the
  columns it reads refuse it as they refuse a `where` filter on them; while any
  `object_access` or `object_visibility` policy is declared, reading a column of another
  entity that no dimension declares is refused with `POLICY_DENIED`.
- Add numeric and text CAST expressions with dialect-specific SQL types and
  NULL preservation. Scalar calls now advertise the accepted warehouse functions
  and explain how to fix literal-only selects before execution.
- A `where` item can now say which child rows its conditions mean: a child group
  `{child, match, where}` keeps a row when at least one of its child rows meets every
  condition (`match: any`), or when none does (`match: none`). "Customers with an item that
  is a beverage and costs over 5" is one group; "a beverage, and some item over 5" is two.
  Groups compile to correlated `EXISTS` / `NOT EXISTS`, so no parent is counted twice. A
  query states one child scope: beside a group nothing else may cross a one-to-many hop,
  and groups on different children are refused with `MIXED_GRAIN_INVALID`.
- Two kinds of plain filters on one child entity are now refused with
  `AMBIGUOUS_CHILD_SCOPE` instead of `MIXED_GRAIN_INVALID`: two or more positive filters
  (`same_row` / `separate_rows`), and one negated filter (`!=`, `NOT IN`, `IS NULL`, ...:
  `any_not` / `none`). Its `details.clarification.options` holds each reading as the
  query's whole rewritten `where`, ready to resend. It is offered only when every reading
  answers for this caller: each option is bound (every measure, the warehouse's rules, row
  policies) and passes the caller's semantic policies. Otherwise, and for every other
  shape (a negated filter beside another, filters on different children, an operator such
  as `IS DISTINCT FROM` or `NOT ILIKE`), the query keeps `MIXED_GRAIN_INVALID`. So does one
  whose group on the child would take another route than the filters' own (the refusal
  names the `graph.path_preferences` row to record).
- An ambiguous route to a group's child is refused with `AMBIGUOUS_PATH`. A group is
  refused under a row policy, never reads a rollup, and on ClickHouse only one `any` group
  on the child's own columns is answered.
- Explicit child groups are unavailable under restricted metric and dimension grants,
  which do not provide authority for caller-selected child entity scopes.
- A query with no `time` block that selects measures of different entities or governed metrics
  with differing sets of real time roles, mixing at least two distinct roles, now carries one
  `MIXED_TIME_ROLES` warning naming those roles. Each period is read on its own role's clock;
  measure-level filters can bound those periods. Undated measures are ignored. A governed
  metric counts as one clock, and measures that share a role never warn. The SQL and the rows
  are unchanged. See [What an answer covers](docs/QUERY_IR_SCHEMA.md#what-an-answer-covers).
- `additive: false` on an `aggregate` measure declares values that are already aggregated, such
  as a vendor's pre-counted unique visitors, which the engine must never add together. A query
  that would sum more than one of its rows (for a stock, more than one series) into an output row
  is refused with `ROLLUP_UNSAFE` and `details.unsupported_construct: non_additive_sum`, pointing
  to the measure's key; cumulative, rolling and period-to-date metrics over it are refused, and
  `avg`, `min`, `max`, `median`, `percentile` and `prior_period` stay available. `ROLLUP_UNSAFE`
  previously meant only a parent-entity rollup; check `unsupported_construct` to tell them apart.
  The new field is part of each measure's semantic payload, so every package's
  `semantic_fingerprint` changes once on upgrade.
- A measure filtered by a dimension across a one-to-many hop (order revenue from orders
  with a beverage item) now compiles instead of returning `MIXED_GRAIN_INVALID`: its leaf
  keeps one row per (entity key, output grain) before it aggregates, so each order counts
  once, however many matching items it has (EXISTS). A distinct count grouped by such a
  dimension (orders per item product type) counts each order once in every type it
  contains. A `REWRITE_APPLIED` warning (`fanout_dedup`) states both meanings. The path must
  go down one-to-many hops, each joined on the declared key of its one side, before any
  lookup; no package change is needed. The new leaf does not answer, and `why_invalid` says
  why: another aggregation grouped across the hop (order revenue by item product type),
  negated, null or `false` tests across it, a second group or filter across a one-to-many
  hop (a query may have one), measures whose rows are finer than their entity's key, and
  many-to-many or off-key paths. Every leaf that crosses such a hop, including the
  `entity_in_terms_of` rewrite in a query with several measures, now carries a rewrite step.
- A `kind: lookup` measure (`from:` a measure, `via:` a parent entity) carries the parent's
  all-time total onto each of its child rows, such as a coverage's premium on each claim. It is
  answered only where each output row holds one parent: coarser groupings are refused with
  `ROLLUP_UNSAFE`, a child of the child with `MIXED_GRAIN_INVALID`, and time on the lookup itself
  with `REWRITE_NOT_SUPPORTED`. Composite parent keys and conflicting recorded routes are
  refused at load; access policies also govern the lookup's direct relationship. Physical
  relation names are preserved even when they match a nested lookup CTE name.
  See "Lookup measures" in
  [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
- Derived metrics whose expression is a distribution can be selected.
- A query whose sum, count or distinct count (or a sum or difference of them) reads `NULL` on
  every returned row, or that returns no rows with no time window and no metric filter, now
  carries one `NO_DATA_IN_SCOPE` warning that names those outputs. A misspelled filter value
  used to read as a confident `0`; it now reads `NULL` with the warning. A `prior_period`,
  ratio or rolling output never gets it. It needs no extra query, and a clipped (`truncated`)
  result never gets it.
- A query whose lowering skips the step that settles empty groups is refused with the stable
  code `EMPTY_GROUPS_UNSETTLED` instead of answering with a silent `NULL`.
- Accept packages that declare no time for counts, sums, ratios, grouping, filters
  and lookups. Explicit Query IR time requests refuse with an actionable `INVALID_TEMPORAL_ROLE`;
  project scaffolds accept a blank time column.
- Return every natural-language draft on a package without time as `low_confidence`,
  retaining its Query IR with a warning to check for a time breakdown or window.
- A new `object_access` action, `withhold_values`, lets a caller rank by a metric or
  measure without seeing its values: "the 3 biggest accounts in EMEA by revenue" returns
  the accounts only, with a top-level `withheld` list and a `VALUES_WITHHELD` warning. The
  query selects the metric directly and orders by it first, then by every group key in the
  same direction (added when omitted), with a `limit` of at most `config.max_rank`
  (default 10, at most 100). Every other use (a selected expression or derived metric that
  reads it, a filter or threshold on it, a segment on it, `export`, a larger limit) is
  refused with `POLICY_DENIED` and `details.withheld_objects`. `deny` and `redact` are
  unchanged.
- Withheld ranks sort NULL values and group keys consistently across warehouses, so
  ascending reverses descending exactly. Their diagnostics exclude withheld values;
  resource-granted responses retain the withholding notice and redacted output descriptors.
- Resource-granted responses expose only listed engine diagnostics for granted objects,
  omitting semantic caveat metadata and summaries.
- The MCP `execute` tool refuses a result whose rows serialize to more than 32,000 characters
  (about 8,000 tokens) with the error `RESULT_TOO_LARGE`. No rows are returned; the message names
  the row count and what would fit (a set or coarser `time.grain`, a filter, fewer `group_by`
  dimensions or columns), and `details` carries `row_count`, `total_row_count`, `result_chars` and
  `max_result_chars`. Set another limit with `SEMANTIC_RAILS_MCP_MAX_RESULT_CHARS`. The
  `max_rows` cap still clips long results and reports `truncated`. See
  [MCP interface](docs/MCP_INTERFACE.md).
- Query MCP sessions point repeated requests with the same row cap to their first
  response and flag validation or SQL requests with the latest successful run's
  row count. Capped historical responses include their truncation flag and cap.
- Session-scoped query MCP dry runs now advise agents to use an already-run result
  or change the query, with firmer guidance after consecutive identical validates.
- `semantic-rails check` and `validate` list the join routes a package still has to decide. The
  parse report's `route_census` names every entity pair a question can need (from any
  entity to each other reachable entity) that is refused with `AMBIGUOUS_PATH` until a
  `graph.path_preferences` row records its route (`undecided`, with the refusal's clarification
  options; pass an option's `decision` to `record_route_decision`), and
  the multi-route pairs answered by the start entity's own key (`assumed`, to confirm). One
  `ROUTES_UNDECIDED` warning counts them; the shipped `jaffle_shop` package has 18. Architect's
  `project_status` returns the census, and its next actions, like those of `create_project` and
  `setup_project_dialog`, say to decide the pairs; `promotion_check` lists them under
  `advisories`, never as a blocker.
- `impact-report` lists `route_changes`: each such pair a change resolves differently, such as a
  pair refused after a new relationship adds a second route, with its base and head route or
  refusal code, without suggesting recovery rows. Any entry makes the risk
  `high`, and the Markdown summary lists each one in entity labels. See
  [the route census](docs/PACKAGE_AUTHORING.md#route-census-and-route-changes).
- Architect writes and previews refuse unapproved changes to answered join routes with
  `ROUTE_DECISION_NOT_RECORDED`, listing affected pairs and explicit `graph.path_preferences`
  fields (`source_entity`, `target_entity`, `relationship_path`), without suggesting rows.
  Census, impact and guard comparisons use package decisions independently of active query
  route overrides. Authors record decisions with `record_route_decision` or include chosen rows
  in the change; Architect generates no route rows and `route_decisions_added` stays empty.
  An explicit route defines lookup semantics, including unmatched keys. Removals use the
  same preservation guard: a cut may leave a pair refused, while another answer requires
  its own decision. Deliberate decisions and removals report every changed pair in
  `route_changes`, including refused-to-answered and inherited changes. Writes and previews
  with `validate_after=False` refuse loader-invalid input when the current package loads,
  preserving the route baseline through subsequent edits.
- Query IR `route_decisions`: a query can answer an ambiguous route with the option the person
  chose, for that query only. Each row is shaped like a `graph.path_preferences` row (its `label`
  is ignored), must take one of the pair's routes within the hop ceiling (else `INVALID_QUERY`,
  `route_not_offered`), is checked by the loader's rules, and applies before the package's row for
  the same exact pair, never to other pairs and never cached as the package's route. A bad row,
  two rows for one pair, or a row for a pair the query never walks is `INVALID_QUERY`; under a row
  filter on any entity of the pair's routes it is `POLICY_DENIED`
  (`route_override_under_row_policy`). Each applied row is disclosed, at every verbosity, as an
  `info` warning `ROUTE_CHOSEN_BY_QUERY` with the row and `replaced`, how the package resolves the
  pair without it (`decided`, `colocated_key`, `inherited`, `only_route` or `undecided`), and
  `hop_profile` reports `route_basis: query`. `build-options` follows the rows (a patch that would
  leave a row unused is offered blocked with that refusal), and live `valid-values` reads values
  through them using only the query's measures, metrics included. Invalid rows and
  anchor refusals propagate unchanged before SQL; unrelated measures never supply values. See [`route_decisions`](docs/QUERY_IR_SCHEMA.md#route_decisions).
- A `graph.path_preferences` row takes an optional `label`, the route's meaning in business words.
  The loader and the package writer keep it, and a compile's `hop_profile` and discovery's path
  availability show it as `route_label` for the recorded route.
- Architect `record_route_decision(source_entity, target_entity, relationship_path, label)`
  records a pair's route in the `path_preferences` list the loader reads, replacing every row for
  exactly the pair. The row is checked by the loader's rules, and the changed package is loaded
  before anything is written: an off-route path, a row another row disagrees with (named in
  `details.rows`), or a row that would not take effect is `INVALID_CONFIG` and writes nothing. It
  returns the row in effect before (`replaced`) and a one-sentence `summary` for the review. It is
  on the Architect MCP server and `ArchitectProject`. See
  [the Architect MCP](docs/ARCHITECT_MCP.md).
- Add an opt-in `snowflake_adbc` connector experiment with bound row-filter
  parameters, Arrow decimal results, query tags, and session statement timeouts.
- Keep Snowflake ADBC native driver selection under runtime operator control;
  package connection options cannot select a driver library or manifest.
- Refuse Snowflake ADBC timestamp overflow and temporal values with nonzero
  sub-microsecond precision instead of returning incorrect dates or losing precision.
- Validate Snowflake ADBC authentication sources when loading packages and during
  guided setup; refuse named profiles that this connector does not use.
- Accept literal account and user locators and file-based key passphrases for
  Snowflake ADBC; expose its allowed options through `semantic_rails.embedding`.
- Add positive `connect_timeout_seconds` and `read_timeout_seconds` options
  for native Postgres, ClickHouse, Databricks, Snowflake, BigQuery and Athena
  connections; longer request deadlines receive a five-second client margin.

### Changed

- The comparison pack qualifies Semantic Rails' frozen-model count with its unreleased
  engine commit and the latest checked release's count, and tests fresh query results
  against the independent answer key.
- A group with no rows now reads `0` or `NULL` by one rule: sometimes there is no data (`NULL`),
  and sometimes there is data of nothing (`0`). A `sum`, `count` or `count_distinct` of an
  additive, event-count or entity-count measure reads `0` where the measure has data in scope
  and `NULL` in every group where it has none; an average, minimum, maximum, stock, distinct
  population or `additive: false` measure stays `NULL`. One settling step now applies this to
  every query, so a count beside a second fact reads `0` for a group the second fact lacks,
  and a measure beside a `distribution` no longer reads `NULL` for a period without rows. A
  `metric_predicate` follows the same rule, so `orders - returned_orders > 1` keeps a customer
  with 2 orders and no returns, as a metric filter on the same expression does. Every entity,
  with rows or none, reads the same: an operand is `0` where its measure has data somewhere in
  the predicate's scope and `NULL` where it has none, and `NULL` passes no threshold. So
  `large_orders = 0` keeps every customer without a large order when some order in scope is
  large, and keeps nobody when none is. One answer
  changed the other way: a `sum` or `count` with nothing in scope reads `NULL`, not `0` (an
  ungrouped count of a filter that matches nothing). Arithmetic settles its operands first, so
  `revenue - refunds` is `NULL` if refunds were never recorded.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
- Known limitation: a `time.fill` bucket in a window with no rows reads `NULL` even when the
  measure has data outside the window, where the rule says `0`. It stays until the engine
  checks for data outside the window (tracked in
  [issue #201](https://github.com/semantic-rails/semantic-rails/issues/201)).
- Known limitation: an ungrouped distinct-population count over no rows (a count of distinct
  customers under a `where` that matches nothing) reads `0` with no `NO_DATA_IN_SCOPE`
  warning, where the rule says `NULL` (tracked in
  [issue #203](https://github.com/semantic-rails/semantic-rails/issues/203)).
- Refusals name visible non-additive key dimensions, rank compatible replacements by
  naming tokens before character similarity, suggest visible exact metric/measure counterparts,
  and name the allowed measure default when aggregation can be omitted. Invalid filter
  operators list query operators and show the null-test form.
- Error suggestions use one package snapshot for candidates and visibility checks.
- A select item sent as `{"metric": "<id>"}` or `{"measure": "<id>", "aggregation": "sum"}`
  without its `expression` wrapper, and a `{"dimension": "<id>"}` in `select[].expression`
  beside an empty `group_by`, are now accepted instead of refused with `Expression requires a
  'kind'`. They compile exactly like the canonical form. `validate`, `compile` and `execute`
  (the MCP `execute` tool in every mode) report every rewrite, including the existing bare
  `{"dimension": "<id>"}` select item, with a `QUERY_SHORTHAND_NORMALIZED` warning naming the
  canonical form. `plan` accepts the same shapes in its `query` but returns no such warning:
  the canonical form is in `best.query_ir`.
- A dimension moved out of `select` into `group_by` shifts the position of every later select
  item, so an unaliased expression after it takes a default alias (`expr_N`) and a diagnostic
  path (`select[N]`) numbered in the rewritten `select`, not in the query as sent. Give such an
  expression an `as`.
- A rewrite never drops a key: a select item naming more than one of `metric`, `measure` and
  `dimension`, a dimension item with `as` or any other key, and `expression` beside `metric`,
  `measure` or `dimension` are refused with the canonical form in the message. The bare
  `{"dimension": "<id>", "as": "<alias>"}` item, which used to lose its alias, is refused too.
  An ungrained-time warning now reads the query after the rewrite, so a shorthand dimension
  counts as grouped. See [Query IR schema](docs/QUERY_IR_SCHEMA.md).
- The Architect's `suggest_model`, dbt import suggestions and their `upsert_model` drafts give a
  time column named like a snapshot's as-of time (`snapshot_date`, `as_of_date`) `class:
  as_of_time` instead of `event_time`. A `stock` measure on such a clock whose key doesn't contain
  it is refused instead of summing every snapshot.
- A query answered by a `stock` measure whose key doesn't contain its event- or state-time clock,
  including one read only by a metric predicate or segment filter, now carries a
  `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` warning in its `warnings`, not only at parse time (query,
  validate and compile answers, `segment_validate` and `segment_explain`; `segment_preview`
  returns no warnings yet). Such a
  stock counts each row as its own series, which is right for a table with one row per series and
  sums the snapshots of a table that keeps several; the answer now says so. Key a snapshot table
  by its series columns plus the snapshot time, and declare that clock `class: as_of_time` so a
  mis-keyed table is refused instead.
- A `stock` query refused because its key can't tell apart the snapshots of an as-of clock
  names a clock in the `INVALID_CONFIG` error only when it's the queried one, and never lists the
  key: a `metric_constraint` policy may hide the other clocks from the caller. The error says to
  key the entity by its series and snapshot time, to keep one as-of clock in the key, or to query
  the stock on its as-of clock, and the package's parse warning names the clock.
- A `stock` measure whose row key (its declared grain, else its entity key) doesn't contain its
  clock's column now gets a `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` parse warning. Keyed by a
  surrogate that is unique per snapshot row, every snapshot counted as its own series, so a week
  holding two daily snapshots of one series returned their sum. **Upgrade note:** on an
  `as_of_time` clock such queries are now refused with `INVALID_CONFIG` instead of returning that
  sum, so `project validate` and `semantic-rails check` report a failed probe for the measure, and
  a segment that filters on it makes package validation fail, until the entity is keyed by the
  series columns plus its snapshot time (for example `key: [store_id, date_day]`).
- A `stock` measure on a snapshot table with a second clock no longer sums snapshots on the
  clock that isn't in its key. If its key holds none of its `as_of_time` clocks, every query of
  the stock is now refused with `INVALID_CONFIG` (before, only queries on that clock were), and a
  query ordered by another clock while the key holds an as-of clock is refused too, with a new
  `STOCK_SERIES_HOLDS_AS_OF_CLOCK` parse warning. Each used to return the sum of a series'
  snapshots. Keep one as-of clock in the key and query the stock on it. An event-time column in
  the key, such as a cohort month, still identifies a series there.
- The Architect's `suggest_model` (and dbt import suggestions) flag a numeric column named like a
  count of distinct people (`unique`, `uniques`, `distinct`, `visitors`, `users`, `cloners`, but
  not an average or rate of one) as a low-confidence measure whose suggestion carries
  `additive: false` and asks the author to declare it, instead of calling `sum` "the usual
  default": a vendor's pre-counted uniques can't be added up across days or pages. The applied
  draft still sums the measure and doesn't set `additive`; declare it yourself. The REPL's
  `author model` leaves these columns unticked by default.
- In a package whose one-to-many relationships are `rollup_safe`, a distinct count grouped
  by two dimensions that each cross a one-to-many hop, on the same child (orders by item
  product type and item product name) or on different children (customers by item product
  type and session store), used to be answered and now returns `MIXED_GRAIN_INVALID`: two
  groups that cross a one-to-many hop, on the same child or on different children, now
  refuse; ask one such group per query. A measure's leaf refusals are now all
  `MIXED_GRAIN_INVALID` (some were `REWRITE_NOT_SUPPORTED`), with recovery hints.
- Remove the declaration-order fallback for primary entities and refuse graph
  relationships authored with `from`/`to`; use `entities: [source, target]` instead.
- The engine never chooses a join route by hop count or weight. Which of two routes to an entity a
  question means is a business definition: the package records it once as a
  `graph.path_preferences` row, and every query uses it. For each start and target entity, over
  every route within the hop ceiling: a query's own `route_decisions` row for exactly the pair
  wins, for that query only; then a package row for exactly the pair; then the start entity's one
  direct key (a many-to-one or one-to-one relationship from it); then the routes that follow every
  row whose pair they walk through, when one remains. Anything else is refused with
  `AMBIGUOUS_PATH`, whatever the routes' lengths: two direct keys, routes with no direct key, and
  routes that all fan out, where the shortest used to win. The refusal is a clarification:
  `details.reason` is `route_decision_required`, and `details.clarification` asks which route the
  question means, one option per route with its meaning in business words and the row that
  decides it (rows accept entity ids as well as keys and names). One place resolves every route
  (grouping, filters, a measure's own filter, metric predicates, time roles, conversions, the
  direct read of a foreign key, grain recovery hints and discovery), and it remembers a refusal as
  it remembers a route. So adding a route never changes an answer silently: a pair answered by its
  own key keeps the answer, and any other pair is refused until a row records it. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- `PATH_ALTERNATES_UNPINNED` is replaced by two short `info` notes, at `compact` and `full`
  verbosity: where the engine chose one of two or more routes for a pair the query reads,
  `ROUTE_COLOCATED_KEY` (the start entity's own key) or `ROUTE_RECORDED` (a
  `graph.path_preferences` row) names the chosen route (`details.route`, and its meaning in the
  message). A pair with one route gets none, and the minimal response, the MCP default, leaves
  them out.
- A calendar dimension reached only through other facts' rows (orders grouped by a calendar month
  through store inventory snapshots) is now refused with `AMBIGUOUS_PATH` instead of
  `MIXED_GRAIN_INVALID`; its recovery hint still points at `time.grain`.
- `jaffle_shop` records four routes (an item's customer and store through its order, and the stores
  and products a customer ordered) and the comparison package two (an item's customer and store
  through its order); an order's, a lifecycle event's or a session's own customer and store keys
  need no row. Every answer is unchanged. The package writer writes `graph.path_preferences`, and
  an Architect removal drops, and lists, the rows that name an entity or relationship it removes.
- Packages imported or converted from other tools may need `graph.path_preferences` rows: a
  MetricFlow project with denormalized foreign keys, or a package exported to Ossie and back (the
  export doesn't carry the rows), can have pairs that were answered by their shortest route and
  are now refused until a row records the route.
- Query MCP restores the expression-shape list beside arithmetic and conditional-count
  hints, explains empty-select row listings and optional validation, and retains static
  demo ids in server instructions. Segment tool and prompt availability follows package
  reloads; the MCP doctor checks the core tools and any configured segment tool.
- Query MCP responses omit empty optional envelope and issue fields, redundant error
  explanations. Required envelope fields, distinct issue
  explanations, policy context, and recovery hints remain available. Minimal discover
  cards omit redundant kind and availability fields; compact and full cards stay unchanged.
- `MIXED_GRAIN_INVALID` no longer offers a `closest_valid_query` that swaps the requested
  measure or dimension for another one: a different measure answers a different question
  (item revenue is not order revenue). `replace_measure` and `replace_dimension` still name
  the compatible objects, and `details.closest_compatible_measure_query` and
  `details.closest_compatible_dimension_query` are gone. A `use_time_grain` fix keeps its
  query, since it asks the same question.
- A filtered query now reads `0`, not `NULL`, for a sum or count with no rows when its measure
  has data anywhere else. Whether a measure has data is judged across its own rows, after its
  authored conditions and your row filters, ignoring the query's `where` filters: if store 5
  sold no apples, "apples at store 5" reads `0`, as store 5 does in a breakdown by store, and
  no `NO_DATA_IN_SCOPE` warning comes with it. A string `=` or `IN` `where` value that matches
  no row now adds one `FILTER_VALUE_NOT_FOUND` warning naming the value and the closest one, so
  a misspelling isn't read as a confident `0`. A measure whose authored condition never
  matched still reads `NULL`. Send `observation_scope: "query"`, or set
  `defaults.observation_scope: query` in the package, to judge inside the query's filters as
  before. A metric predicate still selects the population measured, in either scope. Under
  the new default, a query with a `where` filter beside a metric predicate or a
  `distribution`, or over a measure whose condition reads a fan-out or has a `CASE` below its
  top level, is refused with `EMPTY_GROUPS_UNSETTLED` and asks for `observation_scope: "query"`.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
- Filter-value warnings check each string literal with warehouse equality, including child
  conditions, and preserve request limits. A failed or unsupported existence read reports
  `FILTER_VALUE_UNVERIFIED`; suggestion failures omit only the suggestion. Under dataset
  observation, unknown amounts remain `NULL` without `NO_DATA_IN_SCOPE` when the measure
  has data elsewhere.
- Resource-granted callers retain `FILTER_VALUE_NOT_FOUND` and `FILTER_VALUE_UNVERIFIED`
  warnings for their granted filter dimensions, so an unverified filter value is not silent.
- Unknown measure keys now fail package loading with `INVALID_CONFIG`.
- Relationship contract payloads no longer carry `rollup_safe_aggregations`.
- `plan` no longer reports `ok` when its draft leaves out a question word that names something in
  the catalog. "Revenue by store, customer type and product type" drafted revenue by store alone,
  and a question about discounts could draft a measure described as "the charges that are not
  discounts", each with only a `PLAN_UNMATCHED_TERMS` warning. A word of the label or aliases of a
  measure, metric, dimension, entity, segment or time role, or of the last dotted part of its id
  or name outside its own namespaces, must now be consumed by the draft: by the label, aliases,
  id or name of an object it selects, a filter value, a time grain or count ("number of") it
  carries, or a time phrase it read. A synonym, a typo, a namespace, a description, a framing
  word or an object the draft
  doesn't select (a measure's entity included) never consumes one, so one catalog name can't
  stand in for another; "revenue from orders" is held back too, since Orders is a measure, and
  so is "revenue by store, date" when the draft carries no time grain. Otherwise the plan is
  `low_confidence` with `why.code="PLAN_UNMATCHED_TERMS"` naming the words, and
  `why.details.dropped_groupings` when they sit in a grouping the question asks for; the draft
  stays in `best`. Descriptions and topics never account for a word in the warning any more, so
  the warning now names a word only a description holds; such a word, which no object's names
  hold, stays a warning.
- Plan responses carry the query only in `best.query_ir`; validate or execute that
  draft directly. Compact fallback diagnostics reference existing intent slots,
  and repeated catalog rows and query fields reference their canonical value.
- Run `postgres_native` through ADBC with bound access-policy row filters, exact
  Decimal and aware timestamp results, bounded fetching and millisecond deadlines.
  The `postgres` and `all` extras now install ADBC and PyArrow instead of psycopg;
  `schema` selects one exact, case-sensitive schema name. Queries preserve
  inherited statement timeouts and restore prior session settings after overrides.
  Plain SQL accepts JSON operators; parameterized SQL still refuses `?` operators.
  Bind scanning preserves identifiers containing `$` and E-string escapes.
  Session zones unavailable to Python return aware UTC timestamps.
- Split seed scripts only at unquoted semicolons, preserving statement text,
  comments and E-string escapes. Tagged and untagged dollar-quoted values retain
  comment delimiters and semicolons verbatim; unterminated quotes or block
  comments refuse the script before any statement executes.
- A contextual `metric_predicate` whose input is measured on another clock than the query's
  `time.temporal_role` is refused with `INVALID_TEMPORAL_BINDING`, naming both clocks and the
  choices. It used to be matched to the query month by month on the input's clock without saying
  so. Query on the input's clock, use `scope_mode: entity_only` for all time, or set
  `time_alignment: same_query_period` (pinning one clock with the input's `temporal_role` if it
  has several) to compare the calendar periods on purpose. Plan drafts that would cross clocks
  are no longer offered as ready to execute.
- Query results use one JSON value format across warehouses and transports, with
  decimal columns encoded as precise strings, float and integer columns as numbers,
  ISO dates and times retaining full seconds precision, query-zone offsets for aware
  timestamps, and explicit naive metadata. Package snapshots, CLI tables and MCP
  segment previews retain and interpret the result column types. Encoding follows
  driver values; authored types leave text and derived numeric results unchanged.
- An `AMBIGUOUS_PATH` refusal now asks which route the question means, in business words:
  `details.clarification` replaces `details.candidates`, `details.meanings`, `details.pins` and
  `details.conflicts_with`. It holds the question ("Which District does the question mean for an
  Account?") and one option per route the route rule keeps: a `meaning` built only from package
  labels ("the District of the Account's Branch"; a one-to-many hop reads "any of the …", and two
  relationships between the same entities are told apart by their own label or their foreign-key
  columns), an `id` unique within the refusal (`branch_district`, `origin_airport`), the route's
  `relationship_path`, and its `decision`: the `graph.path_preferences` row that makes it the
  package default. When that row would disagree with the package's rows, the option adds
  `conflicts_with`, the rows to change before recording it; its `decision` still answers per
  query. Every refusal of an ambiguous route, a conditional aggregate's included, carries the same
  clarification. `details.start`, `details.target` and `details.hint` stay. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- The refusal's `details.hint` tells agents to ask, then resend the chosen option's `decision` in
  `route_decisions`; the query MCP's tool descriptions don't grow.
- A join route is chosen only by a recorded decision or the start entity's own key. For each
  start and target entity: a `graph.path_preferences` row for exactly the pair wins; otherwise
  the start's one direct key to the target is used, even where a row for another pair points
  elsewhere (a loan that holds its own district reads it, noted `ROUTE_COLOCATED_KEY`);
  otherwise every row holds wherever a route walks its pair, so a row for (account, district)
  also decides the district of a loan, card or transaction reached through the account, the
  region beyond the district, and, walked back, the accounts of a district. The one route left
  is used; two or more are refused with `AMBIGUOUS_PATH`, whatever their lengths. See
  [the route rule](docs/PACKAGE_AUTHORING.md#the-route-rule).
- When the rows rule out every route within the hop ceiling, the query is refused with
  `PATH_NOT_FOUND` and `details.reason: excluded_by_decision`, naming the rows in
  `details.rows`.
- `graph.path_preferences` rows must agree: when one row's path walks through another row's
  pair by a different route (or the reverse pair records another route), the package fails to
  load with `INVALID_CONFIG`, naming the rows in `details.rows`; a configuration built in code is
  refused the same way when first used.
- `ROUTE_COLOCATED_KEY` notes list, in `details.alternatives`, the row that would make each
  other route the default, only when that row would load; `details.conflicts_with` names any
  other route with the rows its row would disagree with (an `AMBIGUOUS_PATH` option says the
  same in its own `conflicts_with`). A route inherited from rows is noted `ROUTE_RECORDED`, with
  the rows it follows in `details.rows`. A note names only a route the SQL reads. `hop_profile`
  targets carry `route_basis`: `query`, `decided`, `colocated_key`, `inherited` or `only_route`.
- A distinct count computed from a child's rows (customers counted from their orders) reads
  each grouping through the counted entity's own route. A customer's city read through its own
  key beside its region recorded through the orders' ship-to city is now refused with
  `PATH_JOIN_CONFLICT`; it used to read both from the ship-to city.
- `jaffle_shop` and the comparison package keep every answer. Pairs they refused because two
  routes reached the target now follow their recorded rows where those leave one route (for
  example, the items of a customer's orders).
- Reduce minimal discovery cards by merging unpinned default-aggregate metrics with their
  measures and omitting scores. Keep whole description sentences and preserve package-object
  references beyond the description cap; omit generic next actions from minimal inspect cards.
- Bound compact MCP execute results before transport warnings and session annotations by
  omitting optional plan details with a SQL-mode retrieval hint and warning severity. Sizing
  respects normalized verbosity and unknown-value fallback; row-size refusal rules remain.
- Minimal discovery retains grant-scoped starter patches and separate cards for unavailable
  or policy-targeted aggregate pairs. Additional equivalent metrics keep their own cards.
- A group whose rows exist but whose amounts are all `NULL` now reads `NULL` for a `sum`, as
  SQL's `SUM` does, instead of `0`: its amounts are unknown, and a count still counts its rows.
  A sum reads `0` only in a group with no rows while its measure has data elsewhere in scope,
  and a conditional sum (`aggregate_if`, or an aggregate with a `filter`) only where no row
  meets its condition. The period's own value, filled (`time.fill`) or unfilled, and a
  `prior_period` read of it are `NULL`; rolling and cumulative windows skip its unknown
  value. A window of a sum or difference windows each operand first, so an unknown goods
  amount drops only the goods, not that month's revenue. A summing window refuses a metric
  referenced inside its input, such as `net * 2` where `net` is a metric, with `ROLLUP_UNSAFE`
  (`nested_metric_window_input`); write that metric's expression inline instead.
  An unknown amount carries through: a ratio over it is `NULL`, and neither a
  `metric_filters` threshold nor a `metric_predicate` threshold keeps it, one that `0`
  passes (`< 5`) included, and
  `goods + shipping` by refund type is `NULL` for a type whose rows leave one of the columns
  `NULL`. A `metric_predicate` threshold that `0` passes on an add or subtract of measures
  still reads an operand's unknown amounts as `0`, so `goods + shipping = 0` keeps the orders
  with no refunds. A sum of a `case` measure with no `else` (or `else: null`), with one
  branch or several, reads `0` in a group where no row meets a branch and is no longer
  answered from a rollup, which can't tell rows that fail its conditions from rows that meet
  one with no amount. All of this covers queries without a `distribution`. A query with a
  `distribution` output keeps the earlier settlement, and its plan and SQL, in every output:
  there a sum whose amounts are all `NULL` still reads `0` where its measure has data in
  scope, and arithmetic beside the distribution settles each operand that way.
  A measure containing a `case` below its expression root, such as a conditional amount
  divided by 100, keeps the earlier settlement individually and never reads a rollup:
  no-match groups read `0` where an amount is known elsewhere in scope; matched-unknown
  groups also read `0` there and stay `NULL` only when no amount is known in scope.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
- Source rollups preserve aggregate amounts when a physical join column is named
  `__source_value`, including unknown amounts that must remain `NULL`.
- Native warehouse connection operations default to 10 seconds and supported
  network reads/query waits to 65 seconds per operation; configure larger waits
  for long queries. Driver retries and polling can extend total elapsed time.
  Postgres/Snowflake server deadlines remain opt-in; explicit zero defers to
  the server on Postgres and disables the session limit on Snowflake. Named
  Snowflake profiles retain inherited settings; put `QUERY_TAG` in the profile,
  since nonempty authored `query_tag` overrides are refused before connecting.
  Direct connections still pass authored tags via connector session parameters.
  MotherDuck and Snowflake CLI are excluded from these client defaults.
- Named Snowflake profile sessions are cached only after an authored statement
  timeout is applied successfully; setup failures discard the connection even
  if closing it also fails.
- BigQuery supplies a default server job deadline and attempts cancellation on
  result timeout; Athena cancels unfinished queries on polling timeout.
- A `time` block with a `start` and/or `end` window and no `grain` now returns one total over the
  window for each `group_by` group, with no time column, including a window inside one day. It
  used to return one row per raw timestamp. The response names this in `assumptions` and sets
  `time_shape: "window_total"`; both survive `minimal` verbosity and a metric grant. Set
  `time.grain` for one row per period. A `time` block with no window, a query that needs a time
  axis (a rolling or prior-period expression), and a query with a metric predicate still group by
  the raw timestamp and warn `UNGRAINED_TIME_PROJECTION`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#timeblock).

### Removed

- Query MCP no longer advertises transport-only `request_id` and `policy_context`
  in tool input schemas; every tool still accepts them at runtime.
- The `null_behavior` key is removed from metrics and expressions (`coalesce_zero` on
  arithmetic, and the `null_if_zero` a ratio carried), from the query IR schemas, the
  capabilities payload, the Ossie export and import, the REPL metric wizard and the
  MetricFlow import. A package that still authors it fails to load with one message:
  delete the line. A ratio always divided by `NULLIF(denominator, 0)`; an empty group is now
  settled by the engine (see [the empty-groups rule](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0)),
  so an operand with data reads `0` without a `coalesce_zero`, in a `metric_predicate` too. Ossie
  SQL written as `COALESCE(x, 0) + COALESCE(y, 0)` is no longer read back as a metric.
- Remove measure `subject_entity` and `aggregation_entity` declarations and unused
  forward relationship rollup hints. Measures aggregate at their own model grain;
  reverse population-count rewrite permissions remain supported.
- Existing packages must delete `subject_entity` and `aggregation_entity` lines
  from `defaults.measure` and individual measures, and remove forward rollup
  hints from relationships. Unsupported declarations now fail package loading;
  errors for measure defaults name the line to delete.
- Model joins and relationship defaults reject `rollup_safe` in any form instead
  of silently ignoring it; use `graph.relationships` with `rollup_safe.reverse`
  for reverse population-count rewrite permissions.
- Relationship defaults reject `rollup_safe_aggregations` even when its value is
  `null` or the package has no relationships; delete the named defaults line.
- Removed duplicate query MCP error payloads and error recovery hints: `error` now
  contains only the first issue's code and message; full issues and their hints live
  in `errors`. Minimal responses omit mixed-grain relationship analysis and rewrite
  analysis/path details; request `compact` or `full` for those details.
- Removed copies of an issue's `recovery_hints` from its `details` across runtime,
  HTTP, CLI, and MCP responses; hints remain on the issue itself.
- **Breaking:** the query key `path_policy` (`preference`, `ask_if_ambiguous`) is removed from
  Query IR v1 (`schemas/query_ir.v1.json`), the preview v2 schema, and so from the HTTP API, the
  query MCP and a segment's `membership:`. Query IR stays at v1: before 1.0 the project follows
  [Semantic Versioning](https://semver.org/spec/v2.0.0.html)'s major-zero rule, under which a
  0.x release may change the public API. The key never changed an answer: `preference` only
  entered a cache key, and `ask_if_ambiguous` was never read. A query that still sends it is
  refused with `INVALID_QUERY` (`details.unsupported_keys: ["path_policy"]`), and `check` and
  `validate` report it in a segment's `membership:` like any unknown key; delete it. A package's
  `graph.path_policy.max_hops` is unchanged.
- A relationship's `path_preference` weight is removed: a number on a relationship never says
  which route a question means. A package that still sets it fails to load with `INVALID_CONFIG`,
  naming the relationship; delete it, and record the route for each entity pair that needs one as
  a `graph.path_preferences` row. Relationship metadata no longer lists it, and
  `ROUTES_UNDECIDED` warns for every undecided pair a question can need.
- A `graph.path_preferences` row no longer covers only queries that start at its
  `source_entity` and end at its `target_entity`: it holds wherever a route walks its pair.
- A row recorded for the reverse pair no longer stops the direct read of a start entity's one
  own key; the key and the target's other columns still come from the same route.

### Fixed

- Plans with an unknown question word left unresolved and unconsumed no longer offer execute
  readiness, including a city whose filter value has no declared domain, or a plural or synonym
  the intent parse records in another form ("sent messages", "accounts"). The recovery hint
  asks for an explicit filter or a revised question.
- Clock groupings consume only their planned grain and debit each unit once, and only one
  clock grouping per question is consumed, so a dropped second grain or date grouping cannot
  leave a plan ready. Count measures used only in filters no longer consume a selected
  measure's "number of" request. A selected count-valued measure consumes "number of" only
  when the draft has no grouping; grouped counts keep the plan from being ready.
- Running and rolling ratios now divide the windowed numerator by the windowed
  denominator. Summing windows refuse statistics, stocks, distributions, and other
  inputs that do not add up across periods, including distinct counts of non-key columns
  or individual components of composite keys. An entity key cannot establish uniqueness
  when the measure's rows have a finer grain or come from another relation.
  Numeric literal multipliers and divisors preserve the windowed sum.
  Period-to-date on a non-default calendar refuses before execution instead of silently
  resetting on Gregorian periods.
- Refuse models whose grain matches multiple entity keys unless a graph model
  binding or singular `entity:` identifies the primary entity; declaration order
  no longer resolves the ambiguity.
- Report oversized decimal parameters as structured `INVALID_EXPRESSION_AST`
  errors in query validation and package checks.
- Refuse BigQuery decimal CAST precision and scale constraints with
  `INVALID_EXPRESSION_AST` instead of rendering unsupported parameterized types.
- Leave scalar-call argument types and overload resolution to the warehouse,
  so supported overloads compile in query and package expressions. Warehouse
  execution failures retain their stable, redacted error code.
- An `aggregate_if`, and each operand of a `ratio` or arithmetic, now answers under a
  positive filter on a child table's dimension instead of returning
  `MIXED_GRAIN_INVALID`: for example, the share of orders over an amount among orders
  that have a goods refund. Each leaf keeps the rows of its own entity that have a
  matching child, so several matching children never count a row twice. The rules for
  measures apply to every leaf: one child route or a pinned one, at most one condition
  across a one-to-many hop, no negated child filters, only `count_distinct` grouped by
  a child dimension, and `POLICY_DENIED` under a row policy. An `aggregate_if` over a
  model whose measures declare rows finer than the entity's key is refused, as those
  measures are; on ClickHouse, over a model with no measures, only `count_distinct`,
  `min` and `max` are answered.
- ClickHouse statements now end with `SETTINGS join_use_nulls = 1`. Without it an unmatched
  outer-join field read its type's default (`0` or an empty string) instead of `NULL`, so a
  group one measure lacked could read as data of nothing, and the combined time key of a
  multi-measure query could read `0`.
- A policy on a dimension now also refuses a conditional aggregate that reads
  its column in a condition or value on the measure's own table.
- Package checks finish for constant measure expressions, including row counts
  authored as `expr: "1"` with `default_agg: sum`, and inspect column references
  inside compound expressions without traversing literal values.
- Accept portable `DATE_DIFF` scalar calls, including package measures and
  conditional aggregates, with validated units and NULL endpoints preserved in
  averages. Refuse Athena calls and `week` on Snowflake, BigQuery and ClickHouse
  where native semantics differ; preserve ClickHouse NULL endpoints with nullable
  timestamp casts that also preserve pre-1970 dates. BigQuery TIMESTAMP endpoints
  count calendar boundaries in UTC, preserving NULLs and supporting month,
  quarter and year differences.
- `discover` no longer returns empty results for a `kinds` filter that arrived as a
  JSON-encoded string such as `"[\"metric\"]"`: it reads the same as the array. A `kinds`
  value that does not parse is refused (`INVALID_MCP_ARGUMENTS` over MCP, `INVALID_REQUEST`
  over HTTP, both `400`-class), and one that names a kind the search cannot rank is refused
  with `INVALID_MCP_ARGUMENTS` (HTTP `400`) and a `use_valid_kind` recovery hint that names
  the valid kinds, instead of returning an empty result; the `DISCOVER_UNKNOWN_KIND` warning
  is gone. The CLI `--kinds` flag reads the same encodings. An MCP or HTTP `limit` below 1 is
  refused rather than emptying every bucket. A misspelled `kind` argument still warns, and the recovery
  hint no longer claims nothing matched.
- The `discover` no-match hint now follows the response's own `no_matches` signal: it no longer
  appears beside matching dimension values, and a search that was screened out before it ran
  (`low_relevance`, `out_of_scope`) does not claim a kind-scoped search found nothing. In
  resource-grant mode the same refusal applies to any kind a grant cannot produce (only
  `metric`, `dimension` and `temporal_role` are searched, on MCP and HTTP alike), and it keeps
  its `valid_kinds` detail. The MCP empty-terms id listing refuses those kinds under a grant
  too and names the three it can list; the shared catalog (`/catalog`, MCP catalog resources,
  CLI catalog) is unchanged. HTTP has no id listing: empty terms there run the ranked search.
  A grant search never reports `no_matches`, because it covers a filtered view.
- Require DuckDB 1.5.6 or newer to prevent intermittent internal errors when
  filling time buckets over empty measure groups with parallel window execution.
- A DuckDB `limits.statement_timeout_ms` now stops the running query. Before, the timeout
  interrupted the shared connection rather than the query's own cursor, so it never fired and
  a slow query ran to completion. Each query's timeout stops only that query.
- Aggregation error hints now list only the measure's allowed aggregations and
  report the rejected aggregation. Missing-path hints suggest only entities and
  dimensions reachable under the package's path rules. The lists are exact under
  these path rules and do not cache unrelated route refusals.
- Preserve exact native integers inside array and object query results, including
  values beyond binary64's exact integer range.
- The Architect's `upsert_model` no longer wipes an existing dimension, time, measure or join when
  you only relabel it. A label-only update to a time role used to replace the whole role with
  `{label: …}`, dropping its column, kind, class, default flag and grains while the parse gate
  still passed. An update that names only `label`, `description`, `synonyms` or `meta` now keeps
  the object's other fields. Any other update still rewrites the object, and the report's
  `dropped_fields` now names each field that rewrite drops.
- Re-importing a dbt model with the Architect's `import_dbt_project` no longer reverts the
  dimensions, times and measures an author changed on the package model. The import restated each existing dimension, time and
  measure from the dbt draft, so a `stock` measure turned back into a summed `flow`, an
  `as_of_time` clock back into `event_time` (both unreported) and a declared `additive: false`
  was dropped. An import now adds only the objects the model doesn't have yet, leaves existing
  ones as authored and lists them in each model's `kept_objects`; it no longer refreshes an
  existing object's dbt description or value set (change it with `upsert_model`). The model's
  relation, its entity's key and its foreign-key entries still follow dbt.
- `plan` no longer reports `ok` when it answers a question about a period with a stock that
  carries its own trailing window. "Unique visitors this week" drafted "Unique visitors (14 days)"
  filtered to this week and returned the 14-day count as this week's number. When a stock's
  label, name or id states a span and each row the draft reports covers a period of another
  length, the plan is now `low_confidence` with a `subject_window_mismatch` gap, whatever the
  question says, so a daily read of a rolling-window stock is flagged too.
- Refuse graph entities without a key instead of borrowing another exposed
  entity's key. Graph model bindings determine the primary entity independently
  of declaration order and preserve an explicitly authored measure row grain
  even when it differs from the entity key. Invalid or unattached graph
  relationships now fail loading with a named `INVALID_CONFIG` error instead of
  being silently omitted.
- Keep path error hints responsive on branching graphs with recorded routes,
  while preserving which targets and dimensions are eligible.
- Refuse `IS` / `IS NOT` filters with values other than null or booleans before
  execution, with a validation hint to use `=` / `!=` for scalar comparisons.
- Live valid-values keeps query route decisions used by metric filters and temporal
  role overrides, and accepts mixed selections containing `aggregate_if` while
  anchoring on configured query measures. Queries without a measure anchor receive
  an explanation of `NO_VALID_VALUES_SOURCE`.
- A metric no longer loads as a broader metric than the one it says. The loader passes an
  `expression:` to the expression parser as written and carries every direct field a metric
  kind takes into the expression, so a `partition_by` on a rolling, period-to-date or
  cumulative metric is kept, and a field the kind does not take (a `window` on a cumulative
  metric) is rejected at load with the metric named. A `scoped_aggregate` recipe with an
  `anchor` and `window` used to return a lifetime value; it now keeps them and is refused
  when queried until anchored windows compile. Short `measure` and `where` field keys in a
  `scoped_aggregate` recipe resolve like other package-relative references, and the
  `prior_period` shorthand the parser accepts now loads. The `INVALID_ANCHOR_ROLE` hint
  no longer points authors at a metric recipe and suggests an offset column instead.
  A metric that has both an `expression:` block and a direct field (`window`,
  `partition_by`, `offset` and so on) is refused at load instead of ignoring one of them,
  and a `partition_by` entry that is not a dimension of the package is refused at load
  instead of failing every query; short dimension keys resolve like other references.
  A `partition_by` the query does not group by is refused with `INVALID_QUERY`, naming the
  metric and the missing dimension, instead of failing in the warehouse; a `partition_by`
  that is not a list, and a window value that is not a number, are refused at load with the
  metric named. The `prior_period` shorthand resolves a short `measure` key, and the REPL
  drops a window's `partition_by` when a metric switches recipe.
- A many-to-one or one-to-one lookup now keeps the measure's rows whose foreign key is NULL or
  matches nothing in every query shape, not only for a `group_by` or `where` on the plain path.
  A sum grouped by a region two hops away now adds up to the ungrouped total instead of
  dropping those rows; a query with any metric filter, a measure's own `filter`, an
  `aggregate_if` condition, a measure expression that reads another model, a distinct count
  grouped beside a one-to-many child, an entity-set ratio and a dimension-only query keep them
  too, under NULL. The same `IS NULL` condition now returns one answer as a `where`, a
  measure's own `filter` or a segment. Totals change only where such rows exist. A time role
  read through a lookup, a metric filter's own query and the entities its set is matched on,
  a distribution's per-entity values, conversions, a dimension a rollup of the measure's model
  holds, and ClickHouse still leave them out. A rollup of another model, such as one of the
  items for an order count, never does, whichever way the count is read, and no rollup does
  in a dimension-only query.
- A parent count grouped by a child's looked-up dimension counts only parents that exist,
  with or without a time axis. Orphan children no longer inflate the NULL group, while
  existing parents whose children have a NULL or unmatched lookup key still count there.
  Multiple child-to-parent relationships or a reverse-only relationship use the parent's
  own rows, preserving the selected relationship's count without an extra lookup failure.
- A `group_by` or `where` dimension looked up through a many-to-one or one-to-one relationship
  no longer drops the measure's rows whose foreign key is NULL or matches nothing. They group
  under NULL, so grouped rows add up to the ungrouped total, and an `IS NULL` filter on the
  looked-up dimension selects them: "passengers excluding crew" through a crew-roster lookup now
  counts the passengers instead of returning 0. A filter such as `=`, `!=` or `NOT IN` still
  excludes them. Totals change only where such rows exist. The exception is a dimension that
  any rollup of the measure's model holds pre-joined: it keeps the inner join, even for a
  grain that rollup could never answer, so those rows are still left out for that dimension
  (routing to the rollup never changes an answer). A time role read through a lookup, a
  metric filter's own query and the entities its set is matched on, and conversions still
  leave those rows out. ClickHouse is unchanged too: its lookups stay inner joins, because an
  unmatched outer-join column reads `''` or `0` there, not NULL.
- Managed MCP startup reports OS-assigned-port bind failures as configuration
  errors without spawning a process or registering a server. Configuration
  conflicts include the assigned port, and start help explains port zero.
- MCP HTTP servers consume the inherited socket-fd environment variable at
  startup so child processes do not receive stale socket-fd configuration.
- Managed MCP HTTP servers support `--port 0` to select an available port safely
  during concurrent starts, and report the assigned port in start and status output.
- Report package loading errors as structured MCP initialization errors with the
  package path and engine message, keeping stdio diagnostics off the protocol channel.
- `semantic-rails mcp start`, `status` and `stop` no longer hang when `ps` does not
  answer. The process check gives up after five seconds and treats the server as
  unverified, so `stop` never signals a process it could not identify. If identity
  observation fails, `stop` reports `identity_unverifiable` and keeps the server
  registered so the stop can be retried.
- A `sum` or `count` measure whose expression reads a column of an entity two or more hops
  away now aggregates after its joins, instead of pre-aggregating its own table first and
  failing in the warehouse on the column it could not see.
- A measure with no time role, asked for by a date at a time grain (for example a claim
  amount by its open month, when the model's open date is a time role but not marked
  `default: true` and the measure lists no `times:`), now fails with
  `INCOMPATIBLE_TEMPORAL_ROLE` and a `declare_measure_time_role` recovery hint instead of an
  `INTERNAL_ERROR`. Declaring the time role on the model or the measure makes the same
  request answer. Naming a role by `temporal_role_overrides` or an aggregate's `temporal_role`
  on such a measure gets the same hint. An `aggregate_if` can't be used with `time` and says
  so, with no hint. When any requested measure has no time role, the mixed-grain recovery for
  a calendar-date group-by suggests no time block, so no `use_time_grain` hint points at a
  refused query.
- Contextual metric predicates grouped by an attribute of their input rows now
  count within that attribute's values, including NULL groups. Distributions,
  including those in derived metrics, refuse metric filters whose grouping grain
  cannot be preserved instead of returning dropped or resurrected groups.
- Refuse metric predicates in distinct-group queries without a measure or conversion
  leaf instead of silently dropping the filter; add a select that reads a measure, or
  remove `metric_filters`.
- Treat equality and inequality comparisons with null literals as `IS NULL` and
  `IS NOT NULL` in expressions, metric predicate inputs, segment conditions, and
  relation joins, including joins that compare in lower case. Reject ordering
  comparisons against null instead of silently returning incorrect results.
  `IS DISTINCT FROM`, `IS NOT DISTINCT FROM` and `<=>` keep their null-safe meaning,
  and `NOT` of a null literal stays NULL. Comparisons against that computed NULL
  retain SQL three-valued semantics. A metric predicate with a null threshold
  is refused with `INVALID_METRIC_PREDICATE` instead of dropping entities that have
  no rows.
- Keep far-side dimensions on outgoing temporal lookups when the query clock is
  the existing version's validity end, including open versions with no end time.
- Count or sum parents with matching children without multiplying their values,
  including paths through a lookup or an alternate join key when the child
  route is the only candidate or is pinned by the package author. Ambiguous child
  groupings and negations remain refused, and under a row policy these queries
  are refused, as before.
- ClickHouse retains parent deduplication for key-based descents, including beside
  lookup selections, groupings or filters. It refuses lookup-before-child paths
  and paths joined off the parent's declared key, including beside a lookup.
- Plan the question's values from one list phrase on one dimension as one filter
  (`=` for one value, `in` for several) for one total or combined ranking, without
  adding grouping.
  Keep caller filter rows as written, with surrounding field whitespace removed;
  append generated rows unless an identical row already exists.
  Keep separate equality clauses as separate predicates and report contradictions
  without execute readiness.
- Catalog fallback plans now use stable candidate ordering, so tied candidates produce
  the same refusal diagnostics across Python interpreters and hash seeds.
- `plan` is no longer ready when the draft drops a grouping the question lists after a comma:
  "repair cost by incident name, incident" grouped by the incident name alone is held, since two
  incidents that share a name would be added into one row. Each grouping the question lists,
  apart from clock terms and declared values, needs its own matching dimension in the draft, and
  only unmatched terms appear in `why.details.dropped_groupings`; "repair cost by repair", with
  an entity named like the measure, is held instead of answering one total. The check only holds
  a plan: the draft and every other plan are unchanged.
- A listed grouping that names an entity is satisfied only by that entity's own key dimension,
  or by the single declared dimension of that entity whose own words name it, so "order count
  by customer history, month" is no longer ready when grouped by the customer id alone: an
  entity with a composite key is never satisfied, and the plan is not ready. A dimension's
  words for this check are its label, aliases and the last part of its name, not its id.
- `plan` no longer calls ready a draft that picked one reading of a grouping that dimensions of
  several entities match, none of them the measure's own: "order count by month and name"
  (Customer name or Store name) is not ready, with the term in `why.details.ambiguous_groupings`.
  "Customer name" or "store name" in the question, or the dimension in the caller's
  `partial_query` group_by, settles it.
- `plan` no longer changes what a question asks when it reads a time or a name. "Revenue from
  12:00 to 13:00 on 15 March 2017" drafted the whole day and reported `ok`. `plan` resolves days
  and coarser windows only, under one rule: a draft is `ok` only if every number, spelled-out
  number and clock or zone word in the question ("9", "nine", "o'clock", "hour", "noon", "UTC",
  "EST", "ET", "Europe/Berlin") sits inside the text of a construct the draft carries (the date
  or window, a limit, threshold or percentile the question states, a filter value, an object's
  name), never because its value equals one: "at 1930" is not a year. A ranking's count ("top 5",
  "the 5 customers who spent the most") is the limit's text, and a number is a percentage only
  before "%", "percent" or "percentile". A window you pass in `query.time` consumes no time of day: a
  question that states an hour is refused whatever hours the window carries. Otherwise the plan is
  `low_confidence` with
  `PLAN_UNMATCHED_TERMS`, the leftover words in `why.details.terms` and no `next.ready_for`, so
  "between 9 and 17", "from nine to five", "at 14h30" and "in UTC" beside a date are not ready.
  Ordinary words such as "min", "net" and "EBIT" are not clock or zone words. A number range the
  draft doesn't carry ("aged 25-34", "2 to 5 orders") is named the same way; it is never read
  as an hour. A window shorter than a day ("last 24 hours", "past hour", "last 30 minutes") is
  `TIME_WINDOW_UNRESOLVED` with no query, not a query over all time. A zone written as an
  ordinary word ("Pacific time", "local time") is not recognised on its own. To ask for an hour
  range, pass `query.time.start` and `query.time.end` as end-exclusive ISO timestamps in the
  temporal role's time zone. A window restated beside
  itself ("Q1 2017 (January 1 to March 31, 2017)") is one window; two that differ, or the same
  one beside another condition ("revenue in 2017 from customers who signed up in 2017"), are
  named in `why.details.conflicting_phrases`. "Year 2017" and "calendar year 2017" resolve;
  "financial year 2017" and "model year 2017" are reported. A range's spoken last day is
  stated as included in `assumptions`, which `ask` prints with its warnings.
- `plan` reads a measure the question names in full ahead of a shorter one that shares a word
  with it, for a measure by a dimension: "item revenue" is Item revenue, not Revenue, while
  "large order revenue" stays revenue. A ratio or growth question keeps its metric target. A
  question that lists several
  measures ("item revenue and orders in Q1 2017", "revenue, orders and gross profit") is
  `low_confidence` with a `multiple_subjects_unrealized` gap when the draft leaves one out,
  also when a time phrase follows the list.
- `PLAN_UNMATCHED_TERMS` no longer names verbs and function words such as "dated", "placed",
  "only", "while" and "using". Two or more names the catalog doesn't have after "for", "from", "of"
  or "with" ("for tangaroo and vanilla ice") make the plan `low_confidence` instead of a
  warning, since the draft dropped a filter.
- `plan` no longer calls a draft ready when its time window is not the one the question states.
  A window in the draft (one you pass in `query.time`, or plan's own) consumes the date phrases
  plan resolved only if it agrees with them: each bound it carries, read at the day, is the
  earliest start or the latest end of the windows the question states. "Revenue on 15 March 2017"
  against a window for 1 June 2018 is `low_confidence` with a `time_window_unrealized` gap, where
  it was `ok`; hours within the stated day still agree. A year is never consumed because a
  window's bounds hold it, its exclusive end year included: "revenue 2018" against a 2017 window
  and "at 2000" are left over in `why.details.terms`. A year counts as part of a phrase plan could
  not resolve only after a bound or qualifier word ("before 2017", "the end of 2017"). In a
  question over 2,000 characters, only a single 20xx year after "in", "for", "during" or "year"
  is checked, as a calendar year; two different years, or a count such as "in 2000 or more",
  state no window and are left over. A window you pass is never held to a lone "previous month"
  when the draft carries a `prior_period` expression.
- Keep plan drafts at low confidence when a time grain or request word masks an omitted
  catalog grouping; recognize selected plural names and count-valued snapshot measures
  consistently when checking readiness.
- Validate semantic policy kinds and actions against their supported values in
  every package, and refuse invalid policies before query compilation or execution.
- Return TIME and BYTEA results through the Postgres ADBC adapter with
  exact values and their logical result types.
- Refuse Postgres TIME values outside Python's exact clock range, including
  `24:00:00`, instead of silently wrapping them to midnight.
- Postgres interval results now use the same exact duration values and JSON
  interval metadata as DuckDB, including its 30-day month convention. Values
  beyond Python's duration range or microsecond precision refuse explicitly.
- SQL seed and CSV post-load scripts reject bare carriage returns with an error
  naming the source file before executing any script statement. CRLF line
  endings preserve and execute every statement like LF line endings.
- Postgres refuses unsupported result column types with `RESULT_TYPE_UNSUPPORTED`
  instead of returning nested NUMERIC or JSON/JSONB values as strings. Supported
  scalar results retain their exact types, including NUMERIC as `Decimal`.
- A `metric_predicate` threshold that zero satisfies (`= 0`, `< 3`, `<= 0`, `!= 1`) now counts the
  entities that have no rows, as 0, when its input is a count or a sum (or an add/subtract of
  them) and the measure has data somewhere in the predicate's scope; with none, no entity
  counts. "Customers with no orders"
  and "members with zero activity" used to return 0 because those entities never reached the
  aggregate. An average, minimum, maximum, median or ratio over no rows is NULL, so an entity
  with no rows never satisfies a threshold on one ("average order value under 20" keeps only
  customers with orders). Conversion metrics and anchored `scoped_aggregate` ratios refuse, for now,
  a count or sum threshold that zero satisfies.
- Retain each named value in a compound filter phrase, including in ranked
  questions.
- Preserve distinct requested groupings in catalog fallback, deduplicating
  only identical dimension IDs and retaining the user's discovery terms.
- Ask for clarification whenever the caller passes `group_by` and the draft
  adds a grouping dimension the caller didn't pass: the plan keeps both
  groupings and returns `low_confidence` until `group_by` names every intended
  dimension ID.
- Two relationships between the same pair of entities (for example a leg's origin and
  destination airport) are now both kept. Previously the loader kept only one, and a query
  that reached the airport could silently return origin or destination values depending on
  declaration order. Every such query is now refused with `AMBIGUOUS_PATH` naming the routes
  and how to pin one: the airport's city, its key column, a filter on either, and a metric
  predicate on the airport, including when one role joins to a non-key column of the airport. A
  `graph.path_preferences` row pins the role for queries from its source entity to its target
  entity. Parsing the package warns with `ROUTES_UNDECIDED`, for every undecided pair a question can need. A pinned
  role reads the airport's key through the pinned relationship's join, so a leg whose code matches
  no airport groups under a NULL key. When a `graph.path_preferences` row exists for the pair (even one
  that names a route through another entity), the key is read through path selection too, so a
  row never pairs one airport's city with another airport's code. Two authored `graph.relationships` entries on the same `via` columns
  are refused at load instead of one silently replacing the other. When several
  relationships join one pair, a rollup aggregation must be allowed by every one that lists any.
  `upsert_relationship` refuses a pair that may have several roles instead of rewriting one of them.
- Keep route notes fast on densely connected packages with recorded routes,
  without enumerating all alternatives to produce an informational note.
- Single-argument and/or are refused before SQL, including negated forms and NULL
  arguments, consistently across configured measures, post-aggregation expressions,
  and relations. Zero-argument forms are also refused; use at least two arguments.
- Render NOT(NULL) with a nullable boolean cast
  on ClickHouse, so projections and comparisons work with default cast settings.
- Preserve significant whitespace in Snowflake ADBC key passphrase files,
  removing only one optional trailing LF or CRLF line terminator.
- Apply declared temporal-validity joins when grouping or filtering by a history
  key reached by a hop into the validity window, including NULL for missing versions;
  require a query time even when the source has a matching key column. Hops out of
  the table holding the window keep the source-key shortcut.
- Distinguish Jaffle Shop's historical customer key from its customer key when
  planning all-time customer rankings.
- Resolve relative time ranges using the temporal role's local date, so equivalent
  UTC and offset timestamps produce the same bounds. Refuse coarse relative periods
  on non-default calendars; use exact dates for those periods.
- Apply the same whole-day bounds to DATE clocks and calendar fill, including an
  end day when the exclusive end falls after midnight, after timezone conversion
  and when a snapshot or population clock differs from the query's time axis.
- Convert DATE clocks from midnight in their declared storage zone on DuckDB,
  MotherDuck, DuckLake and Postgres, preserving both local days across timezone
  boundaries whatever the session zone. Other warehouses keep their existing
  conversion SQL.
- Apply entity-only predicate windows using valid logical field filters on
  unconverted roles. On roles requiring timezone conversion, refuse entity-only
  predicate windows, contextual metric predicates joined on its time period, and
  conversion metrics queried on it, rather than comparing local bounds or buckets
  against raw stored values.
- On DuckDB and Postgres, empty time buckets use observation outside bounded query windows
  and remain NULL outside loaded base coverage. Coverage gates only zero filling and
  preserves populated values, including NULL time keys and future dates. Its current-time
  cap is the only instant comparison: it compares UTC instants independently of the session
  zone and honors naive columns' storage zones, while buckets, calendar joins and window
  filters keep each leaf's own time frame. Snowflake, BigQuery, Databricks, Athena and
  ClickHouse keep the in-window test until their coverage SQL has execution evidence.
- On DuckDB and Postgres, filled, dense-series (rolling and prior-period) and combined
  queries, bounded or not, read base relations so available rollups cannot change their
  coverage answers. Performance guidance includes the unbounded coverage and observation
  reads, which respect policy row filters.
- Time-series results default to ascending time order on every warehouse,
  including filled series, with grouped dimensions breaking ties in their stated
  order. This default applies only to the request's final projection, keeping
  internal branches and predicate sources unordered. Explicit ordering continues
  to take precedence.
- A query that groups, filters or otherwise reads through a many-to-one relationship into a
  table holding a `temporal_validity` window, and has no `time`, is now refused with
  `FANOUT_UNSAFE`, naming the relationship and the entity, instead of joining every version of
  the far row and counting a row once per version: a customer with two segment versions no
  longer adds its amount to both segments, so grouped rows add up to the ungrouped total again.
  Add `time` so each row reads the version valid at its time; queries with a `time` answer as
  before, and a hop out of the table holding the window needs no time, nor do two measures
  selected together, which are aggregated on their own. This covers group-by and where
  dimensions, measure filters, dimension-only queries, conversions, metric predicates and live
  valid-values lookups. `discover`, `inspect`, `build-options` and `plan` answer as before, so
  a dimension they list may still need `time` when the query is compiled.
- A validity window qualified with a schema (`analytics.customer_history.valid_from`) now joins
  on that column when the query has a `time`, and a hop out of its table still needs none.
- `plan` is no longer ready when the draft adds a grouping the question never asks for: "food
  revenue vs drink revenue by store and customer type" split by month is held with
  `why.code="PLAN_UNASKED_GROUPING"` and the month in `why.details.unasked_groupings`. A grain
  traces to the question's words outside its windows ("by month", "monthly", "over time"), to
  the caller's `partial_query`, or to a window that fits in one bucket; a dimension traces to a
  grouping the question asks for ("by store", "which 5 stores", "per store", "for each store"),
  to the caller's group_by, or to a filter on values the question names. So a comparison, a
  year-over-year shift or a qualified ranking that buckets by month, and a window of several
  periods split into them ("revenue last 7 days" by day, "revenue in 2016 and 2017" by year),
  are held until the question names the grain. The check only holds a plan: the draft and every
  other plan are unchanged.
- A ranking split by a period the question names ("top 3 stores by revenue at month level") is
  no longer ready with the top 3 store-months. It is held with
  `why.code="PLAN_RANKING_PERIOD_AMBIGUOUS"`: its message asks whether the question means the
  top 3 overall or the top 3 in each month, and its hint says to ask the user which ranking is
  meant. Any other ranking that keeps more than the ranked entity ("which 3 stores have the
  highest revenue by customer type" keeps the top 3 store and customer type pairs, a ranked
  month) is held with the same code. Neither hold carries a runnable option.
- The listed-grouping check reads a window inside the list as a comma, so "repair cost by
  incident name, last month and incident" grouped by the incident name alone is held instead of
  adding two incidents that share a name into one row.

### Security

- Connector advisory exceptions require a verified upper-bound cap below patched
  releases, exclusion of every reported patched version under packaging specifier
  rules, and an active direct blocker present in the audited dependency surface.
- Update the locked urllib3 and PyJWT dependencies to patched releases for published
  security advisories. The Databricks connector still requires oauthlib below 4.0,
  leaving CVE-2026-49265 unresolved for the `databricks` and `all` extras. Track this
  connector-only exception with a 30-day review limit and automatic rejection
  when the connector's latest PyPI dependency metadata permits any advisory-reported
  patched version, including backports. Audit every published extra and require
  `all` to equal their union; core advisories remain a hard gate.
- Validation recovery hints, near-match suggestions, diagnostics and error message
  text omit hidden dimensions and respect catalog visibility,
  withholding alternatives when the caller's policy context cannot be resolved.
  Alias and calendar alternatives follow the same check. Package authoring retains
  full reference suggestions.
- Intent planning respects dimension visibility before choosing groupings, including
  catalog fallback, Intent IR and diagnostic hints in every response detail mode.
- Refuse nonempty authored Snowflake tags on named-profile connections before
  connecting, removing manual tag SQL while preserving profile session settings.

## 0.3.2rc2 — 2026-09-26 — Embedding seams and zone-aware time buckets

**Pre-release.** Install it with `pip install semantic-rails==0.3.2rc2`; `pip install
semantic-rails` and `<0.4` ranges keep resolving 0.3.1.

**Upgrading from 0.3.2rc1** (from 0.3.1, read the 0.3.2rc1 note below first): check these
changes; each has its entry below.

- **Zone-aware time columns bucket in their time role's zone** on DuckDB, MotherDuck, DuckLake
  and Postgres (Fixed). A `TIMESTAMP WITH TIME ZONE` column's buckets at every grain, and its
  window filters, follow the role's `timezone` (UTC without one) instead of the session zone:
  the machine's local zone on DuckDB, the server's `TimeZone` on Postgres. So answers change
  wherever the two differ, for a role with a non-UTC `timezone` and for a UTC role on a machine
  or server that isn't on UTC: an event near a bucket boundary can move to the neighbouring day,
  week or month, and a window's totals can change. Authored SQL that depends on the session
  zone follows the role's zone too (`now()`, `current_date`, a `call` over a zone-aware value),
  and zone-aware values in rows show its offset. A query whose measures use time roles in other
  zones returns a new `TIME_ZONE_NOT_APPLIED` warning. Naive `TIMESTAMP` and `DATE` columns
  bucket and filter as before, and other warehouses are unchanged. To reproduce an answer in a
  SQL console, set `TimeZone` to the role's zone first.
- **`certify_aggregate_relation` no longer certifies a rollup under a role whose `timezone`
  isn't `UTC` or `Etc/UTC`** (`timezone_not_utc`, on every warehouse; Fixed). A host that
  certifies with it stops certifying such a rollup, so its queries run on the base tables. On
  DuckDB, MotherDuck, DuckLake and Postgres, build a rollup you certify, and run its paired
  queries, with the session time zone set to UTC.
- **A distribution beside a `rolling` or `prior_period` item** returns each period once on
  Postgres and BigQuery, where 0.3.2rc1 returned every period twice (Fixed).
- **Embedding hosts:** nothing is removed. `semantic_rails.embedding` gains the Architect
  authoring seams, `handle_streamable_http_request`, the `MCPAdapter` protocol, and
  `SemanticLayerMCPAdapter.replace_tool_handler` to use instead of the private
  `_tool_handlers` mapping (Added). A warehouse adapter, including one set with
  `Runtime.set_adapter`, now also finds the zone the query runs in under `limits["time_zone"]`:
  one that forwards `limits` to a built-in adapter gets the fix above, and one that rejects keys
  it doesn't know must accept this one. A request's own `limits` still take only
  `statement_timeout_ms` and `max_rows`.

### Added

- `semantic_rails.embedding` exports the package authoring seams `ArchitectProject`,
  `ArchitectMutation`, `project_revision`, `ABSENT_PROJECT_REVISION` and `impact_report`, and
  the stateless Streamable HTTP handler `handle_streamable_http_request` with its
  `MCPHTTPResponse`. See "Serving MCP over HTTP" and "Package authoring" in
  [docs/EMBEDDING.md](docs/EMBEDDING.md).
- `handle_jsonrpc_message` and `handle_streamable_http_request` accept any adapter that
  satisfies the new `MCPAdapter` protocol, so a host's own adapter type-checks.
- `SemanticLayerMCPAdapter.replace_tool_handler(name, handler)` swaps the body of one tool on
  one adapter and keeps its argument validation, trusted request context and audit event; an
  exception the handler raises becomes a tool error response, as for the built-in tools. Use it
  instead of the private `_tool_handlers` mapping, which keeps working through the 0.3 series.
- [docs/EMBEDDING.md](docs/EMBEDDING.md) ends with a reference of every facade name and its
  call shape, checked against the facade by a test.

### Fixed

- A long-running process that authors many package directories no longer keeps one Architect
  lock per directory in memory: a project's in-process lock is dropped once no thread holds or
  waits for it.
- A distribution beside a `rolling` or `prior_period` item no longer returns every period twice
  on Postgres and BigQuery. The items are answered separately and joined on the period, which one
  item typed as a date and the other as a timestamp; the join now compares both as the
  warehouse's timestamp, so each period is one row with every item's value.
- A `TIMESTAMP WITH TIME ZONE` time column on DuckDB, MotherDuck, DuckLake and Postgres now
  buckets and filters in its time role's `timezone` (UTC by default) at every grain. Before,
  day and week answers, and month answers from the base table, followed the server's or
  machine's session time zone, so they could disagree with each other and with a rollup built
  in UTC. Each query now runs with the session time zone set to its time role's zone (UTC
  without one), only for that query, on the engine's connection or a host's. Everything
  zone-dependent in the query follows that zone: authored `call` expressions over zone-aware
  values, `now()` and `current_date`, and the offset shown on zone-aware values in rows. A query
  whose measures use time roles in other zones returns a `TIME_ZONE_NOT_APPLIED` warning. Naive
  `TIMESTAMP` and `DATE` columns are unaffected, and other warehouses are unchanged; see
  "`times:` — temporal roles" in [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
  On every warehouse, `certify_aggregate_relation` no longer certifies a rollup under a role
  whose `timezone` isn't UTC (`timezone_not_utc`), so those queries use the base tables. On
  DuckDB, MotherDuck, DuckLake and Postgres, a host builds a rollup it certifies, and runs the
  paired queries, with the session time zone set to UTC.

## 0.3.2rc1 — 2026-09-26 — Exact rollups, row filters, Ossie and a v2-only query MCP

**Pre-release.** Install it with `pip install semantic-rails==0.3.2rc1`; `pip install
semantic-rails` and `<0.4` ranges keep resolving 0.3.1.

**Upgrading from 0.3.1:** run `semantic-rails project validate --mode parse` on your packages
first, and check these changes; each has its entry below.

- **Query MCP interface v1, 0.3.1's default, is removed** (Removed). Move clients to v2's six
  tools. v2's defaults are smaller: `execute` returns at most 200 rows, `discover` and `inspect`
  return slim cards (pass `verbosity: "compact"` for v1's), and `plan` returns
  `detail: "query"`.
- **Packages that 0.3.1 accepted can now be rejected:** a conversion metric whose operands both
  count the conversion entity on the same clock (Fixed); a rollup binding with a key the engine
  doesn't know, a `holds` the measure can't be queried with, or an unknown relationship in
  `path` (Changed); and a policy that looks like a misspelled `row_filter` (Added).
  `validate runtime` also runs each segment's preview query, so it can report a segment
  membership value the warehouse can't compare with its column (Changed).
- **Some answers change, and some queries are refused:** a conversion `window` is now a
  duration after the base event, so rates can drop (Changed); a query on the default calendar in
  a package that declares only non-default calendars fills from an implicit Gregorian calendar
  instead of borrowing another calendar's periods, and ClickHouse refuses it until the package
  adds a default calendar (Fixed); queries a rollup can't answer exactly run on the base tables
  instead (Fixed); and a `distribution` with `time.fill: true`, or with a `rolling` or
  `prior_period` window in its input or metric filter, is refused instead of returning wrong
  values (Fixed).
- **`plan` returns `low_confidence` where 0.3.1 returned `ok`** when several measures or
  metrics tie and the question names none of them (a `subject_ambiguous` gap; `semantic-rails
  ask` then exits 1 without running, so name one in the question), and for a fiscal question it
  can't put on the package's fiscal calendar, which 0.3.1 answered with Gregorian periods;
  fiscal windows such as "FY2017" return `TIME_WINDOW_UNRESOLVED` with no runnable draft
  (Fixed). "Number of customers" in the bundled demo now answers Customer count.
- **Embedding hosts:** `MCP_INTERFACE_VERSION` and the other query MCP interface constants are
  gone from `semantic_rails.embedding` (Removed); `semantic_rails.cache.compilation_cache_key`
  takes a required `aggregate_routing` argument (Added); and importing the audit sink and
  API-key helpers from `semantic_rails.request_context` is deprecated and stops working in
  0.3.3 (Deprecated).

### Added

- The compile plan's `performance_plan.aggregate_routing.candidates` lists every
  declared rollup considered for each measure leaf, whether it was `selected`,
  `eligible`, `rejected` or `unknown`, and why, and `aggregate_routing.selected`
  lists every rollup the compiled SQL reads, including a `distribution`'s
  separately compiled branches. Setting
  `SEMANTIC_RAILS_AGGREGATE_ROUTING=off` (or calling
  `runtime.set_aggregate_routing(False)`) runs every query a runtime serves on the
  base tables, including queries whose compiled plan is already cached.
- `semantic_rails.cache.compilation_cache_key` takes a required `aggregate_routing`
  argument, so a custom compile cache keys on the routing switch. Plans cached
  before the upgrade miss once.
- The comparison pack adds eight frozen-model questions (q17-q24). Each changes one parameter
  of a metric the first 16 questions use (a conversion window, a rolling window, a period
  offset, a per-metric filter, an aggregation or a threshold), and every layer answers it with
  its model unchanged, through its query-time interface only. The rubric gains a
  `requires_model_change` label, which `comparisons/semantic_layers/shared/frozen_model.yml`
  backs with a reason and a documentation link, and the published matrix gives each layer's
  count answered with the model frozen. Cube's runner now also sends SQL API queries through
  `/cubesql`, so Cube's start script opens its SQL API port (15432, on every interface) with a
  random password that no client receives.
- `docs/EMBEDDING.md` describes how changes to `semantic_rails.embedding` are staged:
  the new form ships next to the old one, the old one is deprecated with a named removal
  release, and it is removed only after embedders have moved. The test suite now checks
  the facade names, attributes, call shapes and implemented protocols recorded from a
  known embedder's code.
- `rolling`, `prior_period` and `time.fill` now work in a package that declares no calendar:
  a query on the default calendar fills its periods from an implicit Gregorian calendar the
  engine generates in SQL (calendar months, quarters and years, Monday weeks, in the time
  role's zone), spanning the query's window or the data's first to last period. They used to
  fail with "time.fill requires a calendar entity in the package". An authored calendar still
  fills when the package has one; any other `calendar_id`, such as a fiscal calendar, still
  needs its calendar authored and is refused without it. Not available on ClickHouse. See
  [docs/QUERY_IR_SCHEMA.md](docs/QUERY_IR_SCHEMA.md).
- `semantic-rails export --format ossie --output DIR` writes a package as an Apache Ossie 0.1.1
  document (`<package-id>.ossie.yaml`) that passes the spec's validator, plus a sidecar
  (`<package-id>.semantic_rails.json`) holding what Ossie 0.1.1 can't express. Each such construct
  gets a counted warning. Metrics, measures and relationships Ossie can't state faithfully are left
  out of the document instead of approximated, and semantic policies carry an extra warning that
  Ossie consumers won't enforce them. See [docs/OSSIE.md](docs/OSSIE.md).
- `semantic-rails import --from ossie` reads a document written by `export --format ossie` back
  into a package. With the sidecar the package comes back exactly, checked by exporting it again,
  and a document edited since the export is refused. Without the sidecar, datasets, column fields,
  joins to a primary key and metrics in the export's aggregate SQL are imported, with the types
  and aggregations it defaults counted in warnings, and anything else is skipped with a warning.
  A refused import leaves no files behind. Reading other Ossie 0.1.x and 0.2 documents is
  experimental. See [docs/OSSIE.md](docs/OSSIE.md).
- Compiled statements can carry typed parameter slots that the engine binds per request from
  the host's `TrustedAttributes`. DuckDB receives the values through its own parameter
  binding, never in the SQL text; every other warehouse adapter refuses such statements, and a
  missing or mistyped attribute is denied. Requests with different attribute values no longer
  share a compile-cache entry. `row_filter` policies produce the parameters. See
  [docs/ADDING_A_DIALECT.md](docs/ADDING_A_DIALECT.md).
- A rollup can declare `requires_certification: true`. It then routes only while
  the host's certification provider, installed with
  `semantic_rails.acceleration.routing.set_certification_provider`, says it is
  certified. With no provider it runs on the base tables (`not_certified`). A
  package with such a rollup skips the compile cache and compiles every request,
  so a revoked certification applies to the next one.
  `semantic_rails.acceleration.certification.certify_aggregate_relation(config,
  relation_id)` returns the engine's verdict on each of a rollup's measure columns
  with a paired base and rollup query to compare before certifying it. Rollup rows
  in validation metadata gain a `requires_certification` field.
- A rollup's measure column can declare what it holds per row with `holds:`
  (`sum`, `min`, `max` or `count_distinct`), so `min` and `max` queries can run on
  a rollup. A column declared `holds: count_distinct` also answers a distinct count
  of a key other than the row key at the rollup's own time grain, when every
  rollup dimension is grouped or pinned by an `=` filter. A column without
  `holds:` keeps its meaning: a sum for an `aggregate` measure or a distinct count
  for an `entity_count` measure, re-added with `SUM`. A dimension column
  pre-joined from another model declares the relationship `path:` it was built
  along, and routes only when the query joins along that same many-to-one path.
- A new `row_filter` package policy limits a relation's rows to a trusted request attribute,
  for example each customer's own orders: the compiler adds `<column> = ?` and the runtime
  binds the host's `TrustedAttributes` value. Only DuckDB executes these statements today;
  other adapters refuse them. A missing or mistyped attribute is denied on every surface, as
  is any query that reads more than the one filtered relation (joins, metric filters, calendar
  spines); rollups aren't routed to under a row filter, and the zero-row coverage probe is
  skipped. The package loader rejects a row filter it can't express, and a policy that looks
  like a misspelled one. A warehouse error on a parameterized statement is now raised without
  the driver's message. See [docs/PACKAGE_AUTHORING.md](docs/PACKAGE_AUTHORING.md).
- Embedding hosts can attach typed, immutable `TrustedAttributes` (for example a customer ID from a
  verified token) to a `RequestContext`, imported from `semantic_rails.embedding`. The engine
  carries them through every transport and internal call; request bodies, headers and plans
  can't set or replace them, and they never appear in the public `request_context`, echoed
  queries, errors or audit events. `row_filter` policies read them. See
  [docs/EMBEDDING.md](docs/EMBEDDING.md).

### Changed

- The API-key helpers (`api_key_auth_result`, `configured_api_keys`,
  `extract_bearer_or_api_key`) moved from `semantic_rails.request_context` to the new
  `semantic_rails.api_keys` module. Importing them from `semantic_rails.request_context`
  still works.
- Package validation warns when a measure duplicates another one (same entity, expression,
  aggregation and default clock).
- An Architect write refused as stale now says that writes sent together with one
  `expected_revision` apply only the first, and names the revision to resend with; the
  Architect MCP instructions ask for one write at a time.
- The semantic layer comparison pack models MetricFlow (dbt-metricflow 0.15.0), Cube Core
  (1.7.45), Malloy (`@malloydata/cli` 0.0.57) and KtX (`@kaelio/ktx` 0.16.0) with the features
  their current releases ship, and re-runs them and Semantic Rails on the shared dataset. Cube
  runs live again from a locked, audited install. The rubric now labels MetricFlow, Cube and
  Malloy `native` on all 16 questions; every layer checked on the current data still matches
  the independent answer key. Snowflake Semantic Views remains a stale April capture.
- A conversion `window` is now a duration after the base event on every warehouse: a converted
  event counts when `base <= converted < base + window`, as in MetricFlow. It used to count
  unit boundaries, so a 7-day window ran to the end of the 7th calendar day (up to 8 days), a
  1-week window ran through the 13th day on DuckDB and Postgres, and a 1-month window covered
  all of the next month. Conversion rates can drop where converted events fell in that extra
  time; the shipped `jaffle_shop` conversion metrics are unchanged. See
  [docs/QUERY_API.md](docs/QUERY_API.md).
- The query MCP's `plan` and `execute` tool descriptions now say to draft Query IR with `plan`
  first and that `time.end` is exclusive, for hosts that don't pass the server instructions to
  the model. `plan`'s `next.ready_for` now lists only `execute` (over HTTP and the CLI too), and
  recovery hints that name a follow-up call say which MCP call it is, for example "validate it
  (over MCP, execute with mode 'validate')", on every surface.
- Rollup bindings are checked when a package loads. A measure binding (a variant
  `columns:` entry or an `aggregate_relations:` measure) accepts only `column`,
  `rollup`, `aggregation` and `holds`, and a dimension binding only `column` and
  `path`. Any other key, a `holds` the measure can't be queried with, or an
  unknown relationship in `path` is now `INVALID_CONFIG`, where before it was
  ignored. An `aggregate_relations:` entry that holds a column from another model
  without a many-to-one `path` no longer routes at all.
  `performance_plan.aggregate_routing.selected_count` now counts the distinct
  rollups the SQL reads, not the rollup scans in the physical plan.
- `validate runtime` (and `project validate --mode runtime`) also runs each segment's
  preview query, so a membership value the warehouse can't compare with its column is
  reported, with a hint, before `segment preview` fails. `segment validate` stays
  warehouse-free: for a membership value on a text, id, date or time dimension, which it
  can't check against the column, it adds a `SEGMENT_VALUES_UNCHECKED` warning that names
  those commands.

### Deprecated

- The audit sink (`AuditSink`, `StderrAuditSink`, `audit_logging_enabled`,
  `emit_audit_event`, `get_audit_sink`, `set_audit_sink`) moved from
  `semantic_rails.request_context` to the new `semantic_rails.audit` module, and
  `semantic_rails.embedding` now also exports `audit_logging_enabled`. Importing these names,
  or the API-key helpers that moved to `semantic_rails.api_keys`
  (`MISSING_API_KEY_FILE_SENTINEL`, `api_key_auth_result`, `configured_api_keys`,
  `extract_bearer_or_api_key`), from `semantic_rails.request_context` is deprecated and stops
  working in 0.3.3. Import them from their new modules (hosts: the audit names from
  `semantic_rails.embedding`), and install a sink with `set_audit_sink` rather than patching
  module state.

### Removed

- **Breaking:** query MCP interface v1 is removed. Interface v2 is the only one: six
  tools (`discover`, `inspect`, `valid-values`, `plan`, `execute` and `segment`) and one
  contract, `query_mcp.v2.json`; `query_mcp.v1.json` is no longer shipped. Setting
  `SEMANTIC_RAILS_MCP_INTERFACE=v1` or passing `interface="v1"` fails with "The v1 MCP interface
  was removed; v2 is the only interface" (`mcp stdio` returns it as the error of the client's
  `initialize` and logs it to stderr), and calling a v1 tool returns `UNKNOWN_MCP_TOOL` naming
  its replacement. The Architect MCP's `preview_query` builds a query adapter, so it fails the
  same way under that setting, and its results report `api_version` `v2`.
  `MCP_INTERFACE_VERSION` and the other interface constants are gone from
  `semantic_rails.mcp` and `semantic_rails.embedding`. Upgrading from v1:
  - `validate` → `execute` with `mode: "validate"`; `compile` → `execute` with `mode: "sql"`.
  - `segment-validate`, `segment-explain`, `segment-preview` → `segment` with `action`
    `validate`, `explain` or `preview` (`verbosity: "full"` for the whole response).
  - `catalog` → `discover` with empty `terms`, or the `semantic-rails://catalog/*` resources.
  - `capabilities`, `build-options` → draft Query IR with `plan`. The HTTP API keeps both,
    and the CLI keeps `build-options`.
  - Smaller defaults: `execute` returns at most 200 rows (`max_rows` up to 100,000);
    `discover` and `inspect` return slim cards (`verbosity: "compact"` for v1's); `plan`
    returns `detail: "query"` (`detail: "best"` for v1's).
  See [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md#migrating-from-interface-v1).

### Fixed

- Segments on a true/false column work from `author segment` to preview. `author model`
  declares a BOOLEAN column as `kind: boolean`, not `categorical`; the segment wizard
  offers true and false for it, types every other value by its dimension (`'completed'`
  means the text completed), asks again for a value of the wrong type, offers only
  entities that can hold a segment and that entity's own metrics, and starts an edit from
  the saved membership field, so Enter at every prompt keeps a segment it wrote.
- A segment or query filter value of the wrong type for its dimension fails validation
  with a recovery hint, and a segment preview the warehouse refuses carries a hint about
  membership values and dimension kinds.
- `author model` on a package whose seed files aren't built into its DuckDB file yet
  names `validate runtime`, which builds it, instead of `dbt build` (a dbt-built package
  still names `dbt build`).
- The bundled sample package, installed from a wheel, builds its DuckDB file in
  `~/.semantic_rails/cache/` (or under `SEMANTIC_RAILS_HOME`), one per installed version,
  not in `site-packages`.
  A `site-packages/data/jaffle_shop.duckdb` left by an earlier version, and a folder under
  `~/.semantic_rails/cache/jaffle_shop/` for a version no longer installed, can be deleted.
- REPL polish: `author model` recommends the largest unmodeled table and doesn't
  pre-tick rank or sequence-number columns as summed measures (the Architect's
  `suggest_model` marks them low confidence); "Model to extend" lists calendars last;
  the filtered-metric wizard offers true and false for a boolean dimension; the growth
  recipe's example question plans without a warning, and labels keep MoM, YoY and YTD;
  `help` lists `help [command]`; `ask` prints its warnings before the rows; and
  day-or-coarser time buckets print as dates.
- The semantic layer comparison pack's published Cube SQL excerpts show Cube's generated SQL
  instead of its first character, and Cube's baseline join count matches its baseline files.
  Each layer's listed weaknesses now state how its q09 and q15 conversion window's boundaries
  differ from the stated rule. The Cube runner no longer passes the caller's environment to
  Cube, the KtX runner caches its wheel in the pack instead of a shared `/tmp` directory and
  imports only the bytes it checked, and CI fails if Cube's start script loses its dev-server
  guards.
- A conversion expression without a supported `matching_mode` now fails with an error that
  names the parameter, lists `first_converted_after_base` and `closest_converted_after_base`
  with what each matches, and returns the sent expression with a mode set. The window error
  lists the supported units. A conversion metric's `inspect` card now shows its own
  expression under `conversion`, so the same conversion can run over another window, such
  as 50 minutes instead of 7 days, without a new metric.
- A conversion metric whose base and converted operands both count the conversion entity
  itself on the same clock (for example a 90-day repeat-purchase rate counting customers
  instead of orders) is now rejected by package validation and at query time. Each entity
  was a single event that converted to itself, so the window never applied and the metric
  returned the share of entities that ever matched the converted filter. The error names
  measures that count events keyed by the entity, such as orders.
- A query on the default calendar no longer fills from another calendar when the package
  declares only non-default ones (for example only a fiscal calendar). It borrowed that
  calendar's periods, so fiscal quarters and years missed every Gregorian period and read 0;
  it now uses the implicit Gregorian calendar. Answers change for such packages, including
  bounded `fill` windows, which now follow the rule for a calendar whose `date_day` is a
  `date`. On ClickHouse, which has no implicit calendar, such a query is now refused until the
  package adds a default calendar.
- A `distribution` with `time.fill: true` returned wrong values: every entity entered every
  period as a `0`, so a monthly median or percentile read 0 or too low (for example 3.0
  instead of 9.0). Such a query is now refused; without fill it answers as before, omitting
  periods with no data. A `distribution` whose input or metric filter has a `rolling` or
  `prior_period` window, which counted entities in periods where they had no rows, is refused
  too. See [docs/QUERY_IR_SCHEMA.md](docs/QUERY_IR_SCHEMA.md).
- `semantic-rails ls` accepts the REPL's `ls [kind] [search]` form, for example
  `semantic-rails ls metric revenue`.
- `author model` warns when the seed files changed after the DuckDB file was built, with
  the command that rebuilds it, and refuses a typed table name the file lacks.
- `author model` no longer pre-ticks `_cents` columns as money amounts, which printed cents
  as dollars.
- The authoring banner says that typing `cancel` in a list picks an option containing it.
- The query MCP `discover` schema no longer advertises a `limit` default, so a client that
  fills in schema defaults gets 100-id pages for empty `terms`, not 10. Two recovery hints that
  pointed MCP agents at HTTP routes now name `discover`.
- Query MCP interface v2: `discover` with empty `terms` lists at most 100 ids
  per kind at a time, so its response stays bounded on large packages. `limit`
  and `offset` page the ids, and a `DISCOVER_IDS_TRUNCATED` warning says which
  kinds have more. An unknown tool name on v2 now gets a hint that names only
  v2 tools, not `validate` and `compile`.
- `plan` answers "number of customers" and "how many customers" with Customer count. The
  words "number" and "of" tied it with measures described as "Number of …", and the tie went
  to Active menu count by label; a measure named for what the question counts, plus "count",
  now counts as the one the question names.
- `plan` counts a fiscal question's time on the fiscal calendar. "Revenue by fiscal quarter"
  came back as Gregorian quarters with status ok, and "vs prior fiscal quarter" was dropped
  with status ok. With one calendar whose name says fiscal, `plan` puts a question that asks
  for fiscal buckets ("by fiscal quarter") on it (`time.calendar_id` with `time.fill: true`).
  Any other fiscal period ("the first fiscal quarter"), or a package without such a calendar,
  returns `low_confidence` with a `fiscal_calendar_unrealized` gap, and a dropped fiscal
  comparison is reported like any other. A fiscal question's window resolves only from exact
  days: "fiscal Q2 2017", "FY2017" and "last fiscal quarter" return `TIME_WINDOW_UNRESOLVED`
  instead of the Gregorian period of the same name.
- `plan` with a partial query it can't read (`group_by: [["dimension.x"]]`) returns
  `INVALID_QUERY` with a recovery hint instead of an internal error, and a select item passed
  in `query` appears once, under the caller's alias, instead of again under the draft's.
- When several measures or metrics match a question equally well, `plan` now picks the one
  the question names: "What is revenue by month?" uses a measure labelled Revenue, not Item
  Revenue Cents, which used to win on alphabetical order. When the question names none of
  them (Gross Revenue and Net Revenue for "revenue"), `plan` returns `low_confidence` with a
  `subject_ambiguous` gap that lists the candidates, and `ask` says which to name.
- These queries, which a declared rollup can't answer exactly, now run on the base
  tables instead of returning a wrong number: distinct counts of anything but the
  single-column row key of a model that isn't a fact model, weekly rollups asked
  for months, quarters or years, time ranges that don't start and end on the
  rollup's bucket boundaries (day boundaries for minute and hour rollups), time
  roles that convert time zones, non-default calendars, rollups that declare their
  own `filters`, aggregates filtered by a `metric_predicate`, stock
  (semi-additive) measures, aggregations other than the one a rollup column holds,
  dimensions pre-joined into a rollup along a join path other than the query's (or
  with no declared `path`), rollups with a pre-joined column asked a query that
  doesn't use it, `aggregate_relations:` entries without a `temporal_role`, and
  measures or time roles read from another model.
  The logical plan's `aggregate_relation_rejections` says why each rejected rollup
  wasn't used. The engine still trusts the rollup's author on what it can't see
  in the tables: a weekly rollup is built on Monday-start weeks, a pre-joined
  column is built with an inner join as the base path joins it, and a declared
  distinct count has one row per time bucket and dimension.

## 0.3.1 — 2026-09-25 — Honest plans, clock-safe metrics and a REPL calendar

**Upgrading from 0.3.0:** validation and planning are stricter; run `project validate --mode parse`
on existing packages. A metric that names an undefined measure or metric now fails validation, as
does a non-conversion metric whose declared clock one of its multi-clock measures lacks, and a query
on such a clock fails with `INCOMPATIBLE_TEMPORAL_ROLE`. `plan` no longer returns a runnable draft
on `TIME_WINDOW_UNRESOLVED`. Details are under Fixed.

### Added

- `mf2sr --schema-strict`, and `semantic-rails import --from metricflow --schema-strict`, write a
  `schema_strict: true` package whose relations keep the schema and database that dbt's
  `semantic_manifest.json` records, named the way `import_dbt_project` names dbt relations. A DuckDB
  package reads the database dbt built (`seed: {kind: external}`), and the output is parse-checked.
  See [mf2sr/README.md](mf2sr/README.md).

### Changed

- The Architect MCP's tool list is a fifth smaller: its schemas no longer carry a title for every
  property, and the workflow and write contract moved into the server instructions. Every tool now
  has a title and hints, including `openWorldHint` on the checks that query the warehouse. See
  [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).

### Fixed

- A retried Architect `write_project_file` with `overwrite: false`, or a retried
  `archive_project_file`, now replays the first call's result instead of failing because the first
  call already wrote or archived the file.
- On Linux, `semantic-rails mcp start`, `mcp status` and `mcp stop` identify a managed server
  by the kernel's process start tick instead of `ps`'s start time, which can shift by a second
  when the system clock is stepped. `mcp start` no longer reports `failed_to_start` for a
  healthy server, and `mcp stop` no longer refuses to stop one. Servers started by an earlier
  version are still recognized.
- A query whose clock a metric's measure lacks no longer times that measure by the first
  of several clocks it has. For example, a ratio of order-line revenue (order and delivery
  clocks) over orders, queried on the order's delivery clock, divided order-date revenue
  by delivered orders and warned only `REWRITE_APPLIED`. The query now fails with
  `INCOMPATIBLE_TEMPORAL_ROLE`, naming the measure and its clocks; choose one with
  `temporal_role_overrides`. This includes a snapshot measure aligned to a calendar clock
  at month grain or coarser. A measure with a single clock is still aligned by it;
  conversion operands and `metric_predicate` inputs are unchanged. Package validation
  rejects a metric without a conversion whose declared clock would be refused this way.
- Package validation, including `project validate --mode parse`, rejects a metric that
  names a measure or metric the package doesn't define. Before, the package parsed and
  every query of the metric failed with `Unknown measure`.
- `plan` no longer returns a runnable `best.query_ir` when it can't resolve the question's time
  window (`TIME_WINDOW_UNRESOLVED`); executing that draft used to return every period with `ok`.
  Pass the window, temporal role and grain in `query.time` and plan again.
- A number in a time phrase no longer makes a top N: "What was revenue from January 1 2017 to
  March 31 2017 by store?" or "…in the last 3 months by store?" used to return only the first
  1 or 3 rows ranked by revenue.
- A question that names a metric whose measure has the same name ("rolling 28-day revenue by
  day", "Revenue QTD by day") now uses that metric instead of plain revenue.
- `plan` flags a second subject named with "count" or a question word ("What is order count and
  revenue by month?", "how many orders and revenue by month") instead of answering with revenue
  alone.
- `ask` and the REPL now print the plan's own warnings, such as `PLAN_UNMATCHED_TERMS`.
- The `UNGRAINED_TIME_PROJECTION` hint no longer suggests removing `time.temporal_role`, which
  the engine rejects. The MCP `max_rows` description says `total_row_count` is null past
  10,000 rows.
- The REPL's `author calendar` writes the package calendar, so rolling, prior-period and
  growth metrics can be authored without editing YAML; the metric wizard offers the units
  of the calendar that queries fill from.
- In the REPL, `ls` accepts `--limit N` and `--json`, a bare `ls` of a large package counts
  objects by kind, and `help <command>` shows that command. `ls --json` without `--limit`
  lists every object, in the CLI too, as the truncation hint says.
- `ask` and `run` print the engine's first recovery hint under each error.
- In an arrow-key list, typing `cancel` picks an option that contains it, such as
  "Cancelled orders", instead of ending the wizard.

## 0.3.0 — 2026-09-25 — Guided authoring, warehouse import and a leaner query MCP

### Added

- The Architect MCP's `upsert_model` takes `calendar: true`, with an optional
  `calendar_id`, to make a model the package calendar: its entity becomes
  `kind: time` and not a query root, and may carry date dimensions, so
  `time.fill` works in packages authored through the MCP.
- One project scaffold, shared by `architect_service.create_project(path, ProjectSpec)`
  (the Architect MCP and the REPL) and `semantic-rails init`. It is
  warehouse-aware: DuckDB packages get a starter CSV seed or read a database
  another tool builds (`data: external`, for dbt), and other warehouses get a
  `connection` block. Every package is strict and ships a `.gitignore`. The
  Architect MCP's `create_project` gains `warehouse`, `data`, `default_db`,
  `connection_*` and `dimension_column`, and `setup_project_dialog` asks for
  the warehouse and connection. A directory without authored files (one that
  holds only the warehouse dbt built) now has revision `absent`, so a package
  can be created in it.
- The Architect MCP's `upsert_example` and `upsert_test` tools write example questions and
  package tests, refusing entries the test runner can't check and queries that don't validate.
  `preview_query` returns up to 200 rows of a query. See
  [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
- Read-only warehouse introspection for authoring, in
  `semantic_rails.architect_introspection` and as Architect MCP tools:
  `list_tables`, `describe_table` (types, nullability, declared keys),
  `profile_columns` (counts, min/max, capped samples) and `suggest_model`
  (key, time, dimension, measure and foreign-key candidates, each with a
  confidence and a reason, plus draft `upsert_model` arguments). They read
  DuckDB databases, including one dbt builds, and never write to them.
- The Architect MCP's `upsert_metric` takes `file_name`, which puts a new metric in
  `metrics/<file_name>` so several metrics can share one file.
- The Architect MCP's `remove_object` removes a model, dimension, time, measure, metric, segment or
  foreign-key relationship in one parse-gated transaction, archiving the removed YAML. A model takes
  its entity and the relationships naming it along. A removal that would leave a metric naming what
  it removes is refused, and the report shows the `impact_project` summary and the files that still
  name a removed id. See [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
- The Architect MCP's `upsert_model`, `upsert_metric` and `upsert_segment` take `replace: true`,
  which rewrites the object from the arguments instead of merging into it. A model keeps its id,
  entity references and calendar binding, and the report lists what the rewrite dropped in
  `dropped_fields`. A metric or segment keeps its public id; `ArchitectProject.upsert_metric`'s
  `replace` used to drop it. `upsert_model` also takes `label` and refuses fact models. See
  [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
- The Architect MCP's `upsert_relationship` tool relates two entities through foreign-key
  columns: many-to-one in the model's `entities` block, or `one_to_one` recorded in
  `graph.relationships`. See [docs/ARCHITECT_MCP.md](docs/ARCHITECT_MCP.md).
- `semantic-rails ask` prints "Interpreted as: ...", a plain restatement of what the
  executed query computes: measures and metrics with their aggregation, grouping, time grain
  and window, filters and row limits. `--json` adds `interpretation`. If a question was
  misread, the restatement shows it instead of the answer looking fine.
- Other installed packages can add `semantic-rails` commands: each entry point in the
  `semantic_rails.cli` group receives the top-level subparsers and adds its commands. A
  plugin that fails to load or reuses a command name is skipped with a warning;
  `SEMANTIC_RAILS_CLI_PLUGINS=0` turns plugins off.
- `semantic_rails.dbt_artifacts` reads a dbt project's `manifest.json` and
  `catalog.json` (dbt never runs) and suggests a model per dbt model, keeping
  its schema-qualified relation: keys from contracts, `unique` + `not_null`
  and `unique_combination_of_columns` tests, foreign keys from `relationships`
  tests, value sets from `accepted_values`, and descriptions. The Architect
  MCP exposes it as `suggest_models_from_dbt`.
- The Architect MCP's `import_dbt_project` creates or updates package models
  from selected dbt models in one transaction (with dry run, revision and
  idempotency checks), writing each `relationships` test as an entity
  reference so the engine can join across them. `ArchitectProject.upsert_models`
  stages several models and their foreign-key references in one transaction.
- `package.seed.kind: external` declares a DuckDB database another tool (such
  as dbt) builds. It takes no `source`, and the runtime only reads it.
- Query MCP interface v2, opt-in with `SEMANTIC_RAILS_MCP_INTERFACE=v2` in the
  MCP server's environment (for example in the client config's `env`) or
  `SemanticLayerMCPAdapter(runtime, interface="v2")`: six tools instead of
  thirteen, served by the same handlers, each returning its smallest response
  unless asked for more. `execute(mode="run"|"validate"|"sql")` replaces
  `validate` and `compile`; `segment(action="validate"|"explain"|"preview")`
  replaces the three segment tools; `discover` with empty `terms` lists every
  id, replacing `catalog`. `discover` and `inspect` return slim cards, `plan`
  defaults to `detail="query"`, `execute` returns at most 200 rows (with
  `truncated` and `total_row_count` beyond that), and `segment` defaults to
  minimal responses. The `capabilities` and `build-options` tools are v1-only;
  calling a v1-only tool on v2 returns `UNKNOWN_MCP_TOOL` naming the v2 call. The contract
  is `query_mcp.v2.json`, and `initialize` reports the interface as
  `serverInfo.version`. Interface v1 is unchanged and stays the default.
- To move a v1 client to v2, call `execute(query, mode="validate")` for
  `validate`, `mode="sql"` for `compile`, `segment(segment_id, action=...)` for
  the segment tools and `discover(terms="")` for `catalog`; pass `max_rows` (up
  to 100,000) for more rows, `verbosity="compact"` for full `discover` and
  `inspect` cards, `detail="best"` for v1's `plan` response and
  `verbosity="full"` for v1's segment responses. See "Interface v2" in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
- Add `semantic-rails://catalog/index` for counts and ids per kind and
  `semantic-rails://capabilities/summary` for tool names and titles. These compact resources are
  opt-in. Existing v1 `catalog/summary` keeps its descriptive rows and `counts_total`, and
  `capabilities` keeps complete tool definitions for existing consumers.
- `mcp setup` and `mcp client-config` take `--client claude-code` and
  `--client cursor`. Claude Code servers are registered at user scope through
  `claude mcp add-json`; Cursor servers go into `~/.cursor/mcp.json`. `--client both`
  still means Claude Desktop and Codex.
- `plan` reports the parts of a question its draft doesn't honor. A dropped or different time
  window, a ranking that loses its stated limit, named measure, sort or ranked dimension (even
  when "top" or "bottom" has no count), a named filter value the draft drops or leaves out,
  a list passed to scalar `=`/`!=` instead of membership `IN`/`NOT IN`,
  or conjunctive filters that leave no value surviving downgrade the plan to
  `low_confidence` with `why.code="PLAN_INTENT_COVERAGE_GAP"`. Named values are
  matched to executable filter literals exactly after resolving question labels and
  aliases to their stored values.
  Every exclusion clause is checked, including later clauses after a correctly
  excluded value; a later reversal downgrades a validating draft.
  Question words the draft uses nowhere come back as a `PLAN_UNMATCHED_TERMS` warning.
- The REPL's `author metric` offers a filtered aggregate: one measure over only some rows,
  such as revenue from completed orders. Pick a dimension of the measure's model, "is one
  of" or "is not one of", and the values, from the dimension's declared domain or typed in.
  It is written as the aggregate expression with a filter, the same form the sample package
  uses.
- Reopening a supported filtered metric offers its saved dimension, operator, and values.
  Changing the measure or dimension requires selecting the new filter values. Expressions
  beyond this wizard's single-clause filter remain intact until a different recipe is chosen.
- At a terminal without a package, `semantic-rails repl` and bare `semantic-rails` open a
  home screen instead of offering only the bundled sample: open a package found below the
  working directory or by path, create a starter package, import the models of a dbt-duckdb
  project from its `target/` artifacts, or try the labelled sample. The REPL's `home`
  command shows the same screen. Non-interactive runs still stop with guidance.
- The REPL's `author metric` can create the measure a metric needs without leaving the
  wizard. Cancelling or interrupting the metric takes that measure back, and one `undo`
  reverts both after checking that neither file changed since. If one did, nothing is
  restored: the REPL names the file and the kept changes, and `undo` can still reach them.
- The REPL's `author metric` offers recipes over time besides aggregates and ratios: running
  total, period to date (month, quarter or year), and, in a package with a calendar table,
  rolling window, prior period and growth against a prior period (a percent). Each needs the
  measure's model to have a time column, and says so when it doesn't. Switching an existing
  metric to another recipe drops the old recipe's fields.
- In a DuckDB package whose database can be read, the REPL's `author model` starts from the
  warehouse: pick a table (already modeled tables are marked), then confirm the suggested
  entity, key, time columns, dimensions and measures as prefilled checkboxes, and which
  measures are money amounts. The whole model is written in one change with a preview, a
  parse check and `undo`. Detected links to other tables are listed. Without a readable
  database or table, `author model` asks for the table by name as before. Plain prompts
  accept `none` to clear prefilled checkboxes.
- `pip install 'semantic-rails[repl]'` gives the REPL's authoring wizards arrow-key pickers
  with type-to-filter, checkboxes and highlighted YAML previews. Without the extra, or when
  stdin and stdout aren't a terminal, the REPL keeps its plain line prompts;
  `SEMANTIC_RAILS_UI=plain` forces them (for example with a screen reader) and
  `SEMANTIC_RAILS_UI=pickers` insists on pickers.

### Changed

- `semantic-rails ask` shows the engine's warnings and assumptions, and says when the row
  cap cut the result short (`result.truncated` in `--json`; `--limit 0` removes the cap) or
  the planned query carries its own limit (`result.planned_limit`). Its tables use column
  labels, thousands separators and consistent decimals for measures, print IDs and years as
  stored, right-align numbers, and never cut a number off or show a nonzero value as zero;
  a column of very small values prints about three significant digits.
- `init <name>`, `project new` and `setup --interactive` now write the same starter package
  as the Architect MCP's `create_project`, with the same files and object ids: an
  `<entity>_count` metric beside `total_amount` in `metrics/core.yml`, one example, one
  package test and the shared `.gitignore`. The starter model's label is the plural entity
  name (`Events`, formerly `Event events`), and its `event_type` dimension has no `domain`.
- The semantic-layer comparison pack checks every layer, Semantic Rails included, against an
  independent answer key. The key is SQL written against the shared views without seeing any
  layer's models or outputs, and a second agent reviewed it. Each layer's result columns are
  mapped to the answer's fields explicitly instead of being guessed from their names. On all 16
  questions, the five layers checked on the current data (Semantic Rails, MetricFlow, Cube,
  Malloy and KtX) match the key within 1e-6. The stale Snowflake capture matches 14 and differs
  on q07 and q16.
- The semantic-layer comparison pack generates every support label from one executable rubric
  (`comparisons/semantic_layers/shared/rubric.md`). The rubric applies the same rules to every
  layer, Semantic Rails included, and publishes each label's evidence. The runners this pack
  re-runs now record only whether a question executed. Every layer answers q11 and q12 by
  reading a precomputed customer rollup column, so all six are labeled `precomputed` there.
  Semantic Rails was previously labeled `native`.
- MCP tool results and resource reads carry compact JSON in their text content instead of
  indented JSON, so hosts that forward text pay about a third less per response.
  `structuredContent` is unchanged.
- MCP `discover(verbosity="minimal", limit=5)` returns slim cards: id, kind, label, score, a short
  description, default temporal role and availability, plus the reason when a candidate is
  unavailable. Dimension values also keep their raw value, business label, and availability,
  including blocked values. Omitted options keep the v1 full cards and 10-per-kind limit.
- MCP `execute` can bound its response with an explicit `max_rows` (for example, 200; maximum
  100,000). A larger result returns `truncated: true`, `total_row_count` and an
  `EXECUTE_ROWS_TRUNCATED` warning. An unchanged v1 call keeps its prior uncapped behavior and
  the query's own `limits.max_rows`; the HTTP API also does not add a response cap.
- MCP `validate`, `compile` and `execute` warn with `UNGRAINED_GROUPED_TIME_PROJECTION` when a
  grouped query has a temporal role but no grain, like the runtime's `UNGRAINED_TIME_PROJECTION`
  does for ungrouped queries.
- MCP `inspect(verbosity="minimal")` states each fact once: it leaves out fields that
  repeat another field (`object_type`, `usage_summary`, `top_values`), empty structural fields and all
  but the first starter patch. Declared values and query literals stay exact, including blank/null.
  Omitted verbosity, `"compact"` and `"full"` return the whole v1 card on MCP and HTTP.
  Explicit HTTP `verbosity="minimal"` uses the same slim card projection as MCP.
- MCP `plan` supports opt-in `detail="query"`: `status`, `best.query_ir`, and any `why` or
  `warnings`. An unchanged v1 call keeps the `best` response, including `intent_ir`,
  `best.trace` and `next`. The HTTP API also keeps `detail="best"`.
- `create_optional_fastmcp_server` selects `MCPServer` when the MCP Python SDK 2.x module is
  present, or `FastMCP` on the installed 1.x SDK. The 2.x branch is covered by a simulated
  module test; an SDK 2.x install has not been qualified for the full package.
- MCP `segment-validate`, `segment-explain` and `segment-preview` accept `verbosity="minimal"`
  to return what each tool is for (validity and
  the derived query; the definition, derived query and SQL; member rows and counts) without the
  compiler plans while retaining query/segment policy effects and actionable recovery hints.
  Omitted verbosity and `"full"` keep the v1 whole response; HTTP is unchanged.
- The query MCP states its workflow once, in the server `instructions` returned by
  `initialize`, instead of as "loop position" prose on every tool. Tool descriptions say what
  each tool does, when to use it and its one gotcha, and no longer contradict each other about
  whether `validate` and `compile` must run before `execute` (they are optional dry runs).
  The v1 tool schemas continue to advertise and accept `request_id` and `policy_context`.
  Workflow guidance moves to server instructions, keeping tool descriptions shorter.
- The README and agent quickstart teach the `discover -> plan -> execute` loop: `execute`
  validates and compiles first, so `validate` and `compile` are optional dry runs. The
  README's known limitations match this release, and its links are absolute, so they work
  on PyPI.
- The README now leads with a one-command `uvx` quickstart and copy-paste MCP setup
  for Claude Code, Codex, Claude Desktop, Cursor and the hosted demo endpoint. It also
  adds a telemetry and network-access statement, known limitations and a roadmap. The
  agent API path guide is merged into
  [docs/AGENT_QUICKSTART.md](docs/AGENT_QUICKSTART.md).
- ClickHouse and Databricks connections no longer follow server-directed
  redirects or result links. A ClickHouse request that gets an HTTP redirect
  now fails, including during client initialization, instead of following it.
  Databricks results are fetched inline
  (`use_cloud_fetch=False`) instead of being downloaded from result links.
- Package metadata names Semantic Rails, Inc. as the author.

### Removed

- `semantic_rails.dev_cli` is removed, with no compatibility shim. Import its names from
  their own modules: the developer commands (`cmd_*`, `add_developer_cli`) from
  `semantic_rails.cli.commands.project`; the report builders (`ask_report`,
  `project_status_report`, `setup_report` and the rest) from `semantic_rails.cli.reports`;
  `create_project_report` from `semantic_rails.cli.scaffold`; `cmd_setup_interactive` from
  `semantic_rails.cli.setup_wizard`; `describe_query` from `semantic_rails.cli.interpretation`;
  `DEMO_PACKAGE_ID` and `default_package_ref` from `semantic_rails.cli.common`; and
  `run_interactive_shell` from `semantic_rails.repl.shell`.
- `semantic_rails.cli` now exports only `main`. The engine names it re-exported in 0.2.1 are
  removed. Import them from their own modules: `Runtime` from `semantic_rails.runtime`;
  `SemanticLayerError` from `semantic_rails.errors`; `serve` from `semantic_rails.api`;
  `serve_mcp_http` and `serve_mcp_stdio` as `serve_http` and `serve_stdio` from
  `semantic_rails.mcp_server`; `SemanticLayerMCPAdapter` from `semantic_rails.mcp`;
  `catalog_payload`, `discover_payload`, `inspect_payload`, `build_options_payload` and
  `valid_values_payload` from `semantic_rails.metadata`; `plan_payload` from
  `semantic_rails.planner`; `parse_config_report`, `validate_config_report` and
  `resolve_package_reference` from `semantic_rails.config_validation`; `list_package_ids`
  from `semantic_rails.config`; `export_semantic_contract` from `semantic_rails.contracts`;
  `exception_issue` from `semantic_rails.diagnostics`; and `check_package_report`,
  `build_package_artifact_report`, `diff_package_report`, `impact_report`,
  `promote_package_report`, `run_examples_report` and `run_package_tests_report` from
  `semantic_rails.package_tools`. The CLI command handlers (`cmd_*`) and `MCP_REQUIRED_TOOLS`
  are in the `semantic_rails.cli.commands` modules. The `semantic-rails` command,
  `python -m semantic_rails` and `python -m semantic_rails.cli` are unchanged.
- Removed `semantic_rails.compiler.plan_comparison_bundle` and
  `semantic_rails.config_validation.validate_metric_expression`. Nothing in Semantic Rails called
  them, and neither was documented.

### Fixed

- Two aggregates of one measure that differ only by `filter` or only by
  `temporal_role` no longer share one result column. The column was keyed on
  the measure, aggregation and parameters, so the first aggregate selected
  answered for both. On `jaffle_shop`, the share of new-customer orders by year
  (order count under a filter, divided by order count) returned `[1.0, 1.0]`
  instead of about `[0.034, 0.013]`. Two conversions that differed only by an
  operand filter shared a column the same way. Each now gets its own column.
- An aggregate's `filter` must now be `{all: [...]}`. Other shapes loaded and
  validated anyway. An `any:` list, or `any:` next to `all:`, was silently
  ignored, so the aggregate counted every row. A bare expression node such as
  `{kind: comparison, ...}` passed package validation, and queries then failed
  with a generic `Unsupported expression kind.` error. An `all:` holding one
  condition instead of a list, or a list in place of the mapping, crashed with
  an internal error. These shapes are now rejected with `INVALID_EXPRESSION_AST`
  and the expected shape, in queries and at package validation. A conversion
  operand's `any:` filter reports `INVALID_EXPRESSION_AST` instead of
  `CONVERSION_NOT_SUPPORTED`.
- Falsy malformed filter values (`[]`, empty text, zero, and false) and invalid
  entries under `all:` now fail closed instead of disappearing and producing
  unfiltered counts. The package loader preserves supplied filter values so
  the same validation applies to authored metrics. The package-authoring
  example now uses the supported `all:` predicate form.
- `upsert_metric` and `upsert_segment` no longer lose the object in a file that held a single
  `metric:` or `segment:` when they add another to it: the file becomes a plural block that keeps
  both. `upsert_segment` refuses a segment with no `where`, `metric_filters` or `time` membership,
  which would select the whole population.
- Architect and REPL model edits read package YAML with the YAML 1.2 rules the loader uses, so
  unquoted `no`, `on`, `yes` and `off` in an existing model stay strings instead of being
  rewritten as `false` and `true`.
- `semantic-rails ask` respects a row cap in the planned query alongside `--limit`,
  reports it as `result.planned_row_limit`, and no longer describes it as liftable with
  `--limit 0`. `semantic-rails --help` clarifies which commands require a package.
- `--base-ref` comparisons (`check`, `diff-package`, `impact-report` and
  `promote-package`, and `base_ref` in the Architect MCP's `diff_project`,
  `impact_project` and `promotion_check`) resolve the ref in the git
  repository that holds the package, so they work for packages outside the
  engine's own checkout. The extracted baseline no longer stays behind in a
  temporary directory. Non-regular files, unsafe or ambiguous names, and files
  that cannot be materialized reject the comparison rather than leave an
  incomplete baseline; only regular files with plain names are extracted. A
  ref that looks like an option is refused. For a `base_ref` comparison the
  report's `comparison.source_path` now reads `<ref>@<commit>:<path>` instead
  of naming the temporary directory. Repository and package paths retain
  valid trailing whitespace.
- The CLI no longer answers from the bundled `jaffle_shop` sample package when you haven't
  chosen a package. Without `--package`, `--path`, a package directory or a local profile,
  commands stop and list the ways to choose one; JSON output reports `INVALID_CONFIG` with
  `details.reason: "no_package_selected"`. Scripts that relied on the old fallback should
  pass `--package jaffle_shop`. At an interactive terminal, `ask`, `ls`,
  `project status|validate`, `repl` and bare `semantic-rails` first offer the sample package
  (default No). `ask`, `ls`, `project status|validate`, `mcp setup` and the REPL label a
  bundled package as sample data (`package.bundled` in JSON).
- The semantic-layer comparison pack no longer claims that all 16 questions match across the
  six layers. Its headline is generated from the output check and names any question that
  differs. The pack scores the 7 shared questions separately from the 9 that target Semantic
  Rails features, discloses how the support labels are assigned, and records the Semantic Rails
  version it ran.
- Every layer in the semantic-layer comparison pack now reads the same `comparison_*` views.
  MetricFlow, Malloy and KtX were re-run with pinned environments. Cube 1.6.32 can't be
  reinstalled until its captured lockfile's advisories are resolved, so the SQL it generated is
  re-executed on the same data instead. Each run records a fingerprint of the dataset it read.
  A capture made on other data, such as the Snowflake one, is reported as stale rather than as
  a mismatch.
- A conversion operand whose measure counts anything other than its entity's
  rows is now rejected with `CONVERSION_NOT_SUPPORTED`: a measure that counts an
  expression, such as an `entity_count` measure with a `CASE WHEN ... THEN key
  END` filter, a column other than the entity's key (spelled as the entity
  declares it, case included), or a fact model's rows.
  Before, the conversion counted every row of the entity and silently dropped
  the measure's definition. On `jaffle_shop`, a new-customer-order-to-large-order
  rate with `large_order_count` as the converted operand came out as 1.0
  instead of 0.31: every order counted as a large order, so each base order
  converted to itself. A package with a curated conversion metric built on such
  a measure now fails `validate-config` and `check`, and `discover` lists the
  metric as unavailable. Instead of a filtered measure, count the entity key and
  restrict the operand with its `filter`. Instead of a measure counting another
  column, use a measure on the entity whose rows are the events.
- `import_dbt_project` no longer fails with a duplicate measure id when two dbt models share a
  measure column, such as an order fact and its line fact: the later model's measure gets its
  entity as a prefix (`order_line_usd_to_local_rate`), and re-importing keeps the keys.
- Package validation now rejects unknown keys in the metrics and segments of
  directory packages, in every layout the loader reads (files under `metrics/`
  and `segments/`, root `metrics.yml` and `segments.yml`, and `package.yml`), as
  it already did for single-file packages and for models. These keys used to
  pass silently, and the loader ignored them: a typo such as `valeu_type`, or a
  segment `where` written outside `membership:`, which the segment then
  ignored. Unknown keys inside a segment's `membership:` block are now rejected
  in every package, and `filters` or `dimension_filters` point to
  `membership.where`. Directory-package metrics also get the checks single-file
  packages already had: an unknown `kind:` now fails validation, and a missing
  required field gets a clearer error. A package that is not `schema_strict`
  now fails on `preferred_filter_ops` on a metric, or on `clock_variants`,
  `comparison_peers` or `preferred_filter_ops` on a segment, as a single-file
  package already did. `mf2sr` writes `grain_to_date` on a cumulative metric,
  which the loader ignored, computing an all-time running total, so such a
  translated package now fails validation: rewrite the metric as
  `kind: period_to_date`.
- `discover` ranks an object the question names outright above near-duplicates that add a
  qualifier the question doesn't use: "revenue by store" now puts Revenue ahead of Delivered
  revenue, and "customers" puts Customer count ahead of Ordering customers.
- Correct package-authoring, MetricFlow import, and MCP planning guidance to
  reflect current validation and time-window limits.
- DuckDB runtime bootstrap now creates a missing seed database with atomic,
  no-clobber publication and never replaces an existing database. Validation
  reports missing schema-qualified tables, views, and relation-pipeline sources
  as `INVALID_CONFIG` with `details.missing_relations`, regardless of seed
  provenance or the legacy `SEMANTIC_RAILS_ALLOW_DB_RESEED` setting. Existing
  databases are catalog-probed in a separate process so a stale in-process
  catalog cannot certify a replaced file and the probe cannot release a
  serving connection's process-wide lock. Operators must build missing
  relations with the database owner or explicitly remove a backed-up,
  disposable seed database before restarting.
- A filter comparison that takes one value (`=`, `!=`, `<`, `<=`, `>`, `>=`,
  `LIKE`, `NOT LIKE`) now rejects a list value with `INVALID_QUERY` and a
  `USE_IN_FOR_LIST_VALUE` hint to use `IN`. Before, the list was rendered as
  one string literal, such as `store_name = '[''Philadelphia'', ''Brooklyn'']'`:
  a `where` filter validated and silently returned no rows, and a
  `metric_filters` comparison failed in the warehouse.
- A metric filter whose literal matches no value in the data, such as
  `status = 'Completed'` over a column that holds `completed`, passed
  validation silently, and the metric then returned an empty result with
  status `ok`. Validation that reads the data now warns with
  `FILTER_VALUE_NOT_FOUND` and names the closest value (`did you mean
  'completed'?`), and `project validate` shows runtime warnings in the
  `runtime` and `full` modes. Parse-only validation is unchanged.
- `impact_project`, and `validate_project` with `mode=impact`, no longer fail on a package with a
  metric filtered by an unquoted date, which YAML reads as a date.
- MCP tool descriptions no longer call `execute` side-effecting or the only tool that costs
  warehouse credits (`valid-values` with a live query and `segment-preview` also query the
  warehouse), and `valid-values` gives a real example id (`dimension.jaffle_store_name`).
- Query patches from `discover`, `inspect` and `build-options` contain only Query IR fields, at
  every `build-options` step. They no longer copy the caller's `policy_context` or the tool's
  other arguments, and they validate as returned: `build-options` value filters use `field`, a
  patch for a windowed metric carries its default time window, and a `percentile` option carries
  `p`. (A temporal role offered at the `time` step may still be one the selected measure doesn't
  use.) These tools read Query IR only from their `query` argument, not from top-level fields.
- `mcp setup` and `mcp client-config` run through `uvx` now write client configs that
  start the server with `uv tool run --from <the same install>`. They used to name the
  Python inside uv's cache, so the client stopped starting the server after
  `uv cache prune` or `uv cache clean`. The requirement keeps the version (or source)
  and any installed extras. `mcp status` lists launch commands the same way, so they
  work when `semantic-rails` isn't on `PATH`. Installed commands are unchanged.
- Package validation now rejects a metric whose `temporal_role` is not the
  clock of any of its measures, such as a ratio of order measures declared with
  the sessions table's clock. Such a metric still answered: each measure fell
  back to its own clock, with a `REWRITE_APPLIED` warning, but the result was
  labeled with the declared role, so one clock's series was presented as
  another's. A metric that mixes clocks is still accepted when one of its
  measures has the declared clock, and a conversion metric is timed by its base
  operand, as at query time. A package with such a metric now fails
  `validate-config` and `check`.
- `mf2sr` translates MetricFlow cumulative metrics into the kind that computes them. A
  `grain_to_date` becomes `kind: period_to_date` and a `window` becomes `kind: rolling`.
  Before, both were written onto `kind: cumulative`, which ignores them and returns the
  all-time running total. A cumulative metric is skipped, with the reason, when the engine
  can't compute it: both options set, a window finer than a day, a day-to-date grain, or a
  measure whose periods don't add up, such as an average or a distinct count. A filter on
  a cumulative metric is kept. Where the translated values can differ from MetricFlow's at
  coarser grains, a warning says how.
- `mf2sr` skips a derived metric whose inputs use `offset_window`, `offset_to_grain` or a
  filter, which it would have computed over the same period or unfiltered, and also skips
  a derived metric with its own filter rather than dropping that filter.
- `mf2sr` writes metric filters in the form the engine applies,
  `{all: [{field, op, value}]}`, naming each field by dimension id. Before, every filtered
  metric it wrote returned unfiltered numbers. A metric's filter now also applies to both
  sides of a ratio, and the filter on a measure input is kept. List filters and multiple
  manifest `where_filters` are ANDed, comparisons with a literal and `NOT IN` are
  translated, and an `IN` list keeps commas inside its quoted values. A filter on a time
  dimension, which MetricFlow compares truncated to its grain, is reported instead of
  applied to the raw column.
- `mf2sr` skips any metric whose filters it can't keep, and any metric that uses
  a skipped metric, rather than emitting a metric with changed values. Source
  metric definitions take precedence over same-named measures in ratios and
  transitive dependents. Double-quoted SQL identifiers are rejected as filter
  operands instead of being mistaken for string literals.
- `plan` keeps calendar windows it used to drop silently, such as "in 2017", "for 2017", "the first
  half of 2017", "Q2 of 2017", "April 1 to April 7, 2017" and ISO dates, and gives a total over
  one of them a grain that yields one bucket. It resolves a window only when the question names
  exactly one, in a form it reads unambiguously. A bound ("before 2017", "since March 2017"), a
  qualifier ("early 2017", "the end of 2017"), a comparison ("2017 vs 2016", "2017 over 2016"),
  a numeric date such as 4/3/2017, two periods joined by "and" ("March and May 2017"; "between"
  makes a range), or two windows at once is reported as `TIME_WINDOW_UNRESOLVED`, even when the
  draft carries another window, instead of being narrowed or widened to the nearest form that
  parses. An explicit grain ("monthly revenue in Q2 2017") wins over the window's own bucket.
  Questions longer than 2,000 characters require a shorter question or complete explicit time bounds;
  the planner reports unresolved time scope instead of silently reading only a prefix.
- When a draft can't take the window's start, because the metric looks back over earlier periods
  (month-over-month growth, rolling or cumulative totals) or the question compares with an
  earlier period, `plan` keeps the window's end and reports `TIME_WINDOW_START_DROPPED` with the
  start to filter the rows by.
- `plan` no longer reports `status: ok` for a filter that keeps or drops values the question
  doesn't name. "Revenue for Brooklyn" filtered with `IN ["Brooklyn", "Philadelphia"]` and
  no grouping by store, or "revenue excluding Brooklyn" filtered with
  `NOT IN ["Brooklyn", "Philadelphia"]`, now downgrades to `low_confidence` with a
  `filter_values_unrealized` gap. Grouping by the field still accepts the extra kept value,
  since each value gets its own row, and a filter that keeps exactly the named values, as in
  "revenue for Brooklyn and Philadelphia", is still `ok`.
- `plan` answers with the metric a question names instead of a less specific measure.
  "What is completed revenue by month?", the question the metric wizard suggests for a
  filtered metric, used to return unfiltered revenue with `status: ok`; it now drafts the
  Completed Revenue metric. A metric named by its id works the same way, and a rolling
  metric's label ("revenue, trailing 7 days") is no longer also read as a 7-day window and a
  top 7. A draft that leaves out a metric the question names, or has no filter for a
  "where <dimension> is <value>" clause (such as "revenue where channel is web" when the
  channel values aren't declared), is now `low_confidence` (`named_metric_unrealized`,
  `dimension_filter_unrealized`).
- `plan` and `ask` read a grouping that names the query's own clock as its
  time axis when they total a measure or metric by dimensions. "Revenue by
  store and order date at month grain, from January 1 2017 to March 31 2017"
  grouped by *Customer first order at* as well as the month, and still
  reported `ok`; it now groups by store and month only. "Revenue by store by
  order date" dropped the date and returned one row per store; it now returns
  one row per store and day, and a cadence the question names ("monthly",
  "at week grain") sets the buckets instead.
- `plan` no longer caps a qualified rollup at 5 rows when the question doesn't ask for a top
  N. "Monthly revenue from customers with at least 2 orders" returned only its first 5
  months with `status: ok`; it now returns every month. A "top 3" question still keeps its
  limit of 3.
- Editing a measure, dimension, time column or metric in the REPL now keeps each saved choice
  when you press Enter. Before, a choice the menu did not list was replaced: a `median` or
  `last_value` measure became a `sum`, a stock measure lost its `accumulation`, and other
  values fell back to the first option. The aggregation menu now offers every aggregation the
  measure's accumulation allows, except `percentile`, which a measure cannot parameterize.
  Choosing a different default aggregation prints a warning that names the metrics whose
  numbers change.
- The REPL's `author metric` takes its defaults from the metric's own inputs. The time axis
  is the chosen measure's clock (a metric on orders is no longer reported on another table's
  date), and when the inputs offer more than one clock it asks. A ratio's denominator
  defaults to a count on the numerator's model, and its result type follows the inputs:
  revenue per order is currency per unit, other ratios default to a dimensionless ratio, and
  percent requires an explicit choice.
- Managing a metric reads it back as the package loads it. Inputs named by key, by
  namespace-qualified or custom name, explicit aggregations, filters, windows, currency and
  the time axis (including one set with `time`) are offered as the saved choices, so pressing
  Enter throughout leaves the metric as it was. Changing an input or recipe proposes the new
  input's result type, currency, aggregation and time axis instead, and a new filter dimension
  or measure asks for new filter values. The saved measure stays selected even when another
  measure's key spells its ID or a search would push it out of the short list.
- Expressions the wizard cannot write back, such as authored time expressions, several filter
  clauses or other derived formulas, stay unchanged unless another recipe is chosen. A saved
  window or offset unit the wizard cannot offer is refused rather than replaced.
- Editing a metric keeps its authored `examples` instead of replacing them.
- Plain REPL choices keep canonical option values when accepting uppercase defaults or
  case-insensitive typed answers, including filtered-metric `IN` and `NOT IN` operators.
- A new model proposes a singular entity key without clipping words such as `status` or
  `address` to `statu` or `addre`.
- In the REPL's arrow-key prompts, typing replaces a suggested default instead of appending to
  it. Typing `cancel` and Enter now cancels at a text prompt or a list; Ctrl-C cancels at any
  prompt. At a yes/no question, `y` or `n` waits for Enter, so that Enter no longer answers
  the next question.
- `run` and `ask` say why a planned query cannot run instead of stopping with no result and no
  error. For example, a growth metric by month needs a calendar with a `month_start` column.
- The metric wizard offers rolling windows, prior periods and growth only in the units the
  package calendar can fill, and names the calendar columns the other units need.
- A similar-name warning now comes right after the key and label (also when an update changes
  the label), and choosing different wording asks for them again. Declining to update an
  existing object also asks for another key. Both used to end the wizard.
- Before an operational `validate` (runtime, examples, tests or full) on a DuckDB package, the
  REPL names the selected database file. It explains when a missing seeded file may be created,
  and that an existing file is never rebuilt or replaced. External missing files and broken
  links are reported without creation. Validation output prints a repeated error once with a count.
- Package validation (`parse-config`, `validate-config`, `check` and
  `semantic_rails.embedding.validate_runtime_package`) now rejects segments that
  `catalog`, `inspect` and the `segment-*` commands cannot serve: an `entity:`
  that names no entity, including inside membership `metric_predicate` filters
  (the error suggests the closest entity id when one is close), a segment that
  `catalog` rejects, such as one with a preview dimension from another entity,
  or a derived query that does not compile. These packages used to pass
  validation, and then `catalog` and `inspect`, or the `segment-*` commands,
  failed on every request.
- `query_matches_snapshot` package tests compare numbers by value, so a
  DECIMAL result with cents matches the number written in the test's YAML.
  `metric_equals_query` compares its two results the same way, and mismatch
  details show numbers in that one form (trailing zeros dropped).
- A DuckDB database built from a package's seed no longer goes stale silently. When the seed
  files change after the build, query results and `project validate` (the runtime and full
  modes) include a `STALE_SEED_DATABASE` warning with the command that deletes the file; the
  next run rebuilds it from the current seed. Databases built before this release have no
  recorded seed hash; delete one once to start tracking it.
- Under `schema_strict: true`, a metric that declares `value_type: number` is
  now accepted. Ratio and derived metrics with it were rejected as having an
  "implicit" value type. A metric whose `value_type` is missing, `null` or
  empty is now rejected in every layout the loader reads, single-file packages
  included. Before, only metric files under `metrics/` were checked for a
  missing value type. A directory package that writes `schema_version: "1"`,
  which the loader accepts, now gets the same directory checks as one that
  writes `1`. Before, it skipped them.
- A query with `time.fill: true` no longer drops a bucket at the edge of its
  window. The filled series was built from the calendar's bucket-start column
  filtered to the window, so a week or month that started before `start` was
  left out along with the rows it held from inside the window, and a bound with
  a time of day could drop its day. On `jaffle_shop`, orders for July 2017 by
  week lost the week of June 26, which holds July 1–2 (7,268 instead of 7,438
  orders), and a monthly query from July 15 lost the rest of July. The filled
  series now has every bucket that holds a day of the window, including empty
  buckets, when the calendar declares a `date`-typed `date_day` dimension. A
  timestamp-typed or missing `date_day` keeps the existing bucket-start bounds
  and can still omit a partially covered bucket. For the `date` expansion,
  offset bounds use the temporal role's calendar zone, and the series retains any
  populated source bucket when timestamp offset comparisons differ. A reversed
  interval with no selected source rows produces no filled buckets. Submicrosecond
  bounds retain their precision for interval ordering and midnight day inclusion.
- YAML that Semantic Rails writes into a project (new projects, and Architect and REPL
  edits such as a growth metric) and the REPL's YAML previews no longer use `&id001`
  anchors and `*id001` aliases when one value appears twice in a document. Each value is
  written out in full.

### Security

- The Architect MCP's `sse` and `streamable-http` transports now require a
  bearer token (`--token-file`, `SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE` or
  `SEMANTIC_RAILS_ARCHITECT_TOKEN`: at least 32 characters) and refuse to start
  without one; set it for both the server and its clients. The Host check also
  accepts the address given to `--host` and host names without a port, and
  `mcp_client_config` returns an `Authorization` header template. The stdio
  transport is unchanged.

## 0.2.1 — 2026-09-15 — Authorized execution, immutable snapshots and metric portability

### Added

- A packaged, versioned public contract bundle for package authoring, stable and
  preview Query IR, HTTP, query MCP, framework-neutral semantic validation, and
  validation-report envelopes. Deterministic generation, compatibility checks,
  and wheel/sdist verification make these artifacts release gates.
- A stable `semantic_rails.contracts` producer for dbt, SQLMesh, and future
  validation bindings, plus the `semantic_rails.embedding` facade and a generic
  in-memory warehouse credential-provider seam for independent engine hosts.

### Changed

- Query MCP tool discovery now publishes output schemas and standard behavioral
  annotations. Stable Query IR v1 accepts only `version: 1`; version 2 has a
  separately named preview schema.
- Architect MCP mutations now require optimistic project revisions and
  idempotency keys, serialize across processes, parse-gate atomic multi-file
  commits, roll back failures, and support exact write-free previews through a
  versioned generated interface contract.
- The public repository and distributions now contain only the standalone
  engine. Site deployment code and service-client commands are maintained
  outside this release boundary; local profiles select package paths only.
- Contract schema identifiers use the controlled
  `https://semantic-rails.com/schemas/` mirror. PyPI-verified wheel/sdist bytes,
  every contract JSON artifact, and `SHA256SUMS` are attached to the GitHub
  Release without a second build.

## 0.2.0 — 2026-07-09 — Governed onboarding and release hardening

### Added

- Guided local onboarding with `setup --interactive`, reusable local profiles,
  managed MCP `start` / `stop` / `status`, and Claude/Codex client-config generation.
- Architect MCP workflows for scaffolding, inspecting, and validating Semantic Rails
  packages without weakening the deterministic CLI baseline.
- Precomputed catalog manifests shared by HTTP, MCP tools, and MCP resources, with a
  context-safe live-computation fallback.

### Changed

- Remote HTTP and MCP requests now resolve one trusted request context. Caller-supplied
  policy context cannot override authenticated tenant, role, audience, environment, or
  project fields, including on segment workflows.
- Plain grouped questions such as “orders by store” no longer invent a monthly grain;
  unresolved or low-confidence plans are never marked ready for execution.
- ASGI work runs through a bounded queue while health checks remain responsive. Shared
  warehouse connections are serialized, cursor reads are bounded, truncation is explicit,
  and DuckDB timeouts use an interrupt watchdog.
- Release, package-distribution, and dependency-audit gates now verify the exact
  artifacts and semantics they publish.

### Security

- Policy-sensitive catalogs are no longer shared through the edge cache.
- Managed MCP processes verify OS-observed process identity before signaling a stored PID.

## 0.1.1 — 2026-06-24 — Warehouse expansion and release polish

### Added

- **Warehouse-dialect expansion: nine registered warehouses.** Postgres, BigQuery,
  Databricks, Athena, ClickHouse, MotherDuck, and DuckLake join DuckDB and Snowflake as
  first-class execution targets, each as one dialect class + one adapter + one registry
  entry (`semantic_rails/dialects.py`); option validation, secret resolution
  (env-var/file indirection only — literal credentials are rejected at parse time),
  redacted error envelopes, and factory dispatch are shared machinery
  (`semantic_rails/db_parts/common.py`). Dialect quirks are reconciled to exact DuckDB
  parity — exact percentiles rebuilt from sorted `ARRAY_AGG` where the warehouse only
  offers approximate sketches (BigQuery, Athena), boundary-crossing `date_diff` and
  clamped calendar `date_add` relowered on Postgres, FULL-JOIN-compatible null-safe
  equality on Postgres/BigQuery, and an exact `LAG` emulation on ClickHouse.
  Documented, literal-aware compat passes rewrite rendered SQL only where a hard
  warehouse limit demands it (identifier shortening and float ratio division on
  Postgres; backtick quoting and field-name legalization on BigQuery/Databricks;
  typed temporal literals on Athena).
- Optional pip extras per driver: `semantic-rails[postgres|bigquery|databricks|athena|clickhouse]`
  (joining `snowflake` and `server`), plus `all` for every connector. MotherDuck and
  DuckLake reuse the core `duckdb` dependency. A missing driver maps to a structured
  `MISSING_DEPENDENCY` error naming the extra.
- Cross-warehouse conformance suite (`tests/integration/`): every registered warehouse
  must return row-for-row identical results to the DuckDB reference for the full query
  battery (worked examples + the jaffle_shop package's own test queries) over an
  identical fixture. Targets without credentials skip (`SR_INTEGRATION_STRICT=1` for CI
  posture); a registry-coverage test fails any warehouse registered without a
  conformance target. `make warehouses-up` provisions local Postgres + ClickHouse via
  docker compose; `make test-integration` runs the suite.
- `docs/ADDING_A_DIALECT.md` — the add-a-warehouse guide, with Redshift as the worked
  example (Redshift ships as a documented stub: env-var names reserved in
  `.env.example`, registry block ready to uncomment, account pending verification).

## 0.1.0 — 2026-06-11 — Initial public release

Semantic Rails ships as an Apache-2.0-licensed agent runtime for governed metrics, structured around a
deterministic `discover → inspect → plan/build-options → valid-values → validate → compile →
execute` loop served over MCP (stdio + HTTP) and a 16-route `/api/v1/*` surface. This is the
first published release; everything in this entry (and the feature inventory below) is part
of it.

### Added

- Package-authored semantic caveats: an optional `caveats.yml` (or inline
  `semantic_caveats:` block for single-file packages) attaches advisory
  interpretation context — business events, definition changes, data-quality
  notes — to semantic objects, entity values, and time windows. Matching
  caveats surface as `SEMANTIC_CAVEAT_APPLIED` warnings on `validate`,
  `compile`, and `query`/`execute` (with `SEMANTIC_CAVEATS_TRUNCATED` past the
  verbosity cap); they never alter SQL, rows, access, discovery, or policy
  behavior. Caveats are gated by the same audience/environment scoping as
  policies, validated at load time against declared object ids, counted in
  the package manifest summary, and tracked by `diff-package` /
  `impact-report` as metadata-only changes.
- Entity hopping is now policy-controlled and observable. `graph.path_policy.max_hops`
  (default 4, max 8) replaces the hardcoded hop ceiling; `graph.path_preferences`
  pins the join route per entity pair and is validated at load time. Queries that
  need the same table through two different relationships refuse with
  `PATH_JOIN_CONFLICT`; routes chosen by hop count alone with an undeclared
  alternate emit a `PATH_ALTERNATES_UNPINNED` warning; `PATH_NOT_FOUND` now
  distinguishes `hop_limit_exceeded` from `no_relationship_chain`. Every
  compile/query response carries a `hop_profile` (chosen chains, per-hop
  cardinality/safety, long-hop targets) for acceleration-layer telemetry.

### Fixed

- **Authoring mistakes that every validator silently accepted now fail loudly with located,
  actionable errors.** A blind-author evaluation planted realistic mistakes in a fresh `init`
  package and found a class of shape errors that passed `validate-config`, `check`, and `doctor`
  unflagged:
  - Unknown keys in package/graph/model/dimension/time/measure/metric/segment/join blocks
    (e.g. `agg:` for `default_agg:`) were silently ignored; they are now rejected with
    did-you-mean hints or the full allowed-key list. The document top level flags only
    close-match typos (`modles:` → `models`), so annotation blocks like the capabilities
    reference remain valid.
  - A scalar `domain:` (e.g. `domain: new`) was iterated character-wise into the value set
    `['n', 'e', 'w']`; non-list `domain`/`valid_values`/`supported_grains`/`environments` are
    now rejected with a written-out fix.
  - The dimension-kind/time-kind/time-class enum checks only ran for directory packages;
    single-file packages (`init`'s output) now run them too, plus new measure-kind,
    metric-kind, and accumulation-kind enums.
  - A model `grain:` matching no entity key silently fell back to first-entity primary
    detection; it now errors with the candidate keys.
  - A typo'd ratio `numerator:`/`denominator:` surfaced as a late compiler error
    (`Unknown metric recipe`) with no location; it now fails at parse naming the metric, the
    field, and close matches. A metric whose kind produces no expression names the per-kind
    required fields instead of `Expression requires a 'kind'`.
  - A measure with neither `kind:` nor `expr:` was misdiagnosed as "missing expr"; the error
    now states both authoring options.
- `validate-config` now runs the warehouse column-reachability probe (previously `check`-only),
  which also probes entity key columns — so a dimension, measure expr, or graph `key:` pointing
  at a nonexistent column fails the first command an author runs instead of query time.
- `doctor` failed every standalone package on a cwd-relative Dockerfile check that carried no
  message; the check is now informational (resolved next to the package), failing checks carry
  a hint, and the report lists `failing_checks` by name.
- A missing `package.seed.source` file raised `INTERNAL_ERROR` with a raw errno and a repo-root
  path the author never wrote; it now raises `INVALID_CONFIG` naming the field and both
  locations checked.
- **Conversion operand filters now compile into SQL.** Ad-hoc filters on conversion
  `base`/`converted` aggregate operands (e.g. base = orders containing Product A, converted = a
  later order containing Product B) previously validated cleanly and were silently dropped from
  the compiled plan. They now lower into the conversion event CTEs (filter dimensions are joined
  via the rewrite-safe path gate; fanout joins act as EXISTS-style "contains" filters because
  matching dedups on the event key). Filter shapes that cannot lower — operand-level `window`
  specs, expression-valued clauses, non-`all` combinators — are rejected with structured
  `CONVERSION_NOT_SUPPORTED` errors instead of being ignored.
- A `window` dict on a plain `aggregate` expression was accepted and silently ignored (a
  "rolling 7-day" ask compiled to an unwindowed aggregate). It is now rejected at bind time with
  a pointer to the supported `rolling`/`prior_period`/`period_to_date` expression kinds.
- Conversion `window.unit` values outside the supported set (e.g. `fortnight`) failed only at
  warehouse execution; they now fail `validate` with the supported unit list.
- `dimension_bindings` with unknown keys, unknown `side` values (previously silently treated as
  base-side), or unsupported `denominator` policies are rejected at parse time.
- Unknown dimensions referenced by conversion `constant_properties` raised an internal
  `KeyError`; they now return a structured `OBJECT_NOT_FOUND`.

### Changed

- The planner resolves explicit historical month ranges ("from January 2017 through June 2017",
  "Jan-Jun 2017", "March 2024") into `time.start`/`time.end` instead of dropping the bounds.
- The planner no longer answers conversion/funnel intents with a confident non-conversion draft:
  when conversion markers are detected and the best draft contains no conversion expression, the
  plan is demoted to `low_confidence` with a `CONVERSION_INTENT_UNREALIZED` why, curated
  conversion metrics to redirect to, and the ad-hoc conversion IR shape.
- The `capabilities` payload documents operand-level conversion filters (shape, EXISTS
  semantics, and what is rejected).
- Semi-additive (stock) measures keyed only by their own time column now reduce to the
  last/first snapshot inside each output bucket instead of summing every snapshot in the
  period. Previously a daily YTD rollup queried at month grain returned the sum of all 31
  daily snapshots (16x overstated; 200x as a scalar) with no warning. Distributional
  aggregations (median/avg/min/max) over such series still aggregate across the snapshots
  in the bucket.
- `diff-package` / `impact-report` snapshots now include measure expressions, source
  relations, row grain, accumulation, and entity tables — redefining a measure's SQL or
  repointing a model at a different relation previously diffed as "0 changes, risk: low".
- Warnings no longer carry refusal-shaped envelope fields (`why_invalid`,
  `unsupported_construct`, …) unless the producer set them explicitly; an advisory caveat
  on a successful query no longer reads like a failed one.
- The wheel ships `semantic_rails` plus `mf2sr`, the MetricFlow translator behind
  `semantic-rails import --from metricflow`; `tests/mf2sr` now runs in the CI, publish, and
  release-readiness gates. The `semantic_layer` rename shim and the `semantic-layer`
  console-script alias were dropped before first publish (nothing was ever published under
  the old names, and the unrelated PyPI project `semantic-layer` owns that import
  namespace).
- `semantic-rails --version` and `semantic_rails.__version__` report the installed version.

### Feature inventory

Everything below describes the full surface as shipped in 0.1.0.

#### Naming

- The import package is `semantic_rails` and the console script is `semantic-rails`, matching
  the `semantic-rails` distribution name. (The project was developed under the `semantic_layer`
  / `semantic-layer` names, but the unrelated PyPI project `semantic-layer` claims the
  `semantic_layer` import namespace, so both were renamed before this first publish. No rename
  shim ships: nothing was ever published under the old names.)

#### Added

**MCP Streamable HTTP**

- Stateless MCP Streamable HTTP served at `/mcp` by the ASGI app, protocol `2025-11-25`: JSON
  responses, `202` notifications, Origin validation, and a 64 KiB body cap.

**Runtime**

- `semantic_rails/` is the only supported runtime: graph-first packages, planner-owned
  mixed-grain rewrites, AST-first compile and explain, supported event-pair and same-store
  conversion metrics, `metric_predicate`, temporal-validity joins, and segments. The CLI accepts
  `--path` on `query`, `compile`, `explain`, and `validate` so authors can run a freshly authored
  package without registering it.
- Authored `relation:` pipelines (`json_explode`, `date_spine`, `anti_join`, …) lower into
  reviewed SQL CTEs at compile time.

**Agent surface (MCP + HTTP)**

- 13 MCP tools mirroring the public API operations — `capabilities`, `catalog`, `discover`,
  `inspect`, `build-options`, `valid-values`, `plan`, `validate`, `compile`, `execute`, and
  `segment-{validate,explain,preview}` — each with what/when/gotcha descriptions. Errors return
  structured envelopes with `recovery_hints` and `closest_matches`; the envelope shape is
  identical across all 13 tools, and every tool either rejects unknown arguments with a
  structured error or warns and ignores them — none silently accepts unknown keys.
- `plan(intent, query?, detail?)` — the single public natural-language planning tool and HTTP
  route. Returns one best Query IR with a `status` discriminator
  (`ok | low_confidence | unrealizable | out_of_scope`), `intent_ir`, and pre-baked
  `next.validate` / `next.valid_values` / `next.ready_for` args; `detail="full"` adds
  alternatives and blocked drafts. `status="ok"` is trustworthy: plan runs `runtime.validate`,
  which also warms the compile cache for the follow-up `compile` call. Backed by a pattern
  registry (`semantic_rails/planner/`) with 6 hand-tuned named patterns, 2 catch-alls, a
  per-package `disabled_patterns` opt-out, and gated coverage/byte/latency benchmarks
  (`scripts/benchmark_plan.py --gate`) plus a catalog-walked generated corpus.
- Planner time windows: relative windows in natural-language intents ("last month", "last 7
  days", "last N weeks/months/quarters/years", "this year", "yesterday") resolve into the Query
  IR's relative range form (`time.range.last`) or calendar bounds instead of being silently
  dropped. When an intent names a window the planner cannot resolve (e.g. "last few weeks",
  "since 2023"), the plan is downgraded to `low_confidence` with a structured
  `TIME_WINDOW_UNRESOLVED` warning naming the phrase and recovery hints listing supported window
  forms, instead of marking an unbounded draft ready to execute. Query grain follows a resolved
  relative window's unit when no explicit grain is requested (e.g. "last 7 days" produces a
  daily series).
- `capabilities` (loop position 0, ~3KB cold-start orientation) at `GET /api/v1/capabilities`
  and as an MCP tool, returning the locked v1 route index, warehouse and package capabilities,
  and `expression_shapes` — a `{name, description, example}` entry for every accepted
  `select[*].expression` kind, shared between the HTTP and MCP surfaces so agents can introspect
  the IR contract on either.
- `catalog?verbosity=summary` (counts + flat ID lists, under 10KB on jaffle_shop) for cold-start
  orientation; `compact` caps each kind at 200 rows and ships `counts` / `counts_total` /
  `truncated`. A pre-compiled `.compiled/manifest.json` catalog manifest (written by
  `validate-config`, fingerprint-invalidated, `--no-manifest` to skip) serves unfiltered default
  catalog calls ~100× faster.
- `discover`: IDF-weighted token scoring on the user-facing surface, a runnable
  `starter_query_patch` on every measure/metric/dimension candidate, and structured
  `cross-entity` match reasons (with a rank penalty) when a dimension's root entity disagrees
  with the top measure/metric candidates. `discover` and `plan` route off-topic intents through
  a relevance floor (`out_of_scope` / `low_relevance`) instead of hallucinating confidently.
- `inspect` starter patches covering `select`, `group_by`, `time`, `order_by`, `where` (seeded
  from declared `value_domain`s, no warehouse round-trip), and `metric_filters`; the
  `metric_filters` scaffold fires on every metric. History-backed cards expose
  `coverage_notes` and `null_bucket_meaning`.
- `build-options`: `next_legal_steps` (the full ordered remaining-step list, not just the next
  step) and `review_priority`-based ranking with topic-derived `why_recommended` for
  empty-query calls.
- Diagnostics: `UNGRAINED_TIME_PROJECTION` warning when `time.temporal_role` is set without a
  grain, group_by, or inline window expression; `EXPRESSION_NORMALIZED_AWAY` silent-drop guard;
  `EMPTY_RESULT_WINDOW` ships `actual_data_coverage` plus the resolved `requested_window` and
  the original `relative_range`; `WINDOWED_TIME_FILTER_UNSUPPORTED` carries ordered
  `drop_time_start` (always safe, first) and `widen_time_window` (computed `suggested_start`,
  bucket-boundary caveat) recovery hints.
- Query IR guardrails: unknown top-level keys are rejected with `INVALID_QUERY` and a
  `USE_CANONICAL_KEY` hint naming the right slot (`filters`→`where`, `having`→`metric_filters`,
  …) on both `normalize_query` and `normalize_partial_query`; misplaced absolute bounds emit
  `MOVE_BOUNDS_TO_TIME_TOP_LEVEL`; `{dimension: ...}` in `select[]` emits
  `MOVE_DIMENSION_TO_GROUP_BY`; underscore-prefixed annotation keys (`_note`) pass through.
- MCP resources (`semantic-rails://capabilities`, `semantic-rails://catalog/{summary,full}`)
  and prompts (`semantic-rails-query-builder`, `-query-review`, `-segment-workflow`).

**Hosting compatibility**

- Pluggable `PolicyContextResolver` and `AuditSink` protocols, `CompiledSqlCache` protocol with
  a public `Runtime.set_compile_cache` seam, in-process `Runtime.reload()`, per-request `limits`
  on the query envelope, env-controlled CORS allow-list, HMAC-based API-key check, redacted SQL
  on `QUERY_EXECUTION_ERROR` (raw SQL requires `debug: true` + the
  `SEMANTIC_RAILS_ALLOW_DEBUG_SQL=1` operator opt-in + the `debug` role), and
  compact-by-default JSON responses. None of this turns Semantic Rails into a hosted product —
  it just removes the forks one would need to host it.

**Authoring UX**

- `check` is the one-command package gate (parse, validate, examples, tests, manifest, optional
  artifact) and probes the warehouse for column reachability so missing columns fail at author
  time. `validate-config` rejects unknown kind values, requires `value_type` on every metric,
  and flags semantic collisions (identical labels/names, ≥80% label-token overlap,
  `search_terms` subsets) between package objects. `STATEMENT_TIMEOUT_NOT_HONORED` is surfaced
  when the configured timeout cannot be enforced.

**Packaging, release, and deployment**

- PyPI distribution named `semantic-rails`; the wheel includes the runtime, CLI, bundled Jaffle
  proof package, seed assets, and the PEP 561 `semantic_rails/py.typed` marker so type checkers
  pick up the package's inline annotations. Development Status classifier is `4 - Beta`;
  `Programming Language :: Python :: 3.14` is declared and the CI test matrix covers 3.14.
- Tag-triggered PyPI publish workflow using `uv build` and PyPI Trusted Publishing (OIDC,
  `pypi` environment), with the full test suite as a release gate and all third-party actions
  SHA-pinned; `dorny/paths-filter` is SHA-pinned in the CI workflow as well.
- Docker hardening: docker-compose binds the API to `127.0.0.1` by default, the base image is
  pinned to an exact patch release (`python:3.12.13-slim-trixie`), Dockerfile and compose
  healthchecks probe the same `/api/v1/ready` endpoint, and both files document
  `SEMANTIC_RAILS_API_KEYS` / `SEMANTIC_RAILS_CORS_ORIGINS` for non-local deployments.
- `jsonschema` added to the dev dependency group so the schema-contract tests run instead of
  skipping silently. The distribution verifier installs the wheel into an isolated venv and runs
  `packages`, `catalog`, and `query`.

**Comparison pack and license**

- Six-layer comparison (Semantic Rails, MetricFlow, Cube, Malloy, Snowflake Semantic Views, KtX)
  with split baseline (q01–q07, where five of six layers score 7 native and Cube takes one
  workaround on q05) and differentiator (q08–q16, where the workaround cost is visible)
  scoreboards plus a methodology disclosure.
- License posture: Apache 2.0 throughout (switched from MIT before first publish). No
  "available source," no non-commercial tier, no separate enterprise SKU in this repo.

#### Changed

- `time.range.last.unit` values `minute` and `hour` were removed from the published Query IR
  schema — the runtime never supported sub-day relative ranges.
- `where[]` and `order_by[]` standardized on `field` as the single key; the prior
  `where[].dimension` shape is not accepted and the error envelope points at `where[N].field`.
  `schemas/query_ir.v1.json` requires `field` to match the runtime.
- `range.last` is strictly an object (`{unit, value}`) in the JSON Schema, the MCP-exported
  QUERY_SCHEMA, and the validator; the string shorthand (`"90 days"`) is rejected with a
  `USE_OBJECT_SHAPE` recovery hint carrying the corrected shape inline.
- Response-shape dedup: `inspect` cards ship only `recommended_dimensions` /
  `recommended_filters` (the `preferred_*` aliases are gone); intent candidates carry only
  `candidate_ir`; `catalog?verbosity=compact` no longer ships `alias_index` (moved to `full`).
- `validate` / `compile` / `execute` tool descriptions enumerate all four positional key
  vocabularies (`select[].as`, bare `group_by` strings, `where[].field`, `order_by[].field`)
  and the accepted select-expression shapes inline, under the 700-char scannable cap.
- Metadata internals split into `semantic_rails/metadata_parts/` (`capabilities`,
  `valid_values`, `scope_gate`); the internal `formulate` package was renamed to `planner`.

#### Fixed

**Agent ergonomics (from a blind-agent evaluation against the live MCP endpoint)**

- MCP `validate`/`compile`/`execute` now default to `verbosity=minimal` at the adapter
  boundary — default envelopes dropped from 91–103KB to 0.7–2.4KB and error envelopes from
  ~62KB to ~3KB, while keeping structured errors, `recovery_hints`, `rendered_sql` (compile),
  and `rows`/`row_count` (execute). Explicit `verbosity` still wins; HTTP v1 defaults unchanged.
- `tools/list` shrank from ~28.1KB to ~15.9KB: the IR cheat-sheet and full Query-IR schema now
  ship once (on `validate`) with the other IR tools pointing at it; stale size hints corrected.
- `MIXED_GRAIN_INVALID` is now recoverable: details carry `compatible_measures` /
  `compatible_dimensions` / `closest_compatible_measure` (registry-only, bounded), a
  `replace_measure` hint names the closest workable measure (e.g. `revenue_usd` x product
  dimensions now points at `item_revenue_usd`), and calendar-date dimensions get a leading
  `use_time_grain` hint with a concrete `time` block instead of a dead end.

**Author ergonomics (from a cold-start authoring evaluation)**

- `semantic-rails init` output now passes `validate-config` with zero errors (the starter
  template used an entity name as `entity_key`, an unresolvable package-relative measure
  reference, and a mapping-form `domain:` the loader misparses).
- `catalog`, `discover`, `inspect`, `valid-values`, `plan`, and `build-options` accept
  `--path`, giving single-file/directory package authors the discovery surface that
  query-error hints already pointed them to.
- `parse-config --path <dir>` on a directory holding only a single-file `package.yml` now
  says to pass `--path <dir>/package.yml` instead of dead-ending on "graph.yml is missing".
- Single-file package artifacts now bundle the referenced seed SQL/post-SQL and sibling
  `examples/`/`tests/` directories (a check-passing artifact previously could not hydrate its
  own warehouse), with a structured error when a declared seed asset is missing;
  `validate-config` writes `.compiled/manifest.json` for single-file packages too.
- Docs re-measured against reality: corrected every envelope-size claim (`catalog full` is
  ~2.3MB, not "200KB+"), the catalog default tier, the compile-explain key paths
  (`explain.chosen_paths.<entity>.candidates`, not `explain.candidates`), authoring validation
  commands (`--path`, not the closed `--package` list), `entity_key` examples, the derived-ID
  grammar, and the directory `package.id` rule.
- `/mcp` on the ASGI app now honors the same bearer API keys as `/api/v1/*` — previously a
  deployment that configured `SEMANTIC_RAILS_API_KEYS` still exposed the MCP endpoint (including
  the `execute` tool) unauthenticated.

**`where` filter compilation**

- `where` filters with `value: null` and a negating op (`!=`, `<>`, `IS NOT`) now compile to
  `IS NOT NULL` instead of the always-empty `col != NULL`; ordering/LIKE ops against null are
  rejected with a structured `INVALID_QUERY` and recovery hint.
- `IN` / `NOT IN` with a bare scalar value no longer character-splits strings
  (`'Philadelphia'` became `IN ('P','h','i',...)`); scalars are normalized to a one-element
  list.
- The schema-advertised `where` ops `IS NULL`, `IS NOT NULL`, and `NOT IN` now compile
  end-to-end; an empty `IN` list compiles to constant `FALSE` (and empty `NOT IN` to `TRUE`)
  instead of erroring.

**`metric_filters` and expression AST**

- A bare comparison/boolean expression in `metric_filters[]` is now treated as the predicate
  itself instead of being compared to the envelope defaults and compiling to the always-false
  `(expr) IS NULL`.
- Boolean `not` expressions in `metric_filters` are now lowered as a real negation
  (`FALSE = (arg)`); previously the un-negated argument was silently emitted, inverting filter
  results. `not` with zero or multiple args is rejected with a structured error and a recovery
  hint to wrap multiple conditions in a single `and`/`or` arg.
- `BooleanExpr.op` is validated at parse time — only `and`, `or`, `not` (case-insensitive,
  normalized to lowercase) are accepted; unsupported ops fail `validate` with a structured
  `INVALID_EXPRESSION_AST` error including `allowed`, `closest_matches`, and `recovery_hints`
  instead of an opaque render-time token error.
- Malformed `metric_filters[]` items and `path_policy` payloads now return structured
  `INVALID_QUERY` errors with recovery hints instead of leaking bare AttributeError/TypeError
  as `INTERNAL_ERROR`.
- `rolling` / `prior_period` / `conversion` window payloads passed as an int (e.g.
  `window: 28`) raise `INVALID_EXPRESSION_AST` (or `CONVERSION_WINDOW_REQUIRED`) with a
  `USE_OBJECT_SHAPE` hint naming the `{unit, value}` shape, instead of crashing with a
  `TypeError` outside the structured-envelope contract.
- Every `_EXPRESSION_SHAPES` example round-trips through `parse_semantic_expression`
  (previously the `rolling`, `cumulative`, `conversion`, and `distribution` examples were
  unparseable), pinned by a test that iterates every shape.

**CLI**

- CLI error envelopes get the same `closest_matches` enrichment as the HTTP and MCP surfaces —
  a typo'd object id (e.g. `semantic-rails inspect --object-id metric.revenu`) suggests
  near-miss ids instead of claiming no matches exist.
- CLI failures print the structured error exactly once under `error` (previously the same issue
  was serialized three times per failure); exit codes and `ok`/`status` fields are unchanged.

**Build and tooling**

- `make release-check` / `make test` run through `uv run` instead of bare `python3`, which
  could not import `semantic_rails`.

#### Removed

- The pre-release `formulate` / `propose` / `parse-intent` / `expand` surface, collapsed into
  the single public `plan` tool and HTTP route (`parse_intent` remains an internal debug
  helper).
- Deprecated HTTP routes `/api/v1/resolve`, `/api/v1/valid-next`, and `/api/v1/classify`, and
  the corresponding CLI subcommands. The MCP-first surface (discover, inspect, build-options,
  plan) covers every previously-supported use case.
- The `explain` MCP tool, HTTP route, CLI subcommand, and `Runtime.explain` method. The full
  explain payload is available under `response["explain"]` on `compile`'s output, and most
  fields are flattened at the response root via `compile_response_metadata`.
- The internal workbench surface; the public v1 HTTP surface is locked at the 16 routes listed
  in [docs/QUERY_API.md](docs/QUERY_API.md#http-routes).
