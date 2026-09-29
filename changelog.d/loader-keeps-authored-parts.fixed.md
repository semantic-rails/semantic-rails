- A metric no longer loads as a broader metric than the one it says. The loader passes an
  `expression:` to the expression parser as written and carries every direct field a metric
  kind takes into the expression, so a `partition_by` on a rolling, period-to-date or
  cumulative metric is kept, and a field the kind does not take (a `window` on a cumulative
  metric) is rejected at load with the metric named. A `scoped_aggregate` recipe with an
  `anchor` and `window` used to return a lifetime value; it now keeps them and is refused
  when queried until anchored windows compile. Short `measure` and `where` field keys in a
  `scoped_aggregate` recipe resolve like other package-relative references, and the
  `prior_period` shorthand the parser accepts now loads. The `INVALID_ANCHOR_ROLE` hint
  no longer points authors at a metric recipe and suggests an offset column instead.
