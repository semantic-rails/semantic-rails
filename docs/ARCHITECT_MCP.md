# Architect MCP

Architect MCP is a developer-facing Semantic Rails MCP server for creating and managing package
projects from an MCP client. It is separate from the query MCP and does not start, stop, or
reconfigure cloud services.

## Commands

```bash
semantic-rails-architect-mcp --transport stdio
semantic-rails-architect-mcp --transport streamable-http --host 127.0.0.1 --port 8010 \
  --token-file ~/.config/semantic-rails/architect.token
```

The default HTTP port is `8010` so the Architect server does not collide with the local API server
or the query MCP defaults.

### Network transports

The `streamable-http` transport can write files, so it requires a bearer token and
refuses to start without one. The supported transports are stdio and Streamable HTTP. The server reads it from `--token-file`, then the file named by
`SEMANTIC_RAILS_ARCHITECT_TOKEN_FILE`, then `SEMANTIC_RAILS_ARCHITECT_TOKEN`. A token is at least 32
characters of letters, digits and `- . _ ~ + /` (optionally ending in `=`). Create one without
printing it:

```bash
mkdir -p ~/.config/semantic-rails && (umask 077 && python3 -c \
  "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/semantic-rails/architect.token)
```

Clients send `Authorization: Bearer <token>`; every other request, including a browser's CORS
preflight, gets `401`. `mcp_client_config` returns the header as a template,
`Bearer ${SEMANTIC_RAILS_ARCHITECT_TOKEN}`, for clients that expand environment variables, and never
the token itself. To give such a client the token without printing it:

```bash
export SEMANTIC_RAILS_ARCHITECT_TOKEN="$(cat ~/.config/semantic-rails/architect.token)"
```

The server binds to `127.0.0.1` by default. Host and Origin (DNS-rebinding) checks apply to every
network bind: requests must name a loopback host (`127.0.0.1`, `localhost`, `[::1]`) or the
address given to `--host`, with or without a port, and a browser `Origin` must be `http://` on one
of those names. A wildcard bind (`0.0.0.0`, `::`) therefore serves only clients that reach it
through a loopback name, such as a container port-forwarded to `localhost`. The token travels in
clear text over plain HTTP, so reach a server on another machine through an SSH tunnel or a TLS
proxy rather than binding it to a network address. The stdio transport needs none of this.

From a source checkout, prefix the same commands with `uv run`.

## Recommended Flow

1. Call `architect_guidance` to get the current workflow, safety notes, and validation order.
2. Call `project_status` before editing an existing package and retain its
   `revision`.
3. Call `setup_project_dialog` to collect starter project answers, including the warehouse and
   how to connect to it. Clients that support MCP elicitation can run it interactively; other
   clients receive the questions (with `when` conditions and choices) and draft
   `create_project` arguments. That schema-only draft is for DuckDB; select a warehouse and
   complete its conditional connection answers before using it. The elicitation form accepts
   `connection_options` as a JSON object string and returns it as a parsed object in the
   `create_project` draft. For a non-DuckDB warehouse the dialog sets `data: external` and
   clears `default_db`. Missing or invalid connection details return `ok: false` with
   `status: needs_connection_details` and `required_answers`, without echoing the submitted
   answers or a draft.
   The dialog normalizes warehouse names and checks each adapter's required input groups:
   Databricks needs host, HTTP path and token; MotherDuck needs database and token; DuckLake
   needs a catalog path; and Athena needs region and an S3 staging directory. Snowflake accepts
   a named connection or valid native direct options. Named profiles are not offered for the
   other adapters. This checks whether setup answers are complete, not whether referenced
   environment variables, files, or warehouse services are available at runtime. Guided
   Postgres setup asks for an explicit connection option even though libpq can use ambient
   defaults; BigQuery and ClickHouse can use their documented ambient/local defaults.
4. Preview `create_project` with `expected_revision: absent`, `dry_run: true`,
   and a caller-generated `idempotency_key`; then repeat with `dry_run: false`
   after reviewing its exact file changes.

`create_project` uses `architect_service.create_project` with a `ProjectSpec`, and the CLI's
`init`, `project new` and `setup --interactive` write the same scaffold files. It writes a strict
package (`schema_strict: true`) with one model, its count and amount metrics, an example, a
package test and a `.gitignore` for build outputs.

- DuckDB with `data: starter` (the default) adds a two-row CSV seed, so the package runs at once.
  Starter names are made safe (`Raw Events` becomes `raw_events`).
