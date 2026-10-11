- A named window under a grain answers every period on Snowflake, BigQuery, Databricks and
  Athena too: the day spine lists them, and the data's loaded edge decides `0` inside the
  data and `NULL` after it, as on DuckDB and Postgres. On ClickHouse, which has no day spine,
  and on a clock whose zone is not a known time zone, the series keeps the periods that have
  rows and says so in `assumptions` (`OBSERVED_PERIODS_ONLY`, with its `reason`) instead of
  silently leaving periods out.
