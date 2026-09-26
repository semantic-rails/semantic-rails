- A conversion `window` is now a duration after the base event on every warehouse: a converted
  event counts when `base <= converted < base + window`, as in MetricFlow. It used to count
  unit boundaries, so a 7-day window ran to the end of the 7th calendar day (up to 8 days), a
  1-week window ran through the 13th day on DuckDB and Postgres, and a 1-month window covered
  all of the next month. Conversion rates can drop where converted events fell in that extra
  time; the shipped `jaffle_shop` conversion metrics are unchanged. See
  [docs/QUERY_API.md](docs/QUERY_API.md).
