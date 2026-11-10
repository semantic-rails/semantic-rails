- A `scoped_aggregate` predicate compares the statistic it states. A predicate written
  `{"input": "measure.<id>", "aggregation": "avg", ...}` (or with the `measure` shorthand)
  compared each entity's default statistic, usually its total, and so answered a different
  question: revenue from customers whose average order is at least $30 returned about ten times
  too much on jaffle_shop. Its `aggregation`, `parameters` and `temporal_role` now apply to the
  measure, in calls and in package metrics. A statistic beside a metric input, or one that
  contradicts the input's own, is refused `INVALID_METRIC_PREDICATE`, and a key a predicate does
  not read is refused `INVALID_QUERY`.
