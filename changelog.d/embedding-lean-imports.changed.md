- The embedding facade no longer imports DuckDB until the first DuckDB connection: loading a
  package, compiling and enforcing policies run without it, so a host that passes its own
  warehouse adapter never loads DuckDB. A host can install without DuckDB, the MCP Python SDK
  and pydantic (see [docs/EMBEDDING.md](docs/EMBEDDING.md)); there, opening a DuckDB database
  stops with `MISSING_DEPENDENCY` naming DuckDB. The default install is unchanged.
