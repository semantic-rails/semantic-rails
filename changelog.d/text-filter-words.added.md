- Filters take `starts_with` and `ends_with`, case-insensitive and literal like `contains`, and
  `not_contains`, `not_starts_with` and `not_ends_with`, which keep rows whose value is unknown
  and count them in `UNKNOWN_ROWS_KEPT`, as `not` does. A `comparison` expression takes the same
  words with a text literal. `LIKE`, `NOT LIKE` and `ILIKE` are refused wherever a condition is
  written, `aggregate_if` and `comparison` included, with the word for the pattern: `'X%'` is
  `starts_with`, `'%X'` is `ends_with` and `'%X%'` is `contains`. A prefix or suffix pattern is
  no longer hinted as `contains`, which matched more rows.