- DuckDB with `data: external` reads a database another tool builds, such as dbt
  (`seed.kind: external`, `default_db` defaulting to `data/<package_id>.duckdb`). Names must match
  the warehouse: `relation` may be schema-qualified (`main_marts.fct_orders`) and is never renamed.
  Pass each component as its raw name (for example `sales-data.fct_orders`); SQL rendering quotes
  components that need it. The name must not contain a path separator or control character.
  The model gets only the columns you name (no starter dimension or amount). The database may
  already sit in the project directory: a directory with no authored files still has revision
  `absent`. Keep the database inside the package (for example, point the dbt profile's `path` at
  `<package>/data/<package_id>.duckdb`); a path outside it needs
  `SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS=1`.
- Other warehouses take `connection_kind` and their adapter's required connection inputs.
  Named connections apply only to Snowflake; other adapters use `connection_options` or their
  documented ambient defaults. Name secrets by environment variable only; unsupported or
  malformed connection options are rejected before scaffold files or transaction receipts are
  created, without returning their values.
5. Use `upsert_model`, `upsert_relationship`, `upsert_metric`, `upsert_segment`,
   `upsert_example`, `upsert_test`, or scoped file tools with the latest project revision. Generate a new idempotency key for each
   logical mutation and reuse that key only when retrying the identical call.
   `upsert_relationship` relates `from_entity` to `to_entity`: `columns` on `from_entity`'s
   model hold `to_entity`'s key, in key order, and go in that model's `entities` block (as
   `expr` when named differently). That is a many-to-one relationship; `cardinality:
   one_to_one` also records it in `graph.relationships`. Relate one-to-many from the many
   side, and many-to-many through a bridge model related to each side. A model that already
   relates `to_entity` in a legacy `joins:` or `keys.foreign:` block is refused, since that
   block would override the columns, and so is a pair that may have several roles
   (role-playing keys: several `graph.relationships` entries in either direction, or an entry
   with its own `via` beside the model's foreign key): edit those in `graph.yml`.
   To add a relationship while preserving existing routes, pass `keep_existing_routes: true`.
   The same transaction records each moved pair's previous path and confirms newly ambiguous
   own-key routes. Its `kept_route_decisions` lists the rows recorded. It refuses if a previously
   answered pair still changes, or if generated decisions change a previously refused pair's
   outcome compared with the relationship alone. The relationship's own new answers are allowed.
   `record_route_decision(source_entity, target_entity, relationship_path, label="")` records
   which route a question between two entities means, as the package default: it writes the
   pair's row in the `path_preferences` list the loader reads (a top-level list in `package.yml`
   wins over `graph.path_preferences`), replacing every row for exactly the pair however its
   entities are spelled; a row for the reverse pair is never touched. Pass the `decision` of the
   option a person chose from an `AMBIGUOUS_PATH` refusal's `details.clarification`. The row is
   checked by the loader's rules (an unknown entity or relationship, a broken chain, a disallowed
   direction, or a path that doesn't end at the target), then the changed package is loaded and
   must resolve the pair to exactly that route: otherwise it is `INVALID_CONFIG` (a row it
   disagrees with is named in `details.rows`) and nothing is written. The result adds `replaced`
   (the row in effect before, or `null`) and `summary`, one plain sentence for the review ("For an Account,
   'District' now means the District of the Account's Branch. Other meanings: the District of
   the Account's Owner."). Preview it with `dry_run: true` like every other write.
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
package: its `default_db`). They open the file read-only with DuckDB external access disabled and
never create, seed or change it.

- `list_tables`: tables and views, optionally for one `schema`, with column counts. The MCP tool
  returns at most 200 and sets `truncated` when there are more; narrow with `schema`.
- `describe_table`: columns with types, nullability and defaults, and declared primary, unique and
  foreign keys, with referenced relations schema-qualified when needed.
- `profile_columns`: row, distinct and null counts, min/max and up to 20 sample values per column
  (`sample_limit`, default 5; `0` returns none). At most one million rows are profiled;
  `max_rows` can lower that cap. Larger tables use a uniform sample, reported as `sampled`.
  The full row count and the sample's row count are reported separately; counting the full relation
  may inspect all its rows.
