- Treat equality and inequality comparisons with null literals as `IS NULL` and
  `IS NOT NULL` in expressions, metric predicate inputs, segment conditions, and
  relation joins, including joins that compare in lower case. Reject ordering
  comparisons against null instead of silently returning incorrect results.
  `IS DISTINCT FROM`, `IS NOT DISTINCT FROM` and `<=>` keep their null-safe meaning,
  and `NOT` of a null literal stays NULL. A metric predicate with a null threshold
  is refused with `INVALID_METRIC_PREDICATE` instead of dropping entities that have
  no rows.
