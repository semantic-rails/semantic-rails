- These queries, which a declared rollup can't answer exactly, now run on the base
  tables instead of returning a wrong number: distinct counts of anything but the
  single-column row key of a model that isn't a fact model, weekly rollups asked
  for months, quarters or years, time ranges that don't start and end on the
  rollup's bucket boundaries (day boundaries for minute and hour rollups), time
  roles that convert time zones, non-default calendars, rollups that declare their
  own `filters`, aggregates filtered by a `metric_predicate`, stock
  (semi-additive) measures, aggregations other than the one a rollup column holds,
  and dimensions pre-joined into a rollup along a join path other than the
  query's (or with no declared `path`). The logical plan's
  `aggregate_relation_rejections` says why each rejected rollup wasn't used. The
  engine still trusts the rollup's author that a weekly rollup is built on
  Monday-start weeks.