- `suggest_model`: a key, time roles, dimensions, measures with an aggregation and foreign-key
  links, each with a `confidence` (`high`, `medium`, `low`) and a `reason`. Declared keys come first,
  then uniqueness in the data, then names. For tables above the profile cap, a key that appears
  unique and non-null in the sample is a low-confidence candidate; the draft includes it for
  review, and its reason requires full-relation confirmation before applying. Composite-key probes
  check at most the first one million rows and eight key-like columns. A declared foreign key is
  authoritative. Otherwise a key-like column (`id`, or ending in `_id`, `_key`, `_code`, `_sk`)
  links to each other relation whose declared single-column primary key has the same name and a
  compatible type; values are not compared, so such a link is `medium`, or `low` when several
  relations declare that key. Single-column links use `column`; composite links use `columns` and
  preserve the ordered local and referenced columns. Foreign-key links are review evidence, not
  arguments in the draft `upsert_model` call.
  A numeric column named like a count of distinct people (`unique`, `uniques`, `distinct`,
  `visitors`, `users`, `cloners`, but not an average or rate of one) is a `low`-confidence measure
  whose suggestion carries `additive: false` and says why: a vendor's pre-counted uniques can't be
  added up across days or pages. The draft `upsert_model` call doesn't set `additive` (it still
  sums the measure); declare it yourself when the column really is a distinct count.
  A time column named like a snapshot's as-of time (`snapshot`, `as_of` or `asof` in its name)
  is drafted `class: as_of_time`, so a stock on it whose key lacks the column is refused rather
  than summing snapshots; other times are drafted `event_time`.
  Container columns (arrays, lists, structs, maps and similar types) are omitted from scalar model
  roles and listed in `unsupported_columns`; model them with an explicit supported extraction
  expression. Enum labels containing container names or brackets remain scalar dimensions.
  Declared warehouse keys and foreign keys retain their declared evidence. All
  suggestions return draft `upsert_model` arguments to review before calling `upsert_model`.
  Draft measure expressions use structured column references, so a physical column named like an
  arithmetic expression is read as that column.

Profiles and samples show real values from the warehouse; use `sample_limit: 0` where that matters.
Tables and views backed by data stored in the DuckDB file work normally. A view that needs an
external file or resource can still appear in metadata-only list/describe results, but cannot be
profiled or used for a model suggestion; materialize it in the DuckDB file before introspection.
Warehouse tools spell a relation the way package models do: `table` in the default schema,
otherwise `schema.table`, with each name as it is (`sales-data.fct"orders`); the runtime quotes
each part. Pass the listed `relation` to describe, profile or suggest; the draft `upsert_model`
uses the same spelling. Because the runtime splits relations on dots, a schema or table whose name
contains a dot is not listed and is never a foreign-key candidate; model an undotted view over it.
`list_tables.schema` and its returned `schema`/`name` fields are raw names.
The same functions are available to Python callers in `semantic_rails.architect_introspection`.

## dbt Projects

`suggest_models_from_dbt` reads a dbt project's artifacts; it never runs dbt. Pass `target_dir`
(dbt's `target/`, inside the workspace) or `manifest_path`, and optionally `catalog_path`; `select`
narrows the models by name; without it, the MCP tool returns the first 20 models and sets
`truncated` when there are more. Run `dbt build` first, and `dbt docs generate` for `catalog.json`,
which carries column types (without it, columns the manifest does not type are reported as
`untyped_columns`). The final manifest and catalog files must resolve inside the workspace;
in-workspace links are supported. Container catalog types are also reported in `untyped_columns`
and excluded from draft scalar measures.

Each dbt model becomes a suggestion in the same shape as `suggest_model`, but the facts come from
dbt:

- the relation is the model's `schema.alias` (the database too when it is not the database most
  models are built in), so a model in a custom schema keeps it (`main_marts.fct_orders`); with
  dbt-duckdb, a model in the default schema `main` is spelled `alias`, as introspection spells it;
- the key comes from an enforced contract's `primary_key` constraint, a
  `dbt_utils.unique_combination_of_columns` test, or `unique` + `not_null` tests on one column;
- foreign keys come from `relationships` tests and model- or column-level contract `foreign_key`
  constraints. Targets resolve against manifest identities for `ref()`, package-qualified `ref()`,
  `source()`, and relation names including alias, schema and database; target columns are preserved;
- `accepted_values` tests become dimension value sets (`domain`);
- descriptions carry into the draft; times, dimensions and measures come from column types and
  names.

