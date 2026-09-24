- Two aggregates of one measure that differ only by `filter` or only by
  `temporal_role` no longer share one result column. The column was keyed on
  the measure, aggregation and parameters, so the first aggregate selected
  answered for both. On `jaffle_shop`, the share of new-customer orders by year
  (order count under a filter, divided by order count) returned `[1.0, 1.0]`
  instead of about `[0.034, 0.013]`. Two conversions that differed only by an
  operand filter shared a column the same way. Each now gets its own column.
