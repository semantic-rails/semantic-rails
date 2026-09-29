- A `metric_predicate` threshold that zero satisfies (`= 0`, `< 3`, `<= 0`, `!= 1`) now counts the
  entities that have no rows, as 0, when its input is a count or a sum. "Customers with no orders"
  and "members with zero activity" used to return 0 because those entities never reached the
  aggregate. For an average, minimum, maximum, median or ratio there is no value over no rows,
  so such a threshold is refused with `INVALID_METRIC_PREDICATE` and the ways to ask it; it is
  also refused, for now, inside conversion metrics and anchored `scoped_aggregate` ratios.
