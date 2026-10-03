- Remove measure `subject_entity` and `aggregation_entity` declarations and unused
  forward relationship rollup hints. Measures aggregate at their own model grain;
  reverse population-count rewrite permissions remain supported.
