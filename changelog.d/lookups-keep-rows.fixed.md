- A `group_by` or `where` dimension looked up through a many-to-one or one-to-one relationship
  no longer drops the measure's rows whose foreign key is NULL or matches nothing. They group
  under NULL, so grouped rows add up to the ungrouped total, and an `IS NULL` filter on the
  looked-up dimension selects them: "passengers excluding crew" through a crew-roster lookup now
  counts the passengers instead of returning 0. A filter such as `=`, `!=` or `NOT IN` still
  excludes them. Totals change only where such rows exist. Every other read of a lookup is
  unchanged and still leaves those rows out: a time role read through a lookup, a metric filter
  and its context entities, conversions, qualified sets and metric predicates, anchored
  entity-set ratios, and a dimension a rollup of the measure's model holds pre-joined (so
  routing to that rollup never changes an answer). ClickHouse is unchanged too: its lookups
  stay inner joins, because an unmatched outer-join column reads `''` or `0` there, not NULL.