An ephemeral dbt model is a CTE without a physical warehouse relation. Its suggestion reports
`physical_relation: false` and `nonphysical_reason`, with no `upsert_model` draft;
`import_dbt_project` lists it in `skipped_models` even when dbt tests establish a key.

Both dbt tools return `dbt_warnings` for tests whose attachment or relationship target cannot be
identified uniquely from the manifest. Such tests do not create a foreign key. With no
`attached_node`, a relationship test uses the resolved `ref()` or `source()` target identity and a
single remaining relation dependency to identify the child; dependency order is not an identity.
Singular tests, and generic tests other than those listed above, carry no model facts and are
ignored.

`import_dbt_project` applies them: `select` names the dbt models, and one parse-gated transaction
creates or updates a model per dbt model (in `models/<group>/`, `group` defaulting to `dbt`), with
each resolved foreign key written as an entity reference in the model's `entities:` block (`expr:`
when the foreign-key column is named differently from the target's key), which the engine reads as
a many-to-one relationship. It follows the usual mutation contract (`expected_revision`,
`idempotency_key`, `dry_run`). A model whose target is imported in the same call, or already in the
package, gets the reference; references elsewhere are listed in `skipped_references`, and dbt models
without a key in dbt in `skipped_models`. A dbt model whose derived id matches a package model
(`fct_orders` and a model `orders` for entity `order`) updates that model. An update adds only the
dimensions, times and measures the model doesn't have yet and leaves the existing ones as
authored, listing them in the model's `kept_objects` in `models`, so a re-import doesn't revert
those objects (a stock accumulation, a clock's class, `additive: false`) or refresh their dbt
descriptions; change an existing object with `upsert_model`. The model's relation, its entity's
key and its foreign-key entries still follow dbt: after a re-import, check a key you changed
(for example a snapshot table keyed by its series and snapshot date).
Measure keys are package-wide, so a drafted measure whose key another model already has, in the
package or earlier in the call, gets its entity as a prefix (`order_line_usd_to_local_rate`), like
the drafted `<entity>_count`, unless that key is taken too; a re-imported model keeps its own keys.
A foreign key to a dbt model imported in the same call names that model's entity, even when
another package model reads the same relation. Any other foreign key resolves only when exactly
one existing package entity reads its relation; otherwise it is listed in `skipped_references`.
If a referenced dbt target was explicitly selected but skipped, its child reference is also
reported in `skipped_references`; an older package model at the same relation cannot replace it.
References to intentionally unselected targets may still use one eligible existing entity.
If a model has different foreign-key columns pointing to the same semantic entity, both links
are reported in `skipped_references` because one entity reference cannot represent both joins.
Identical links are recorded once.

Python callers use `semantic_rails.dbt_artifacts` (`load_dbt_artifacts`,
`suggest_models_from_dbt`, `dbt_import_models`) and `ArchitectProject.upsert_models`, which stages
several models and their references in one transaction.

## Metric and Segment Files

`upsert_metric` puts a new metric in `metrics/<group>/<metric_key>.yml`, or in `metrics/<file_name>`
when you pass `file_name`, so several metrics can share one file. `upsert_segment` puts a new
segment in `segments/<file_name>` (default `core.yml`). An existing metric or segment stays in its
file. Adding one to a file that holds a single object (`metric:`, `segment:` or a bare mapping)
turns the file into a `metrics:` or `segments:` block that keeps that object under its `name`, `id`
or file name, the key the loader reads it by.

`upsert_segment` refuses a segment whose `membership:` has no `where`, `metric_filters` or `time`
window, since it would select the whole population.

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

`calendar: false` makes a calendar a regular entity again once it has no `kind: date` dimensions;
until then the parse gate rolls the change back. Leaving `calendar` out keeps the entity as it is.
On a regular model, `calendar_id` binds the model's times to an existing calendar, as
`time.calendar_id` queries expect. `ArchitectProject.upsert_models` items take the same fields.
Calendar checks run after receipt replay and the revision check, so a retried call replays and a
stale one gets `CONFIG_CONFLICT`.

## Removing Objects

`remove_object` removes a `model`, `dimension`, `time`, `measure`, `metric`, `segment` or
`relationship` in one parse-gated transaction and keeps what it removed in
`.architect/archive/<id>/removed.yml`, which `undo` deletes again.

- A relationship is a model's foreign-key reference to another entity (what `upsert_relationship`
  writes): `key` is that entity, and a `graph.relationships` entry for the pair goes too. `model`
  picks the model when several hold a dimension, time, measure or relationship with that key.
