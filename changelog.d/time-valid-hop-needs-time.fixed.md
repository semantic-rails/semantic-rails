- A query that groups, filters or otherwise reads through a many-to-one relationship into a
  table holding a `temporal_validity` window, and has no `time`, is now refused with
  `FANOUT_UNSAFE`, naming the relationship and the entity, instead of joining every version of
  the far row and counting a row once per version: a customer with two segment versions no
  longer adds its amount to both segments, so grouped rows add up to the ungrouped total again.
  Add `time` so each row reads the version valid at its time; queries with a `time` answer as
  before, and a hop out of the table holding the window needs no time, nor do two measures
  selected together, which are aggregated on their own. This covers group-by and where
  dimensions, measure filters, dimension-only queries, conversions, metric predicates and live
  valid-values lookups. `discover`, `build-options` and `inspect` offer such dimensions only to
  a partial query with a `time` (`inspect` names the relationship instead of offering a
  `group_by` patch), and `plan` prefers, of two equally scored dimensions, the one that needs
  no time.
- Schema-qualified validity windows preserve outgoing lookups without a query time. Grouping
  metadata checks every selected measure, including compound expressions and named metrics,
  so changing selection order cannot offer a history grouping that compilation refuses.
