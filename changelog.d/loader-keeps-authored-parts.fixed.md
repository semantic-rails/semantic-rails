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
  A metric that has both an `expression:` block and a direct field (`window`,
  `partition_by`, `offset` and so on) is refused at load instead of ignoring one of them,
  and a `partition_by` entry that is not a dimension of the package is refused at load
  instead of failing every query; short dimension keys resolve like other references.
  A `partition_by` the query does not group by is refused with `INVALID_QUERY`, naming the
  metric and the missing dimension, instead of failing in the warehouse; a `partition_by`
  that is not a list, and a window value that is not a number, are refused at load with the
  metric named. The `prior_period` shorthand resolves a short `measure` key, and the REPL
  drops a window's `partition_by` when a metric switches recipe.
