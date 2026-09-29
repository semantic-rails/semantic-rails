- A dimension looked up through a many-to-one or one-to-one relationship no longer drops the
  measure's rows whose foreign key is NULL or matches nothing. They group under NULL, so
  grouped rows add up to the ungrouped total, and an `IS NULL` filter on the looked-up
  dimension selects them: "passengers excluding crew" through a crew-roster lookup now
  counts the passengers instead of returning 0. A filter such as `=`, `!=` or `NOT IN` still
  excludes them. Totals change only where such rows exist. ClickHouse is unchanged: its
  lookups still drop those rows, because an unmatched outer-join column reads `''` or `0`
  there, not NULL. Conversions still leave out events with no match entity, and now events
  whose looked-up property has no match.
- A rollup with a column pre-joined from another model now routes only once certified, as if
  it declared `requires_certification`, and a runtime does not cache compiles for its package.
  A rollup built with an inner join, as the authoring guide used to say, would otherwise
  answer without the rows the base tables keep under NULL. Rebuild such a rollup with a left
  join, and certify it (`certify_aggregate_relation`) before it routes.
