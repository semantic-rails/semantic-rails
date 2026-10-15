- A first or last value now skips a row whose value or order key is NULL on every warehouse,
  as DuckDB does: Postgres no longer answers NULL when the earliest row has no value, or a row
  with no time when reading the last value; BigQuery no longer reads a row with no time as the
  first; and Snowflake, Databricks and Athena no longer answer NULL when the edge row has no
  value.
