- `execute` answers in mode `run` now start with `read_from`: under each model's entity, the
  ids the answer read from it (each selected measure or metric, and each `group_by` and `where`
  dimension), and the relationship ids the SQL took to reach a dimension's model when nothing
  else in the response states that route. A model `routes` already names is left out, and so is
  an empty `read_from`. Two models can hold the same kind of value (a staging copy, a history,
  another system's table), so the answer now says which one it read. It never changes an answer,
  names only what the caller sees, and a metric grant's response leaves it out. See
  [The answer](docs/QUERY_CALL.md#the-answer).
