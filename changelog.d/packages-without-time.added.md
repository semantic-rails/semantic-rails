- Accept packages that declare no time for counts, sums, ratios, grouping, filters
  and lookups. Time requests refuse with an actionable `INVALID_TEMPORAL_ROLE`;
  project scaffolds accept a blank time column.
- Refuse unconsumed time, grain and trend requests on packages without dates,
  including "every month", "by calendar month", "hourly" and "tomorrow", while
  preserving time words in catalogue names and values carried by the query.
