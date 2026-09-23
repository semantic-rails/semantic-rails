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
   `create_project` arguments.
4. Preview `create_project` with `expected_revision: absent`, `dry_run: true`,
   and a caller-generated `idempotency_key`; then repeat with `dry_run: false`
   after reviewing its exact file changes.

`create_project` writes one scaffold, the same one the CLI and REPL use (`architect_service`
`create_project` with a `ProjectSpec`): a strict package (`schema_strict: true`) with one model,
its count and amount metrics, an example, a package test and a `.gitignore` for build outputs.

- DuckDB with `data: starter` (the default) adds a two-row CSV seed, so the package runs at once.
  Starter names are made safe (`Raw Events` becomes `raw_events`).
- DuckDB with `data: external` reads a database another tool builds, such as dbt
  (`seed.kind: external`, `default_db` defaulting to `data/<package_id>.duckdb`). Names must match
  the warehouse: `relation` may be schema-qualified (`main_marts.fct_orders`) and is never renamed,
  and the model gets only the columns you name (no starter dimension or amount). The database may
  already sit in the project directory: a directory with no authored files still has revision
  `absent`. Keep the database inside the package (for example, point the dbt profile's `path` at
  `<package>/data/<package_id>.duckdb`); a path outside it needs
  `SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS=1`.
- Other warehouses take `connection_kind`, `connection_name` and `connection_options`, with
  secrets named by environment variable only; a literal secret fails the parse gate and nothing is
  written.
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

## Safety Model

All file writes are scoped to the configured workspace root. The default workspace root is the
repository root; pass `--workspace-root` when an MCP client should operate in a separate project
workspace.

Comparison paths for `diff_project`, `impact_project`, and release checks must also resolve to
package directories inside the configured workspace root. Use `mcp_client_config` to get a launch
configuration that includes both `cwd` and `--workspace-root`.

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
