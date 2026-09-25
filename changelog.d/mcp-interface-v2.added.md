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
