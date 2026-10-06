- Filtered additive time series keep observed buckets with zero inside the loaded range
  when the authored filter matches nothing; empty buckets outside it stay NULL.
  Unsupported shapes warn about dropped or unverified buckets.
  Aggregate-filter literals warn when missing or unverified in either observation scope.
