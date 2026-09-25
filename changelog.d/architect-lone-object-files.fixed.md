- `upsert_metric` and `upsert_segment` no longer lose the object in a file that held a single
  `metric:` or `segment:` when they add another to it: the file becomes a plural block that keeps
  both. `upsert_segment` refuses a segment with no `where`, `metric_filters` or `time` membership,
  which would select the whole population.
