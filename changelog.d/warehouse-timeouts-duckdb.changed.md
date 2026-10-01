- Native warehouse connection operations default to 10 seconds and supported
  network reads/query waits to 65 seconds per operation; configure larger waits
  for long queries. Driver retries and polling can extend total elapsed time.
  Postgres/Snowflake server deadlines remain opt-in; explicit zero defers to
  the server on Postgres and disables the session limit on Snowflake. Named
  Snowflake profiles retain inherited settings. MotherDuck and Snowflake
  CLI are excluded from these client defaults.
- Read-only DuckDB bootstrap, execution and authoring introspection reject
  external-file views; materialize them into tables before upgrading.
- BigQuery supplies a default server job deadline and attempts cancellation on
  result timeout; Athena cancels unfinished queries on polling timeout.
