# Architect MCP

Architect MCP is a developer-facing Semantic Rails MCP server for creating and managing package
projects from an MCP client. It is separate from the query MCP and does not start, stop, or
reconfigure cloud services.

## Commands

```bash
semantic-rails-architect-mcp --transport stdio
semantic-rails-architect-mcp --transport streamable-http --host 127.0.0.1 --port 8010
```

The default HTTP port is `8010` so the Architect server does not collide with the local API server
or the query MCP defaults.

From a source checkout, prefix the same commands with `uv run`.

## Recommended Flow

1. Call `architect_guidance` to get the current workflow, safety notes, and validation order.
2. Call `project_status` before editing an existing package and retain its
   `revision`.
3. Call `setup_project_dialog` to collect starter project answers. Clients that support MCP
   elicitation can run it interactively; other clients receive a structured dialog schema and draft
   `create_project` arguments.
4. Preview `create_project` with `expected_revision: absent`, `dry_run: true`,
   and a caller-generated `idempotency_key`; then repeat with `dry_run: false`
   after reviewing its exact file changes.
5. Use `upsert_model`, `upsert_relationship`, `upsert_metric`, `upsert_segment`, or scoped file
   tools with the latest project revision. Generate a new idempotency key for each
   logical mutation and reuse that key only when retrying the identical call.
6. Run `validate_project` with `mode=parse` after structural edits and `mode=runtime` before
   trusting queries.
7. Run `impact_project` with `compare_path` or `base_ref` before release review; use
   `promotion_check` with `compare_path` or `base_ref` when an environment gate matters.

The terminal REPL exposes the same abstraction upserts through `author model`,
`author dimension`, `author time`, `author measure`, `author metric`, and
`author segment`. Use the REPL when a person benefits from recommended choices,
similar-definition warnings, a pre-write YAML preview, and session-local
`undo`; use the MCP tools when an MCP client is orchestrating the same work.

## Warehouse Introspection

Four read-only tools look at a DuckDB database before or while you model it. Pass `duckdb_path`
(a file inside the workspace, for example the one `dbt build` wrote) or `project_path` (a DuckDB
package: its `default_db`). They open the file read-only and never create, seed or change it.

- `list_tables`: tables and views, optionally for one `schema`, with column counts.
- `describe_table`: columns with types, nullability and defaults, and declared primary, unique and
  foreign keys.
- `profile_columns`: row, distinct and null counts, min/max and up to 20 sample values per column
  (`sample_limit`, default 5; `0` returns none). At most `max_rows` rows are scanned (default one
  million; a uniform sample beyond that, reported as `sampled`).
- `suggest_model`: a key, time roles, dimensions, measures with an aggregation and foreign-key
  links, each with a `confidence` (`high`, `medium`, `low`) and a `reason`. Declared keys come first,
  then uniqueness in the data, then names; foreign keys are checked for rows with no match. It also
  returns draft `upsert_model` arguments (low-confidence choices left out) to review before
  calling `upsert_model`.

Profiles and samples show real values from the warehouse; use `sample_limit: 0` where that matters.
The same functions are available to Python callers in `semantic_rails.architect_introspection`.

## dbt Projects

`suggest_models_from_dbt` reads a dbt project's artifacts; it never runs dbt. Pass `target_dir`
(dbt's `target/`, inside the workspace) or `manifest_path`, and optionally `catalog_path`; `select`
narrows the models by name. Run `dbt build` first, and `dbt docs generate` for `catalog.json`,
which carries column types (without it, columns the manifest does not type are reported as
`untyped_columns`).

Each dbt model becomes a suggestion in the same shape as `suggest_model`, but the facts come from
dbt:

- the relation is the model's `schema.alias` (the database too when it is not the project's
  default), so a model in a custom schema keeps it (`main_marts.fct_orders`);
- the key comes from an enforced contract's `primary_key` constraint, a
  `dbt_utils.unique_combination_of_columns` test, or `unique` + `not_null` tests on one column;
- foreign keys come from `relationships` tests and contract `foreign_key` constraints;
- `accepted_values` tests become dimension value sets (`domain`);
- descriptions carry into the draft; times, dimensions and measures come from column types and
  names.

`import_dbt_project` applies them: `select` names the dbt models, and one parse-gated transaction
creates or updates a model per dbt model (in `models/<group>/`, `group` defaulting to `dbt`), with
each `relationships` test written as an entity reference in the model's `entities:` block (`expr:`
when the foreign-key column is named differently from the target's key), which the engine reads as
a many-to-one relationship. It follows the usual mutation contract (`expected_revision`,
`idempotency_key`, `dry_run`). A model whose target is imported in the same call, or already in the
package, gets the reference; references elsewhere are listed in `skipped_references`, and dbt models
without a key in dbt in `skipped_models`. A dbt model whose derived id matches a package model
(`fct_orders` and a model `orders` for entity `order`) updates that model.

