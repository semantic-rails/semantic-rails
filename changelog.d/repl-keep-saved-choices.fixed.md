- Editing a measure, dimension, time column or metric in the REPL now keeps each saved choice
  when you press Enter. Before, a choice the menu did not list was replaced: a `median` or
  `last_value` measure became a `sum`, a stock measure lost its `accumulation`, and other
  values fell back to the first option. The aggregation menu now offers every aggregation the
  measure's accumulation allows, except `percentile`, which a measure cannot parameterize.
  Choosing a different default aggregation prints a warning that names the metrics whose
  numbers change.
