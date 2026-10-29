- An `aggregate_if` with no time window reads its table's rows only through the table's
  declared edge (`zero_through`), as the table's measures do. "The value of orders over 50 we
  have ever had, by region" counted an order after the load log: east read `1179` where
  revenue beside it read `180`, and the count read `3` for `2`. Under an empty load log it now
  reads no row, where it read every row. On ClickHouse, which reads no loaded coverage, such a
  call is refused (`INVALID_CONFIG`), as the table's measures are.
