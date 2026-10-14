- Filters take `starts_with` and `ends_with`, case-insensitive and literal like `contains`, and
  `not_contains`, `not_starts_with` and `not_ends_with`, which keep rows whose value is unknown
  and count them in `UNKNOWN_ROWS_KEPT`, as `not` does. A `comparison` expression takes the same
  words on a column one dimension declares, reading its unknown values as that dimension's
  filter does. `LIKE`, `NOT LIKE` and `ILIKE` are refused wherever a condition is written,
  `aggregate_if`, `comparison` and a package's own measure expressions included, with the word
  for the pattern: `'X%'` is `starts_with`, `'%X'` is `ends_with` and `'%X%'` is `contains`. A
  prefix or suffix pattern is no longer hinted as `contains`, which matched more rows.
- The text words, `contains` included, fold the value and the text with the same warehouse
  function (`lowerUTF8` on ClickHouse), so non-ASCII capitals such as `İ` and `É` match on
  both sides. A text word on an id, number, date, timestamp or boolean, or on a result or
  per-entity value, is refused at its path with the words that compare it, instead of failing
  in the warehouse.
