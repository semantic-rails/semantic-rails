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

Runtime validation is operational. DuckDB validation can create or refresh the package database, and
Snowflake validation can issue live queries through the configured Snow CLI connection. Use
`mode=parse` for a no-query authoring check.

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
