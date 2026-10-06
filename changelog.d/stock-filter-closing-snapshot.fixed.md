- A `where` filter, or a measure's own `filter`, on a stock attribute that changes within the
  period now reads each series' snapshot chosen for that period, as grouping by it does:
  `plan = basic` equals the basic row of the by-plan breakdown, instead of also counting an
  account's last basic day in a week it ended on pro. This includes attributes reached
  through a changing key. Filters on the stock's clock or a calendar dimension still
  apply before the snapshot is chosen; other date or timestamp attributes refuse with
  `REWRITE_NOT_SUPPORTED` and reason `stock_filtered_by_date_attribute`, as in `group_by`.
- A stock that adds up its series reads 0 in a period whose snapshots all fail the filters,
  and NULL only in a period with no snapshot. An entity-set share also keeps an observed
  period and reads 0 when every chosen snapshot fails its attribute filters.
- An entity-set share (a ratio whose numerator alone adds metric predicates) keeps one
  snapshot per series per time bucket, so grouped by the stock's clock, a calendar
  dimension or another date it returned only the bucket's chosen snapshot: by day within a
  week, just the week's last day. Those groupings now refuse with `REWRITE_NOT_SUPPORTED`
  and reason `entity_set_ratio_grouped_by_period`; choose the period with the time grain.
