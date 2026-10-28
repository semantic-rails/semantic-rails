- The MCP Python SDK and pydantic moved from the default install to a new `architect` extra,
  because only the Architect MCP (`semantic-rails-architect-mcp`) uses them. Install it with
  `uv tool install 'semantic-rails[architect]'` (or `uv pip install 'semantic-rails[architect]'`).
  The CLI, the query MCP server and the embedding facade need no extra. Without the extra,
  `semantic-rails-architect-mcp` and `mcp setup --mcp architect` or `--mcp both` stop and name
  it, and `mcp setup` and `mcp client-config` default to the query MCP; with it they include
  both, as before.
- The embedding facade no longer imports DuckDB until the first DuckDB connection: loading a
  package, compiling and enforcing policies run without it, so a host that passes its own
  warehouse adapter never loads DuckDB. Without DuckDB installed, opening a DuckDB database
  stops with `MISSING_DEPENDENCY` naming the `duckdb` package.
