- A measure with no time role, asked for by a date at a time grain (for example a claim
  amount by its open month, when the model's open date is a time role but not marked
  `default: true` and the measure lists no `times:`), now fails with
  `INCOMPATIBLE_TEMPORAL_ROLE` and a `declare_measure_time_role` recovery hint instead of an
  `INTERNAL_ERROR`. Declaring the time role on the model or the measure makes the same
  request answer.