Python callers use `semantic_rails.dbt_artifacts` (`load_dbt_artifacts`,
`suggest_models_from_dbt`, `dbt_import_models`) and `ArchitectProject.upsert_models`, which stages
several models and their references in one transaction.

## Relationships

`upsert_relationship` relates two entities through key columns. `columns` are `from_entity`'s
columns holding `to_entity`'s key; strict packages relate to keys, so `to_columns`, when given, must
be that key. The columns are written to `from_entity`'s model as an entity reference (`expr:` when
named differently from the key), which the engine reads as a safe many-to-one relationship with the
id `relationship.<model>_<entity>` (for example `relationship.orders_customer`).

Rules beyond that default go in `graph.relationships.<name>`. The tool writes that entry when you
pass `cardinality: one_to_one`, a `name`, `allowed_directions` (`forward`, `reverse`), `safety`
(`safe`, `requires_rewrite`, `unsafe`), `path_preference` (lower is preferred; the default is 100),
`label` or `description`. The entry keeps the inferred id, so routes pinned in
`graph.path_preferences` still resolve. An existing entry for the same pair is updated in place and
keeps the settings you do not pass (its `via:` follows the new columns). The tool refuses a pair
with several entries, an entry declared from the other side, and an entry that joins on columns
other than the target's key. `one_to_many` is recorded from the many side: `to_columns` are then
the foreign key on `to_entity`'s model, and `allowed_directions` keep the meaning you gave them.
`many_to_many` is refused, because it needs a bridge: model the link table and relate it
many-to-one to each side. A model whose `bridge` option is off infers no joins, so its
relationships are always written as explicit entries (`bridge` is never an entity name).

The usual mutation contract applies (`expected_revision`, `idempotency_key`, `dry_run`), and the
parse gate rolls back a change the package cannot load, such as one that breaks a pinned route.
Python callers use `ArchitectProject.upsert_relationship`.

## Examples, Package Tests and Query Previews

`upsert_example` writes an example question to `examples/<file_name>` (`core.yml` by default):
`spec` takes `question`, `query` and optionally `expected_shape` (`columns`, `min_rows`,
`max_rows`). `upsert_test` writes a package test to `tests/<file_name>`; `spec.kind` is one of:

| Kind | Fields |
|---|---|
| `query_returns_columns` | `query`, `columns` |
| `query_row_count_bounds` | `query`, `min_rows` and/or `max_rows` |
| `query_matches_snapshot` | `query`, `expected_rows` |
| `validate_fails_with_code` | `query`, `code` |
| `explain_contains` | `query`, `text` |
| `metric_equals_query` | `metric_query`, `expected_query` |

Both tools compile every query against the package before writing and refuse one that does not
compile. A `validate_fails_with_code` query must fail with its `code`. Both merge into an existing
entry, which stays in its file, unless `replace` is true. `upsert_test` with `capture_snapshot:
true` runs a `query_matches_snapshot` query against the warehouse and writes its rows (at most 200)
as `expected_rows`. Snapshots compare numbers by value, so a DECIMAL result such as `12.7500` matches
the `12.75` YAML holds.

`preview_query` runs a semantic query against the package's warehouse and returns at most
`max_rows` rows (default 20, at most 200), with `truncated` when there were more. Its values are
real warehouse data; like runtime validation, it may build a seeded DuckDB database.

## Removing and Replacing Objects

`remove_object` removes a model, dimension, time, measure, metric, segment, relationship, example or
test, and keeps its YAML under `.architect/archive/<id>/removed.yml` (with the `reason` you give).
Pass `model` when a dimension, time or measure key is on several models, and name a relationship
the way `upsert_relationship` reports it (`orders_customer`, or its id). Every definition of the key
goes, including ones another file shadows, so an older definition can't take its place. Removing a
model also removes its entity, its relationships, other models' references to that entity, and
`graph.relationships` entries naming it. A file left with nothing in it is deleted. The exception
is a root file such as `metrics.yml`, which stays behind empty so it keeps masking `package.yml`'s
block.

Every removal is checked against the package before anything is written, without querying the
warehouse. Measures, metrics (also over their time axis), segments, examples and tests are compiled
as they are and as they would be. A removal that would stop a measure, metric or segment from
compiling is refused and names them, so remove or change those first. The report's `impact` has
four parts:

- `broken`: the examples and tests the removal breaks;
- `rerouted`: queries that still compile, but to different SQL (a join that would take another
  path);
