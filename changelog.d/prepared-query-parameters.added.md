- Compiled statements can carry typed parameter slots that the engine binds per request from
  the host's `TrustedAttributes`. DuckDB receives the values through its own parameter
  binding, never in the SQL text; every other warehouse adapter refuses such statements, and a
  missing or mistyped attribute is denied. Requests with different attribute values no longer
  share a compile-cache entry. `row_filter` policies produce the parameters. See
  [docs/ADDING_A_DIALECT.md](docs/ADDING_A_DIALECT.md).
