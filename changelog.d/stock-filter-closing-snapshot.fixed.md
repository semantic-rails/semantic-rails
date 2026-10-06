- A `where` filter, or a measure's own `filter`, on a stock attribute that changes within the
  period now reads each series' snapshot chosen for that period, as grouping by it does:
  `plan = basic` equals the basic row of the by-plan breakdown, instead of also counting an
  account's last basic day in a week it ended on pro. Filters on the stock's clock, a calendar
  dimension, or a date or timestamp still apply before the snapshot is chosen.
- A stock that adds up its series reads 0 in a period whose snapshots all fail the filters,
  and NULL only in a period with no snapshot.
