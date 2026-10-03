- Apply declared temporal-validity joins when grouping or filtering by a history
  key, including NULL for missing versions; require a query time for incoming
  history lookups even when the source has a matching key column.
