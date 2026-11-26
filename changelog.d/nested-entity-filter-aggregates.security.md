- An `aggregate_if` read inside an entity filter that is itself inside another entity filter
  (such as a ratio a scoped aggregate's predicate reads) is now held to the metric constraints
  on the measures over its columns exactly as the same aggregate in `select` is: refused where
  that is refused, and answered where the governed measure's own column exempts it. While
  anything is hidden from a caller, such an aggregate over columns is refused as it is in
  `select`. A package metric whose own definition reads such an aggregate still answers when
  selected or used in `where[].having`: it is held by its policies and what it reads.
