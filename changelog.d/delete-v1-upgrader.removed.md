- Removed `semantic-rails project upgrade`, the Architect MCP tool `upgrade_project`, and the
  `semantic_rails.upgrade` module behind them. They rewrote packages written for a release
  before 0.3.2, and no such packages are left to upgrade. A package written in an old form
  still fails to load with the same `INVALID_CONFIG` or `INVALID_EXPRESSION_AST` refusal as
  before, which names the current form to write; the refusals no longer end with a note that
  `project upgrade` rewrites it. `semantic-rails project validate` and the Architect
  `project_status` and write reports no longer add a hint to run the upgrade. The published
  `architect_mcp.v1.json` contract no longer lists `upgrade_project`.
- `SemanticLayerMCPAdapter` no longer refuses an `interface` value other than `v2`. The
  argument is still accepted and ignored, because `v2` is the only interface; the
  `api_version` of `v2` in the published schemas is unchanged.
