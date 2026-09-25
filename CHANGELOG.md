# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

Pending changes live as fragments in [`changelog.d/`](changelog.d/) until the next release.

## 0.3.0 — 2026-09-25 — Guided authoring, warehouse import and a leaner query MCP

### Added

- The Architect MCP's `upsert_model` takes `calendar: true`, with an optional
  `calendar_id`, to make a model the package calendar: its entity becomes
  `kind: time` and not a query root, and may carry date dimensions, so
  `time.fill` works in packages authored through the MCP.
- One project scaffold, `architect_service.create_project(path, ProjectSpec)`,
  shared by the Architect MCP, `semantic-rails init` and the REPL. It is
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
  minimal responses. `capabilities` and `build-options` are v1-only; calling a
  v1-only tool on v2 returns `UNKNOWN_MCP_TOOL` naming the v2 call. The contract
  is `query_mcp.v2.json`, and `initialize` reports the interface as
  `serverInfo.version`. Interface v1 is unchanged and stays the default.
- To move a v1 client to v2, call `execute(query, mode="validate")` for
  `validate`, `mode="sql"` for `compile`, `segment(segment_id, action=...)` for
  the segment tools and `discover(terms="")` for `catalog`; pass `max_rows` (up
  to 100,000) for more rows, `verbosity="compact"` for full `discover` and
  `inspect` cards, `detail="best"` for v1's `plan` response and
  `verbosity="full"` for v1's segment responses. See "Interface v2" in
  [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md).
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
- Add `semantic-rails://catalog/index` for counts and ids per kind and
  `semantic-rails://capabilities/summary` for tool names and titles. These compact resources are
  opt-in. Existing v1 `catalog/summary` keeps its descriptive rows and `counts_total`, and
  `capabilities` keeps complete tool definitions for existing consumers.
- `create_optional_fastmcp_server` selects `MCPServer` when the MCP Python SDK 2.x module is
  present, or `FastMCP` on the installed 1.x SDK. The 2.x branch is covered by a simulated
  module test; an SDK 2.x install has not been qualified for the full package.
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
