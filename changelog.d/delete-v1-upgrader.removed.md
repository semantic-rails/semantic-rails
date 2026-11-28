- Removed `semantic-rails project upgrade`, the Architect MCP tool `upgrade_project`, and the
  `semantic_rails.upgrade` module behind them. They rewrote packages written for a release
  before 0.3.2, and no such packages are left to upgrade. A package written in an old form
  still fails to load with the same `INVALID_CONFIG` or `INVALID_EXPRESSION_AST` refusal as
  before, which names the current form to write; the refusals no longer end with a note that
  `project upgrade` rewrites it. `semantic-rails project validate` and the Architect
  `project_status` and write reports no longer add a hint to run the upgrade.
- **Breaking:** the Architect MCP contract moves from interface v1 to v2 because
  `upgrade_project` was removed. The manifest is now `architect_mcp.v2.json` (under
  `semantic_rails.contracts` and `schemas/`), with `interface_version: "v2"`; no other tool,
  schema or annotation changed. A client that reads `architect_mcp.v1.json` must read
  `architect_mcp.v2.json` instead: `load_contract("architect_mcp.v1.json")` now raises
  `INVALID_CONTRACT` naming v2. A client that still calls `upgrade_project` gets the MCP
  `Unknown tool: upgrade_project` error. `check_contract_compatibility.py` accepts
  this one declared move and lists it under `major_changes`; any other removed contract, and
  any other change to a kept tool, still fails.
- `check_contract_compatibility.py` now compares every MCP manifest major that both bundles
  ship, including Query MCP v1 and Architect MCP v1, so a tool removed inside one major fails.
- `SemanticLayerMCPAdapter` no longer refuses an `interface` value other than `v2`. The
  argument is still accepted and ignored, because `v2` is the only interface; the
  `api_version` of `v2` in the published schemas is unchanged.
