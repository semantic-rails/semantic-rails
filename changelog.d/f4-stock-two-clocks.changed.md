- A `stock` measure on a snapshot table with a second clock no longer sums snapshots on the
  clock that isn't in its key. If its key lacks the model's `as_of_time` clock, every query of
  the stock is now refused with `INVALID_CONFIG` (before, only queries on that clock were), and
  a query ordered by another clock while the key holds the as-of clock is refused too, with a
  new `STOCK_SERIES_HOLDS_AS_OF_CLOCK` parse warning. Each used to return the sum of a series'
  snapshots. Query such a stock on its as-of clock. An event-time column in the key, such as a
  cohort month, still identifies a series there.
