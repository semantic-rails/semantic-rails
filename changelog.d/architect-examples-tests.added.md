- The Architect MCP's `upsert_example` and `upsert_test` write example
  questions and package tests in one transaction, refusing queries that do
  not compile; `upsert_test` can capture a snapshot test's expected rows from
  the warehouse. `preview_query` returns a capped sample of a query's rows.
