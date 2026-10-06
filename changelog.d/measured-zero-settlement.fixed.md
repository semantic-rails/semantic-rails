- Preserve explicit `ELSE 0` contributions in distribution sums and metric predicates.
- Return zero for conditional sums and counts absent from a loaded time window, including
  totals without a grain, when their source has rows even if the condition never matched.
  Preserve arithmetic over those zeros, NULL amounts, empty sources and coverage limits.