- `references`: the authored files that still mention a removed id;
- `behavior`: the behaviour changes against the current package, as in `impact_project`.

Preview with `dry_run: true`; the usual mutation contract applies, and a retried removal replays.

`upsert_model` and `upsert_metric` merge into an existing object by default. With `replace: true`,
`upsert_metric` writes `spec` as the whole metric, and `upsert_model` rewrites the model from its
arguments, keeping only its entity references and calendar. The dimensions, times and measures a
replace drops are reported as `dropped`, and its other dropped fields as `dropped_fields`. A replace
is checked the same way as a removal. `upsert_model` doesn't manage fact models (`kind: fact`), and
it takes a `label`.

## Package Calendar

`upsert_model` with `calendar: true` makes the model's entity the package calendar. The graph
entity gets `kind: time` and `allowed_as_root: false`, and the model gets a `calendar_id` (`default`
unless you pass one). Calendar ids are lowercase letters, digits and underscores. A package has one
calendar per `calendar_id`, and needs a `default` calendar once it has any, because a query without
a `calendar_id` fills from it. Only a calendar entity may declare `kind: date` dimensions.

`time.fill` and calendar bucketing read the calendar relation's `date_day`, `week_start`,
`month_start`, `quarter_start` and `year_start` columns. Declare `date_day` under `times:` with
`class: calendar_time`, and the coarser grains as `kind: date` dimensions, as
`configs/semantic_rails/jaffle_shop/models/core/calendar.yml` does.

`calendar: false` makes a calendar a regular entity again, once its `kind: date` dimensions have been
removed; leaving `calendar` out keeps the entity as it is. On a regular model, `calendar_id` binds
the model's times to an existing calendar, as `time.calendar_id` queries expect.
`ArchitectProject.upsert_models` items take the same fields.

## Tool Surface

- `architect_guidance`
- `setup_project_dialog`
- `create_project`
- `project_status`
- `list_project_files`
- `read_project_file`
- `write_project_file`
- `upsert_model`
- `upsert_relationship`
- `upsert_metric`
- `upsert_segment`
- `upsert_example`
- `upsert_test`
- `preview_query`
- `remove_object`
- `archive_project_file`
- `validate_project`
- `diff_project`
- `impact_project`
- `promotion_check`
- `mcp_client_config`
- `list_tables`
- `describe_table`
- `profile_columns`
- `suggest_model`
- `suggest_models_from_dbt`
- `import_dbt_project`

## Safety Model

All file writes are scoped to the configured workspace root. The default workspace root is the
repository root; pass `--workspace-root` when an MCP client should operate in a separate project
workspace.

Comparison paths for `diff_project`, `impact_project`, and release checks must also resolve to
package directories inside the configured workspace root. Use `mcp_client_config` to get a launch
configuration that includes both `cwd` and `--workspace-root`.

Runtime validation is operational. DuckDB validation can create the package database from its
seed, and rebuilds it only when the package's own seed built it and nothing has changed it since.
A database another tool builds (for example dbt) declares `seed: {kind: external}` and is never
created or replaced. Snowflake validation can issue live queries through the configured Snow CLI
connection. Use `mode=parse` for a no-query authoring check.

Every mutation tool (`create_project`, raw write, the upserts, `import_dbt_project`, and archive)
uses one engine-owned transaction layer:

- `project_status` computes a deterministic `sha256:` revision over authored
  project files. Internal transaction receipts, archives, locks, generated
  databases, and cache files are excluded.
- `expected_revision` is required at the MCP boundary. A stale writer receives
  `CONFIG_CONFLICT` with both expected and current revisions; it never
  overwrites an intervening edit.
- `idempotency_key` is required and persisted as a hashed, workspace-local
  receipt. Retrying the identical mutation replays its result. Reusing the key
  for a different intent fails closed.
- A per-project OS file lock serializes cooperating processes. Multi-file
  replacements occur under that lock, parse as one package, and restore every
  prior byte if any write or parse step fails.
- `dry_run: true` validates a temporary virtual project and reports exact
  proposed content, unified diffs, hashes, and the proposed revision without
  writing the project or consuming the idempotency key.

Exact existing keys are updated in their current source file instead of
creating duplicate definitions elsewhere. Successful internal REPL mutations
retain transaction snapshots for one-step undo.

The generated stable interface artifact is
`semantic_rails/contracts/architect_mcp.v1.json` (mirrored under `schemas/`).
It pins tool input/output schemas, annotations, protocol versions, and
transaction capabilities. Run
`python scripts/generate_contract_artifacts.py --check` to reject drift.
