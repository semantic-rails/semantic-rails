- A dimension looked up through a many-to-one or one-to-one relationship no longer drops the
  measure's rows whose foreign key is NULL or matches nothing. They group under NULL, so
  grouped rows add up to the ungrouped total, and an `IS NULL` filter on the looked-up
  dimension selects them: "passengers excluding crew" through a crew-roster lookup now
  counts the passengers instead of returning 0. A filter such as `=`, `!=` or `NOT IN` still
  excludes them. Totals change only where such rows exist; a rollup with a pre-joined column
  should now be built with a left join. Conversions still leave out events with no match
  entity. On ClickHouse, `IS NULL` right after an outer join now finds the unmatched rows
  (the session turns off `optimize_functions_to_subcolumns`), and a looked-up column reads
  NULL there only when it is `Nullable`.
