- `plan` answers with the metric a question names instead of a less specific measure.
  "What is completed revenue by month?", the question the metric wizard suggests for a
  filtered metric, used to return unfiltered revenue with `status: ok`; it now drafts the
  Completed Revenue metric. A metric named by its id works the same way, and a rolling
  metric's label ("revenue, trailing 7 days") is no longer also read as a 7-day window and a
  top 7. A draft that leaves out a metric the question names, or has no filter for a
  "where <dimension> is <value>" clause (such as "revenue where channel is web" when the
  channel values aren't declared), is now `low_confidence` (`named_metric_unrealized`,
  `dimension_filter_unrealized`).
