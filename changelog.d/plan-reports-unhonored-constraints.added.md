- `plan` reports the parts of a question its draft doesn't honor. A dropped or different time
  window, a ranking that loses its limit, named measure, sort or ranked dimension, a named
  filter value the draft drops or leaves out, or filters that require one field to equal two
  values downgrade the plan to `low_confidence` with `why.code="PLAN_INTENT_COVERAGE_GAP"`.
  Question words the draft uses nowhere come back as a `PLAN_UNMATCHED_TERMS` warning.
