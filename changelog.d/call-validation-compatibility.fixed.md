- Preserve all configuration errors when package checks find an incompatible
  scalar call, accept warehouse overloads whose argument families are compatible
  or uncertain, and report oversized decimal parameters as structured errors.
- Refuse BigQuery decimal CAST precision and scale constraints with
  `INVALID_EXPRESSION_AST` instead of rendering unsupported parameterized types.
