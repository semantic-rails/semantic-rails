- Remove measure `subject_entity` and `aggregation_entity` declarations and unused
  forward relationship rollup hints. Measures aggregate at their own model grain;
  reverse population-count rewrite permissions remain supported.
- Existing packages must delete `subject_entity` and `aggregation_entity` lines
  from `defaults.measure` and individual measures, and remove forward rollup
  hints from relationships. Unsupported declarations now fail package loading;
  errors for measure defaults name the line to delete.
