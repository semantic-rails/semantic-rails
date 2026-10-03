- `plan` reads a comma-separated grouping list ("by store, customer type", "by a, b, and c") as
  it reads one joined by "and", and requires each listed grouping to have its own matching
  dimension (clock terms and declared values aside). An unrelated dimension cannot hide a
  dropped grouping; only unmatched terms appear in `why.details.dropped_groupings`. Text after
  a comma continues the list only when it names a dimension, entity or clock term, so trailing
  window and ordering clauses keep their original meaning. Two incidents that share a name
  are no longer added into one row. A grain phrase in the list ("by
  order date, at week grain", "by store and month level") sets the time grain of the measure's
  own clock instead of grouping by a calendar dimension.
- A listed grouping that names an entity is satisfied only by that entity's own key dimension,
  or by the single declared dimension of that entity whose own words name it, so "order count
  by customer history, month" is no longer ready when grouped by the customer id alone: an
  entity with a composite key is never satisfied, and the plan is not ready. A dimension's
  words for this check are its label, aliases and the last part of its name, not its id.
