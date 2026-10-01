- Single-argument and/or are refused before SQL, including negated forms and NULL
  arguments, consistently across configured measures, post-aggregation expressions,
  and relations. Zero-argument forms are also refused; use at least two arguments.
- Render NOT(NULL) with a nullable boolean cast
  on ClickHouse, so projections and comparisons work with default cast settings.
