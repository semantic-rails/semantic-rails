- `plan` no longer marks ready a draft that counts every row of a measure while a visible
  metric filters those rows on a dimension of the measure's entity, even when the question
  doesn't name that metric. "How many teams were created last week?" answered with a count of
  every team, including the test teams that `New teams` leaves out, is now `low_confidence`
  with a `governed_metric_unrealized` gap naming the metric in `expected.metrics` and the
  dimensions it filters on in `expected.narrowed_by`. This holds whether the metric filters
  the same measure or counts other rows, such as creation events, by the team's class, and it
  now holds "stores last week" over a published all-kinds store count too. A draft that
  selects the metric, or filters or groups by one of those dimensions, and a measure the
  caller selects in `partial_query`, are unchanged.
