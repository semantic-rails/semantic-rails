- On DuckDB and Postgres, empty time buckets use observation outside bounded query windows
  and remain NULL outside loaded base coverage. Coverage gates only zero filling and
  preserves populated values, including NULL time keys and future dates. Its current-time
  cap is the only instant comparison: it compares UTC instants independently of the session
  zone and honors naive columns' storage zones, while buckets, calendar joins and window
  filters keep each leaf's own time frame. Snowflake, BigQuery, Databricks, Athena and
  ClickHouse keep the in-window test until their coverage SQL has execution evidence.
- On DuckDB and Postgres, filled, dense-series (rolling and prior-period) and combined
  queries, bounded or not, read base relations so available rollups cannot change their
  coverage answers. Performance guidance includes the unbounded coverage and observation
  reads, which respect policy row filters.
