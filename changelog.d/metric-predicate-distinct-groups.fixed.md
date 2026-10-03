- Refuse metric predicates in distinct-group queries without a measure or conversion
  leaf instead of silently dropping the filter; add a select that reads a measure, or
  remove `metric_filters`.
