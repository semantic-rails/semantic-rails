- The `prior_period` shorthand (`{kind: prior_period, measure, offset, grain}`) without an
  `aggregation` now uses the measure's default aggregation, like
  `{kind: prior_period, input: {measure}, offset: {unit, value}}`. It used to apply `sum`.
  On a measure whose default is `avg` or `max`, that returned the prior period's total instead
  of its average or maximum. On a distinct-count measure, it refused the query. An explicit
  `aggregation` keeps its meaning.
