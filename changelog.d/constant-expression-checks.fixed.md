- Package checks finish for constant measure expressions, including row counts
  authored as `expr: "1"` with `default_agg: sum`, and inspect column references
  inside compound expressions without traversing literal values.
