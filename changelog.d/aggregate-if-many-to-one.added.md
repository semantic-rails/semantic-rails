- An `aggregate_if` whose condition reads an entity that its value's entity reaches over
  declared many-to-one relationships now compiles instead of returning
  `UNSUPPORTED_CONDITIONAL_AGGREGATE`: order revenue where the order's customer is in a
  segment, or item quantity where the item's order belongs to a customer in a region (two
  hops). It aggregates each value row once, on the route a `where` filter on that entity
  takes, and a row with no match there reads NULL, as it does for that filter. A condition
  across a one-to-many, many-to-many, bridge or time-valid hop, over two routes with no path
  preference, or in a count with no value column whose condition reads several entities
  stays refused with the same code; the error names the entities and the failing hop, with a
  hint.
