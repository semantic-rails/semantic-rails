- Authoring errors name what to change: a measure's `expr:` may be a finite number (`expr: 1`),
  and an unreadable one (`true`, a list, `.inf`) names its model and measure; a package with no metrics shows the shortest
  `metrics:` entry to add; `INCOMPATIBLE_TEMPORAL_ROLE` names the measure (and the metric that
  reads it) and says to list the role in the measure's `times:`; `project validate` prints the
  `graph.path_preferences` rows that would decide the first undecided route instead of naming an
  MCP tool; Architect's `project_status` on a mistyped path says it does not exist; labels such
  as "Rooms" or "Rooms (east wing)" are no longer pluralized again in route readings.
- An `additive: false` measure on a row-grain model whose `default_agg` is `sum` (the default),
  `first_value` or `last_value` is refused at load, naming the aggregations it can use; asking
  it for a sum says the same instead of suggesting to sum its values.
