- BigQuery: a median, a percentile and first or last value leave NULL values out, as on
  DuckDB, instead of failing on an array that holds NULL; a group with no value reads NULL.
