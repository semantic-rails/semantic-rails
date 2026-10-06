- Filtered additive time series on DuckDB and Postgres keep observed buckets with zero inside
  the loaded range, with or without fill, when the authored filter matches nothing; buckets
  after the last loaded timestamp stay NULL. Unsupported shapes and other warehouses warn
  about dropped or unverified buckets.
  Aggregate-filter literals warn when missing or unverified in either observation scope.
