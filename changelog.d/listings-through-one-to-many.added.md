- A call of dimensions alone whose entities connect only through a one-to-many hop (a
  department's buildings through its rooms, a store's supplies through its orders' items)
  answers with the distinct rows along that route instead of `PATH_NOT_FOUND` or
  `MIXED_GRAIN_INVALID`. It starts at the first entity the call reads, chooses each route by
  the usual route rules (two routes ask with `AMBIGUOUS_PATH`), keeps a start row that reaches
  nothing with NULL, and names its route in `assumptions` (`LISTED_THROUGH`). Hidden objects,
  row filters and denied objects on the route hold as for any call, and a call with a measure
  or metric keeps every fan-out guard. See
  [Listings through one-to-many routes](docs/QUERY_CALL.md#listings-through-one-to-many-routes).
