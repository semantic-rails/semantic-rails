- A `comparison` takes the filter words as `where` reads them: `gt`, `gte`, `lt`, `lte` and
  `is` as `>`, `>=`, `<`, `<=` and `=`, `not` keeping rows with no value, and `missing` with a
  literal `true` or `false`. They were refused as an unsafe SQL token.
- `aggregate_if`'s `condition` is optional: without one it aggregates every row, so a sum or
  average of a column or a row expression (a `case`, `LENGTH`) needs no `1 = 1` condition.
- A raw `column` in `select` is refused with the `aggregate_if` call that aggregates it,
  instead of saying a query reads only measures and metrics.
- A `PATH_NOT_FOUND` for measures on several entities names each measure's own entity in its
  `anchor`; it named the first measure's entity for all of them.
- A measure's `verbosity: "full"` discover card lists its `allowed_aggregations`, as `inspect`
  does.
