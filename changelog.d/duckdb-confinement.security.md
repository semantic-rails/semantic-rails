- Security hardening: the data coverage hint on an empty result no longer builds its
  warehouse query from package text. It renders table and column names as compiled
  queries do, and skips the hint when a name is not a plain SQL identifier.
- Hosts can confine DuckDB and DuckLake connections to one directory with
  `confine_to` on `Runtime`, `DuckDBAdapter`, `DuckLakeAdapter` and `create_warehouse_adapter`;
  file access outside it, extension installs and loads, and setting changes are refused
  (see [docs/EMBEDDING.md](docs/EMBEDDING.md#confining-duckdb-file-access)). It is off
  by default. Runtime retains confinement when reconnecting and requires an existing
  database; confined paths must name files inside the directory.
