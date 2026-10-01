- Keep single-argument AND/OR of NULL unknown in comparisons and filters,
  consistently across configured measures, post-aggregation expressions, and relations.
- Render NOT(NULL) and single-argument AND/OR of NULL with a nullable boolean cast
  on ClickHouse, so projections and comparisons work with default cast settings.
