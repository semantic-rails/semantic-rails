- A query that groups, filters or otherwise reads through a relationship with
  `temporal_validity` and has no `time` is now refused with `FANOUT_UNSAFE`, naming the
  relationship and the entity, instead of joining every version of the far row and counting a
  row once per version: a customer with two segment versions no longer adds its amount to both
  segments, so grouped rows add up to the ungrouped total again. Add `time` so each row reads the
  version valid at its time; queries with a `time` answer as before. This covers group-by and
  where dimensions, measure filters, dimension-only queries, conversions, metric predicates and
  live valid-values lookups. Discovery and build-options still list such dimensions as reachable.