- A model takes its dimensions, times and measures, its entity, other models' references to that
  entity, and the `graph.relationships` entries naming it.
- Every definition goes, including ones an override shadows, so none resurfaces. An emptied root
  `metrics.yml` or `segments.yml` stays, so it keeps masking `package.yml`.

The parse gate rolls back a removal that breaks the package, such as a dimension a segment filters
on. It loads a metric whose measure or input metric is gone, which fails only when queried, so
`remove_object` refuses a removal that leaves a metric naming a removed object and names those
metrics. The report lists `removed`, and `impact` holds the `impact_project` summary of the change
(`risk`, `impacted_metrics`, `changes`) plus `references`: authored files, such as examples and
tests, that still name a removed id. Removals use the same preservation guard as other writes:
a removed route may leave an answered pair refused, but switching it to another answer requires
an explicit decision or the removal refuses with `ROUTE_DECISION_NOT_RECORDED`. The result's
`route_changes` lists every pair whose resolution changes (see [Join Routes](#join-routes)).

## Upgrading a package

`upgrade_project(project_path, dry_run=true, choices={}, expected_revision="",
idempotency_key="")` rewrites a package's legacy forms in one transaction; it is
[`semantic-rails project upgrade`](PACKAGE_AUTHORING.md#upgrading-a-package) as a tool, with the
same report: `rules` (each with its tier and hits), `proof`, `choices_pending`, `changes` and
`next_actions`.

- `project_path` accepts a package directory with `package.yml` or a single-file package.
- A dry run, the default, needs no revision or key. A write needs `expected_revision` (the preview
  returns it as `revision`) and a new `idempotency_key`, like every other write.
- A rewrite that changes the semantic fingerprint or an example's or test's compiled SQL is refused
  with `CONFIG_CONFLICT` and `details.conflict_kind: "upgrade_not_equivalent"`; nothing is written.
- Pending choices return `status: "choices_pending"` and `ok: false`, and write nothing; answer each
  with `choices: {key: option}`.
- Findings with no edits or options are stops. Their rules have tier `unverified` and a `reason`
  naming the finding; the preview returns `status: "unverified"` and `ok: false`. They are excluded
  from `choices_pending`, which still lists real choices. A write refuses with `CONFIG_CONFLICT`
  and writes nothing; resolve the named definition by hand.
- When a write comes back `rolled_back_after_parse_error` or `preview_invalid`, or `project_status`
  can't parse the package, and upgrade rules match the package's files, `next_actions` says how many
  legacy forms `upgrade_project` would rewrite.

## Join Routes

Which route between two entities a question means is a business definition (see
[the route census](PACKAGE_AUTHORING.md#route-census-and-route-changes)).

- `project_status` returns `route_census` once, outside `parse`: `undecided` lists the
  entity pairs a question can need that are refused until a `graph.path_preferences` row records
  their route, each with the `AMBIGUOUS_PATH` refusal's `details.clarification.options`
  (pass an option's `decision` to `record_route_decision`); `assumed` lists the multi-route pairs
  answered by the start entity's own key, to confirm. While pairs are undecided, `next_actions`
  starts with deciding them. `create_project` and `setup_project_dialog` say to decide them once
  entities are related.
- `promotion_check` lists them under `advisories` (`ROUTES_UNDECIDED`), never as a blocker.
- Before writing, the transaction compares the package with the change applied. A change that
  would refuse an answered pair whose route still exists, or answer it by another route, refuses
  with `ROUTE_DECISION_NOT_RECORDED` until the author records an explicit decision. Previews use
  the same guard. Without an explicit keep choice, nothing is written and no route rows are
  generated. The refusal's
  `details.route_changes` lists affected pairs. The message names the explicit
  `graph.path_preferences` fields (`source_entity`, `target_entity`, `relationship_path`),
  without suggesting rows. For `upsert_relationship`, retry with `keep_existing_routes: true`
  to record the previous routes within the relationship write. Otherwise choose routes and
  call `record_route_decision(decisions=[...])` before adding the relationship, or include chosen
  rows in the authored change. Census and guard
  comparisons use package decisions independently of active query route overrides. An ordinary
  change that moves an inherited answer needs that pair's own decision.
- For several pairs, call `record_route_decision(decisions=[...])` with a nonempty list of
  rows containing `source_entity`, `target_entity`, `relationship_path`, and optional `label`,
  instead of single-pair fields. All replacements are staged together and the final package
  must honor every row. One invalid, duplicate, or conflicting row refuses the entire batch;
  no files or receipts are written. This lets forward and reverse decisions be changed together.
  The result's `route_decisions` lists each row, its `replaced` value, and its `summary`.
  Revision checks, idempotent retries, and write-free previews apply to the whole batch.
- `record_route_decision` uses the loader location: top-level
  `package.yml` `path_preferences`, else the file holding `graph`. That file is rewritten as
  Architect YAML, dropping comments.
- A removed route or one beyond the new hop ceiling may leave a pair refused; answering it by
  another route without its own row refuses the mutation with `ROUTE_DECISION_NOT_RECORDED`.
- `record_route_decision` deliberately changes the default.
  `remove_object` uses the same preservation guard. The result's `route_changes` lists
  every pair that resolves differently, including refused → answered, as `impact_project`
  does. `base` and `head` hold the route or refusal code, without suggested recovery rows.
  `route_decisions_added` is empty.
  `create_project` and undo skip the guard: one starts a package, the other restores files exactly.
- An explicit path records the route's meaning. It does not guarantee unchanged orphan-key
  values when adding a second foreign-key role changes a source-key read to a lookup. See
  [the authoring guide](PACKAGE_AUTHORING.md#route-census-and-route-changes) and compare the
  chosen answer with reference SQL.

## Examples, Package Tests and Query Previews

`upsert_example` writes an example question to `examples/<file_name>` (`core.yml` by default):
`spec` takes a `query`, and optionally a `question` and an `expected_shape` (`columns`,
`min_rows`, `max_rows`). `upsert_test` writes a package test to `tests/<file_name>`; `spec.kind`
is one of:

| Kind | Fields |
|---|---|
| `query_returns_columns` | `query`, `columns` |
| `query_row_count_bounds` | `query`, `min_rows` and/or `max_rows` |
| `query_matches_snapshot` | `query`, `expected_rows` |
| `validate_fails_with_code` | `query`, `code` |
| `explain_contains` | `query`, `text` |
| `metric_equals_query` | `metric_query` (or `query`), `expected_query` |

Both tools refuse an entry the test runner would crash on or pass without checking anything,
such as a test without `columns` or bounds, and validate every query against the package without
running it. A `validate_fails_with_code` query must fail with its `code`. `spec` merges into an
existing entry, which stays in its file. These checks run after receipt replay and the revision
check, so a retried call replays and a stale one gets `CONFIG_CONFLICT`. Python callers use
`ArchitectProject.upsert_check(kind="example" | "test", ...)`.

`preview_query` runs a semantic query against the package's warehouse, as the query server's
`execute` does, and returns at most `max_rows` rows (default 20, at most 200), with `truncated` and
`total_row_count` when there are more. Its values are real warehouse data; like runtime
validation, it may build a missing seeded DuckDB database.

## Replacing Objects

`upsert_model`, `upsert_metric` and `upsert_segment` merge their arguments into an existing
object. `replace: true` rewrites the object from the arguments instead.

In `upsert_model`, an existing dimension, time, measure or join given only `label`, `description`,
`synonyms` or `meta` keeps its other fields: `times: {snapshot_date: {label: Snapshot day}}` changes
only the label. Given any other field, the object is rewritten from what you pass, and the report's
`dropped_fields` names each field that drops, such as `times.snapshot_date.column`; restate what
should stay.

- A model keeps only its `id`, its `entities` block (the relationships `upsert_relationship`
  wrote) and its `calendar_id`. The report's `dropped_fields` names every field, dimension, time,
  measure and join the rewrite drops, such as `label` or `dimensions.status`; restate what should
  stay. Without a `description` argument the model gets the default one, and `description` is listed
  too. `upsert_model` refuses fact models.
- A metric or segment keeps its `id`, `as` and `name` unless the spec restates them, so its public
  id doesn't move.

The parse gate still runs, so a replace that drops a dimension a segment filters on is rolled back.
It doesn't check that metrics still find their measures: after dropping a measure, run
`validate_project` with `mode=runtime`.

## Tool Surface

The server's `instructions` hold the workflow and the write contract (`dry_run`,
`expected_revision`, idempotency keys, rollback) once. Tool descriptions follow the rules in
[MCP_INTERFACE.md](MCP_INTERFACE.md#writing-tool-descriptions), and every tool carries a title and
hints: `openWorldHint` marks the ones that can query the warehouse. The listed schemas leave out
pydantic's per-property titles, which only repeated the names. `scripts/mcp_context.py` tracks the
tool list's size.

- `architect_guidance`
- `setup_project_dialog`
- `create_project`
- `project_status`
- `list_project_files`
- `read_project_file`
- `write_project_files`
- `upsert_model`
- `upsert_relationship`
- `upsert_metric`
- `upsert_segment`
- `upsert_example`
- `upsert_test`
- `record_route_decision`
- `upgrade_project`
- `preview_query`
- `remove_object`
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
package directories inside the configured workspace root. A `base_ref` (for example `main` or
`HEAD~1`) names a commit in the git repository that holds the package, which need not be the
engine's. The package is read from that commit into a temporary directory, which is removed
afterwards. Symlinks, submodules, unsafe or ambiguous names, and files that cannot be read or
materialized fail the comparison instead of producing a partial baseline. Use `mcp_client_config`
to get a launch configuration that includes both `cwd` and `--workspace-root`.

Runtime validation is operational. DuckDB validation can create a missing package database from
its seed, but never replaces an existing file. If an existing file lacks configured relations,
validation returns `INVALID_CONFIG` with the missing relation names. A database another tool builds
(for example dbt) declares `seed: {kind: external}` and is never created by the runtime. Snowflake validation can issue live queries through the configured Snow CLI
connection. Use `mode=parse` for a no-query authoring check.

All mutation tools use one engine-owned transaction layer. `write_project_files` takes a
nonempty `files` list of `{path, content, overwrite: true}` writes (overwrite defaults to true)
or `{path, archive: true}` archives, plus `expected_revision`, `idempotency_key`, optional
`reason` for archives, and `dry_run`. A change across files is one call, validated as a whole
after all files are in place; either every change commits or every file is restored. Archives
share `.architect/archive/<id>/` and an optional `ARCHIVE_REASON.txt`. Empty lists, duplicate
paths, entries that both write and archive, missing archives, existing files with `overwrite: false`,
and paths outside the project or under `.architect/` are refused without writes.

The transaction contract is:

- `project_status` computes a deterministic `sha256:` revision over authored
  project files. Internal transaction receipts, archives, locks, generated
  databases, and cache files are excluded.
- `expected_revision` is required at the MCP boundary. A stale writer receives
  `CONFIG_CONFLICT` with both expected and current revisions; it never
  overwrites an intervening edit. Use one `write_project_files` call for a change across files.
  Separate calls use the `revision` the previous write returned; the refusal's `details.retry` names the revision
  to resend with.
- `idempotency_key` is required and persisted as a hashed, workspace-local
  receipt. Retrying the identical mutation replays its result. Reusing the key
  for a different intent fails closed. For `create_project`, the transaction checks that receipt
  and the expected revision before preparing a new scaffold overwrite; a completed retry does
  not re-evaluate later edits as a new mutation.
- A per-project OS file lock serializes cooperating processes. Multi-file
  replacements occur under that lock, parse as one package, and restore every
  prior byte if any write or parse step fails.
- `dry_run: true` validates a temporary virtual project and reports exact
  proposed content, unified diffs, hashes, and the proposed revision without
  writing the project or consuming the idempotency key.
- When `overwrite: true` would replace a scaffold file, its current bytes and graph must match
  a completed creation receipt. Successful creation receipts record hashes for the complete
  generated scaffold, including files unchanged by an overwrite; an intervening edit to one of
  those files cannot become the next scaffold's provenance. Byte-identical files need no
  replacement, but a no-op without proof does not establish new provenance. Receipts without
  complete scaffold hashes cannot authorize an overwrite, even when their change report matches
  the current files. For a missing graph or changed scaffold file without that proof, restore
  the recorded bytes or archive the authored project and create a new one. Changing the first
  entity also retires its old model only when that model matches the receipt. Other authored
  files and warehouse data stay in place.

Exact existing keys are updated in their current source file instead of
creating duplicate definitions elsewhere. Successful internal REPL mutations
retain transaction snapshots for one-step undo.

The generated stable interface artifact is
`semantic_rails/contracts/architect_mcp.v1.json` (mirrored under `schemas/`).
It pins tool input/output schemas, annotations, protocol versions, and
transaction capabilities. Run
`python scripts/generate_contract_artifacts.py --check` to reject drift.
