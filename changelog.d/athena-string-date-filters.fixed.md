- Athena: a filter on a string dimension whose value looks like a date (`dt = '2026-01-01'`)
  stays a string comparison. Only time windows, filters on date or timestamp dimensions,
  relation filter steps on a date or timestamp column, and inline comparisons or `in` lists
  against a date or timestamp value (a column, or a `date_add`, `DATE_TRUNC`, `COALESCE`,
  `NULLIF` or `case` over one) compare as `TIMESTAMP` literals, now including `is` with
  several values.
