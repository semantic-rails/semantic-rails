- Native warehouse connection operations default to 10 seconds and supported
  network reads/query waits to 65 seconds per operation; configure larger waits
  for long queries. Driver retries and polling can extend total elapsed time.
  Postgres/Snowflake server deadlines remain opt-in; explicit zero defers to
  the server on Postgres and disables the session limit on Snowflake. Named
  Snowflake profiles retain inherited settings; put `QUERY_TAG` in the profile,
  since nonempty authored `query_tag` overrides are refused before connecting.
  Direct connections still pass authored tags via connector session parameters.
  MotherDuck and Snowflake CLI are excluded from these client defaults.
- Read-only DuckDB bootstrap, execution and authoring introspection reject
  external-file views on new catalogs; materialize them into tables before
  upgrading. When another connection in the same process already owns the file
  with different settings, readers reuse its settings without adding the lock,
  including read-write mode when necessary. Other open errors still fail.
  An in-process reader beside a live read-only runtime must use the same locked
  configuration; `Database.connect(..., read_only=True)` supplies it.
- Named Snowflake profile sessions are cached only after an authored statement
  timeout is applied successfully; setup failures discard the connection even
  if closing it also fails.
- BigQuery supplies a default server job deadline and attempts cancellation on
  result timeout; Athena cancels unfinished queries on polling timeout.
