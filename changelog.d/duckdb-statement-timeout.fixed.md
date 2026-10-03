- A DuckDB `limits.statement_timeout_ms` now stops the running query. Before, the timeout
  interrupted the shared connection rather than the query's own cursor, so it never fired and
  a slow query ran to completion. Each query's timeout stops only that query.
