- A `where` filter on a date, timestamp or calendar dimension is now refused with windowed
  and cumulative metrics, like `time.start`, instead of truncating their lookback. The rule
  is by the dimension's type, whether or not it is the query's clock, so a snapshot's own
  date with no calendar relationship is refused too, and so is any dimension on a column of
  the same name, compared without case, on any table, as such a dimension or a column a
  relationship pairs with one. An upper bound (`<`, `<=`) on a date or timestamp still runs.
