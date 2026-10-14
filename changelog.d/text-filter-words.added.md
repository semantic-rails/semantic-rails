- Filters take `starts_with` and `ends_with`, case-insensitive and literal like `contains`, and
  `not_contains`, `not_starts_with` and `not_ends_with`, which keep rows whose value is unknown
  and count them in `UNKNOWN_ROWS_KEPT`, as `not` does. A `comparison` expression takes the same
  words on a column one dimension declares, reading its unknown values as that dimension's
  filter does. `LIKE`, `NOT LIKE` and `ILIKE` are refused wherever a condition is written,
  `aggregate_if`, `comparison` and a package's own measure expressions included, with the word
  for the pattern: `'X%'` is `starts_with`, `'%X'` is `ends_with` and `'%X%'` is `contains`. A
  prefix or suffix pattern is no longer hinted as `contains`, which matched more rows.
- The text words, `contains` included, give the same rows on every warehouse: they lower the
  value and the text with the same function, a column's own collation set aside, so `É`
  matches `é` everywhere. Text holding a letter some warehouse lowers differently (Greek sigma,
  `ß`, `İ`, `ı`, the cased letters of U+2000 to U+2FFF and a few others) is refused with the
  exact-values hint, and a string holding NUL or a lone surrogate is refused anywhere in a
  call. The [known limitations](docs/QUERY_CALL.md#known-limitations) say what stays open. A text word on an id, number, date, timestamp or
  boolean (a numeric kind in any spelling), or on a result or per-entity value, is refused at
  its path with the words that compare it, instead of failing in the warehouse.
