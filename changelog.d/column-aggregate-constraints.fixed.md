- Apply measure constraints to caller-created aggregates reading the same source columns,
  including conditional aggregate values and conditions. Qualified and quoted relation names
  inherit the same constraints. On a relation with a constrained count measure, a
  caller-created aggregate runs only as `sum`, `min` or `max` of one column; every other
  form follows the count measure's constraints. Other unrelated columns remain independent.
