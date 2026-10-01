- Accept packages that declare no time for counts, sums, ratios, grouping, filters
  and lookups. Time requests refuse with an actionable `INVALID_TEMPORAL_ROLE`;
  project scaffolds accept a blank time column.
- Refuse unconsumed time, grain and trend requests on packages without dates,
  including "every month", "by calendar month", "hourly" and "tomorrow", while
  retaining catalogue answers containing time words as `low_confidence` plans
  with a warning that the package declares no time.
- Accept a null `group_by` in partial plan queries without an internal error.
