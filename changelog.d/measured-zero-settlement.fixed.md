- Preserve explicit `ELSE 0` contributions in distribution sums and metric predicates.
- Return zero for conditional counts absent from a loaded time window without a grain
  when the measure is observed elsewhere, preserving sums and differences of those counts.
