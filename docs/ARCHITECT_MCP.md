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

The `sse` and `streamable-http` transports can write files, so they require a bearer token and
refuse to start without one. The server reads it from `--token-file`, then the file named by
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
5. Use `upsert_model`, `upsert_metric`, `upsert_segment`, or scoped file tools
   with the latest project revision. Generate a new idempotency key for each
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
package: its `default_db`). They open the file read-only with DuckDB external access disabled and
never create, seed or change it.

- `list_tables`: tables and views, optionally for one `schema`, with column counts.
- `describe_table`: columns with types, nullability and defaults, and declared primary, unique and
  foreign keys.
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
  check at most the first one million rows and eight key-like columns. Inferred foreign-key probes
  consider at most eight child columns and eight target relations per column; a bounded child
  prefix can miss an unmatched row, so such links are low-confidence and call for confirmation.
  Target relations above one million rows are checked for a declared key but their values are not
  matched. Declared warehouse keys and foreign keys retain their declared evidence. All suggestions
  return draft `upsert_model` arguments to review before calling `upsert_model`.

Profiles and samples show real values from the warehouse; use `sample_limit: 0` where that matters.
Tables and views backed by data stored in the DuckDB file work normally. A view that needs an
external file or resource can still appear in metadata-only list/describe results, but cannot be
profiled or used for a model suggestion; materialize it in the DuckDB file before introspection.
Relation components with hyphens, spaces or double quotes use their raw names, as in
`create_project`; introspection quotes them in SQL.
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
- foreign keys come from `relationships` tests and model- or column-level contract `foreign_key`
  constraints. Targets resolve against manifest identities for `ref()`, package-qualified `ref()`,
  `source()`, and relation names including alias, schema and database; target columns are preserved;
- `accepted_values` tests become dimension value sets (`domain`);
- descriptions carry into the draft; times, dimensions and measures come from column types and
  names.

`import_dbt_project` applies them: `select` names the dbt models, and one parse-gated transaction
creates or updates a model per dbt model (in `models/<group>/`, `group` defaulting to `dbt`), with
each resolved foreign key written as an entity reference in the model's `entities:` block (`expr:`
when the foreign-key column is named differently from the target's key), which the engine reads as
a many-to-one relationship. It follows the usual mutation contract (`expected_revision`,
`idempotency_key`, `dry_run`). A model whose target is imported in the same call, or already in the
package, gets the reference; references elsewhere are listed in `skipped_references`, and dbt models
without a key in dbt in `skipped_models`. A dbt model whose derived id matches a package model
(`fct_orders` and a model `orders` for entity `order`) updates that model.
An imported target's dbt manifest identity selects that staged model even when another package
model reads the same relation. A relation-only reference resolves when exactly one eligible
semantic entity reads it; ambiguous targets are listed in `skipped_references` for review.

Python callers use `semantic_rails.dbt_artifacts` (`load_dbt_artifacts`,
`suggest_models_from_dbt`, `dbt_import_models`) and `ArchitectProject.upsert_models`, which stages
several models and their references in one transaction.

## Tool Surface

- `architect_guidance`
- `setup_project_dialog`
- `create_project`
- `project_status`
- `list_project_files`
- `read_project_file`
- `write_project_file`
- `upsert_model`
- `upsert_metric`
- `upsert_segment`
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

All six mutation tools (`create_project`, raw write, the three upserts, and
archive) use one engine-owned transaction layer:

- `project_status` computes a deterministic `sha256:` revision over authored
  project files. Internal transaction receipts, archives, locks, generated
  databases, and cache files are excluded.
- `expected_revision` is required at the MCP boundary. A stale writer receives
  `CONFIG_CONFLICT` with both expected and current revisions; it never
  overwrites an intervening edit.
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
  replacement, but a no-op without proof does not establish new provenance. Older change-only
  receipts can authorize an overwrite only when one receipt proves the complete current generated
  scaffold. For a missing graph or changed scaffold file without that proof, restore the recorded
  bytes or archive the authored project and create a new one. Changing the first entity also
  retires its old model only when that model matches the receipt. Other authored files and
  warehouse data stay in place.

Exact existing keys are updated in their current source file instead of
creating duplicate definitions elsewhere. Successful internal REPL mutations
retain transaction snapshots for one-step undo.

The generated stable interface artifact is
`semantic_rails/contracts/architect_mcp.v1.json` (mirrored under `schemas/`).
It pins tool input/output schemas, annotations, protocol versions, and
transaction capabilities. Run
`python scripts/generate_contract_artifacts.py --check` to reject drift.
