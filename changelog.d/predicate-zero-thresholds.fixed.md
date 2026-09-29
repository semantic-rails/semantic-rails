- A `metric_predicate` threshold that zero satisfies (`= 0`, `< 3`, `<= 0`, `!= 1`) now counts the
  entities that have no rows, as 0, when its input is a count or a sum (or an add/subtract of
  them with `null_behavior: coalesce_zero`). "Customers with no orders"
  and "members with zero activity" used to return 0 because those entities never reached the
  aggregate. An average, minimum, maximum, median or ratio over no rows is NULL, so an entity
  with no rows never satisfies a threshold on one ("average order value under 20" keeps only
  customers with orders). Conversion metrics and anchored `scoped_aggregate` ratios refuse, for now,
  a count or sum threshold that zero satisfies.
