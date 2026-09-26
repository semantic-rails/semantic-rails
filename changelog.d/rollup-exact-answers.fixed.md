- These queries, which a declared rollup can't answer exactly, now run on the base
  tables instead of returning a wrong number: distinct counts of anything but the
  single-column row key of a model that isn't a fact model, weekly rollups asked
  for months, quarters or years, time ranges that don't start and end on the
  rollup's bucket boundaries (day boundaries for minute and hour rollups), time
  roles that convert time zones, non-default calendars, rollups that declare their
  own `filters`, and aggregates filtered by a `metric_predicate`. The logical
  plan's `aggregate_relation_rejections` says why each rejected rollup wasn't
  used. The engine still trusts the rollup's author on two points: a weekly rollup
  must be built on Monday-start weeks, and a column pre-joined from another model
  must follow the query's join path.
