- `mf2sr` writes metric filters in the form the engine applies,
  `{all: [{field, op, value}]}`, naming each field by dimension id. Before, every filtered
  metric it wrote returned unfiltered numbers. A metric's filter now also applies to both
  sides of a ratio, and the filter on a measure input is kept. List filters and multiple
  manifest `where_filters` are ANDed, comparisons with a literal and `NOT IN` are
  translated, and an `IN` list keeps commas inside its quoted values. A filter on a time
  dimension, which MetricFlow compares truncated to its grain, is reported instead of
  applied to the raw column.
- `mf2sr` skips a ratio whose filters it can't keep, which could divide a measure by
  itself, and any metric that uses a skipped metric.
