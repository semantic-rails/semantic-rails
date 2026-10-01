- Native warehouse connection operations default to 10 seconds and supported
  network reads/query waits to 65 seconds per operation; configure larger waits
  for long queries. Driver retries and polling can extend total elapsed time.
  Postgres/Snowflake server deadlines remain opt-in; explicit zero defers to
  the server on Postgres and disables the session limit on Snowflake. Named
  Snowflake profiles retain inherited settings; put `QUERY_TAG` in the profile,
  since nonempty authored `query_tag` overrides are refused before connecting.
  Direct connections still pass authored tags via connector session parameters.
  MotherDuck and Snowflake CLI are excluded from these client defaults.
- Named Snowflake profile sessions are cached only after an authored statement
  timeout is applied successfully; setup failures discard the connection even
  if closing it also fails.
- BigQuery supplies a default server job deadline and attempts cancellation on
  result timeout; Athena cancels unfinished queries on polling timeout.
