- Remove measure `subject_entity` and `aggregation_entity` declarations and unused
  forward relationship rollup hints. Measures aggregate at their own model grain;
  reverse population-count rewrite permissions remain supported.
- Existing packages must delete `subject_entity` and `aggregation_entity` lines
  from `defaults.measure` and individual measures, and remove forward rollup
  hints from relationships. Unsupported declarations now fail package loading;
  errors for measure defaults name the line to delete.
- Model joins and relationship defaults reject `rollup_safe` in any form instead
  of silently ignoring it; use `graph.relationships` with `rollup_safe.reverse`
  for reverse population-count rewrite permissions.
- Relationship defaults reject `rollup_safe_aggregations` even when its value is
  `null` or the package has no relationships; delete the named defaults line.
