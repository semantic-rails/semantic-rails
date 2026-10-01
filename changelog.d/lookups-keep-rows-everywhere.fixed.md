- A many-to-one or one-to-one lookup now keeps the measure's rows whose foreign key is NULL or
  matches nothing in every query shape, not only for a `group_by` or `where` on the plain path.
  A sum grouped by a region two hops away now adds up to the ungrouped total instead of
  dropping those rows; a query with any metric filter, a measure's own `filter`, an
  `aggregate_if` condition, a measure expression that reads another model, a distinct count
  grouped beside a one-to-many child, an entity-set ratio and a dimension-only query keep them
  too, under NULL. The same `IS NULL` condition now returns one answer as a `where`, a
  measure's own `filter` or a segment. Totals change only where such rows exist. A time role
  read through a lookup, a metric filter's own query and the entities its set is matched on,
  a distribution's per-entity values, conversions, a dimension a rollup of the measure's model
  holds, and ClickHouse still leave them out. A rollup of another model, such as one of the
  items for an order count, never does, whichever way the count is read, and no rollup does
  in a dimension-only query.
