- Treat equality and inequality comparisons with null literals as `IS NULL` and
  `IS NOT NULL` in expressions, metric predicates, and segment conditions. Reject
  ordering comparisons against null instead of silently returning incorrect results.
