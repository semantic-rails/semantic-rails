- An `aggregate_if` read inside an entity filter that is itself inside another entity filter
  (such as a ratio a scoped aggregate's predicate reads) is now held to the metric constraints
  on the measures over its columns as the same aggregate in `select` is, and refused where
  that is refused. Its value decides which rows the entity filter keeps, so each column it
  aggregates also counts as a filter there, but a key it counts. While
  anything is hidden from a caller, such an aggregate over columns is refused as it is in
  `select`. A package metric whose own definition reads such an aggregate still answers
  wherever a call uses it: it is held by its policies and what it reads.
  The same aggregate written in the call is held as the caller's wherever the call places it.
