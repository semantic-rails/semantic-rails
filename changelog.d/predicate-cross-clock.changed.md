- A contextual `metric_predicate` whose input is measured on another clock than the query's
  `time.temporal_role` is refused with `INVALID_TEMPORAL_BINDING`, naming both clocks and the
  choices. It used to be matched to the query month by month on the input's clock without saying
  so. Query on the input's clock, use `scope_mode: entity_only` for all time, or set
  `time_alignment: same_query_period` (pinning one clock with the input's `temporal_role` if it
  has several) to compare the calendar periods on purpose. Plan drafts that would cross clocks
  are no longer offered as ready to execute.
