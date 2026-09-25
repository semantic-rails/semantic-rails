- Queries that a declared rollup can't answer exactly now run on the base tables
  instead of returning a wrong number: distinct counts of anything but the model's
  own key, weekly rollups asked for months, quarters or years, time ranges that
  don't start and end on the rollup's bucket boundaries, rollups that declare
  their own `filters`, and aggregates filtered by a `metric_predicate`. The
  logical plan's `aggregate_relation_rejections` says why a rollup wasn't used.
