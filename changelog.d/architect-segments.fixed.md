- The Architect's `upsert_segment` refuses membership fields written outside
  `membership:` (the engine ignored them and selected the whole population),
  unknown fields, a segment without `where` or `metric_filters`, and a segment
  the engine cannot validate; it also takes `replace`. `upsert_metric` takes a
  `file_name` so metrics can share a file.
