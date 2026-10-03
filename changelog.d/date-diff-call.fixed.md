- Accept portable `DATE_DIFF` scalar calls, including package measures and
  conditional aggregates, with validated units and NULL endpoints preserved in
  averages. Refuse Athena calls and `week` on Snowflake, BigQuery and ClickHouse
  where native semantics differ; preserve ClickHouse NULL endpoints with nullable
  timestamp casts. BigQuery TIMESTAMP endpoints count calendar boundaries in UTC,
  preserving NULLs and supporting month, quarter and year differences.
