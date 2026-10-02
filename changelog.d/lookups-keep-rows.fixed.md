- A `group_by` or `where` dimension looked up through a many-to-one or one-to-one relationship
  no longer drops the measure's rows whose foreign key is NULL or matches nothing. They group
  under NULL, so grouped rows add up to the ungrouped total, and an `IS NULL` filter on the
  looked-up dimension selects them: "passengers excluding crew" through a crew-roster lookup now
  counts the passengers instead of returning 0. A filter such as `=`, `!=` or `NOT IN` still
  excludes them. Totals change only where such rows exist. The exception is a dimension that
  any rollup of the measure's model holds pre-joined: it keeps the inner join, even for a
  grain that rollup could never answer, so those rows are still left out for that dimension
  (routing to the rollup never changes an answer). A time role read through a lookup, a
  metric filter's own query and the entities its set is matched on, and conversions still
  leave those rows out. ClickHouse is unchanged too: its lookups stay inner joins, because an
  unmatched outer-join column reads `''` or `0` there, not NULL.
