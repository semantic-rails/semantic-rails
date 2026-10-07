- Apply measure constraints to caller-created aggregates reading the same source columns,
  including conditional aggregate values and conditions. Qualified and quoted relation names
  inherit the same constraints. Counts also inherit constraints on count measures sharing
  their relation name, including counts of literals or different columns. Other unrelated
  columns remain independent.
