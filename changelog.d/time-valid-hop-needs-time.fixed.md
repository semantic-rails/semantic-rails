- A query that groups, filters or otherwise reads through a many-to-one relationship into a
  table holding a `temporal_validity` window, and has no `time`, is now refused with
  `FANOUT_UNSAFE`, naming the relationship and the entity, instead of joining every version of
  the far row and counting a row once per version: a customer with two segment versions no
  longer adds its amount to both segments, so grouped rows add up to the ungrouped total again.
  Add `time` so each row reads the version valid at its time; queries with a `time` answer as
  before, and a hop out of the table holding the window needs no time. This covers group-by and
  where dimensions, measure filters, dimension-only queries, conversions, metric predicates and
  live valid-values lookups. `discover` and `build-options` offer such dimensions only to a
  partial query with a `time`, and `plan` resolves a grouping such as "top customers" to the
  customer rather than to its history when both match equally.
