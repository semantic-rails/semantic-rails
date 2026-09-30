- Accept packages that declare no time for counts, sums, ratios, grouping, filters
  and lookups. Time requests refuse with an actionable `INVALID_TEMPORAL_ROLE`;
  project scaffolds accept a blank time column.
- Keep the same time-refusal error and recovery hint for expression kinds,
  aggregation names and referenced IDs with surrounding whitespace.
