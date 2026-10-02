- Add positive `connect_timeout_seconds` and `read_timeout_seconds` options
  for native Postgres, ClickHouse, Databricks, Snowflake, BigQuery and Athena
  connections; longer request deadlines receive a five-second client margin.
