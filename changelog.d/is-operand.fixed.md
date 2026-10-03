- Refuse `IS` / `IS NOT` filters with values other than null or booleans before
  execution, with a validation hint to use `=` / `!=` for scalar comparisons.
