- Report oversized decimal parameters as structured `INVALID_EXPRESSION_AST`
  errors in query validation and package checks.
- Refuse BigQuery decimal CAST precision and scale constraints with
  `INVALID_EXPRESSION_AST` instead of rendering unsupported parameterized types.
- Leave scalar-call argument types and overload resolution to the warehouse,
  so supported overloads compile in query and package expressions. Warehouse
  execution failures retain their stable, redacted error code.
