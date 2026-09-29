- A measure with no time role, asked for by a date at a time grain (for example a claim
  amount by its open month, when the model's open date is a time role but not marked
  `default: true` and the measure lists no `times:`), now fails with
  `INCOMPATIBLE_TEMPORAL_ROLE` and a `declare_measure_time_role` recovery hint instead of an
  `INTERNAL_ERROR`. Declaring the time role on the model or the measure makes the same
  request answer. Naming a role by `temporal_role_overrides` or an aggregate's `temporal_role`
  on such a measure gets the same hint. An `aggregate_if` can't be used with `time` and says
  so, with no hint. When any requested measure has no time role, the mixed-grain recovery for
  a calendar-date group-by suggests no time block, so no `use_time_grain` hint points at a
  refused query.
