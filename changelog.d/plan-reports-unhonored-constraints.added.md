- `plan` reports the parts of a question its draft doesn't honor. A dropped time window,
  ranking (limit, sort or ranked dimension) or named filter value downgrades the plan to
  `low_confidence` with `why.code="PLAN_INTENT_COVERAGE_GAP"`, and question words the draft
  uses nowhere come back as a `PLAN_UNMATCHED_TERMS` warning.
