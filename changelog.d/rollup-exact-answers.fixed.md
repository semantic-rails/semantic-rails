- Queries that a declared rollup can't answer exactly now run on the base tables
  instead of returning a wrong number: distinct counts of anything but the model's
  single-column row key, weekly rollups asked for months, quarters or years, time
  ranges that don't start and end on the rollup's bucket boundaries, time roles
  that convert time zones, non-default calendars, rollups that declare their own
  `filters`, and aggregates filtered by a `metric_predicate`. The logical plan's
  `aggregate_relation_rejections` says why each rejected rollup wasn't used.
