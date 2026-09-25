- **Breaking:** query MCP interface v1 is removed in 0.3.3. Interface v2 is the only one: six
  tools (`discover`, `inspect`, `valid-values`, `plan`, `execute` and `segment`) and one
  contract, `query_mcp.v2.json`; `query_mcp.v1.json` is no longer shipped. Setting
  `SEMANTIC_RAILS_MCP_INTERFACE=v1` or passing `interface="v1"` fails with "The v1 MCP interface
  was removed in 0.3.3; use v2", and calling a v1 tool returns `UNKNOWN_MCP_TOOL` naming its
  replacement. `MCP_INTERFACE_VERSION` and the other interface constants are gone from
  `semantic_rails.mcp` and `semantic_rails.embedding`. Upgrading from v1:
  - `validate` → `execute` with `mode: "validate"`; `compile` → `execute` with `mode: "sql"`.
  - `segment-validate`, `segment-explain`, `segment-preview` → `segment` with `action`
    `validate`, `explain` or `preview` (`verbosity: "full"` for the whole response).
  - `catalog` → `discover` with empty `terms`, or the `semantic-rails://catalog/*` resources.
  - `capabilities`, `build-options` → draft Query IR with `plan`. The HTTP API and the CLI
    keep both.
  - Smaller defaults: `execute` returns at most 200 rows (`max_rows` up to 100,000);
    `discover` and `inspect` return slim cards (`verbosity: "compact"` for v1's); `plan`
    returns `detail: "query"` (`detail: "best"` for v1's).
  See [docs/MCP_INTERFACE.md](docs/MCP_INTERFACE.md#migrating-from-interface-v1).
