- A call in the previous shape is refused with the caller's own values in the new shape where
  the rewrite is mechanical: `metric_filters` as `having` rows and entity filters, `time.range`
  as the `time` to send, `route_decisions` as `routes`, `limits.max_rows` as the argument,
  `{metric: ...}` and `measure_ref` objects (with their aggregation) as ids or `kind: measure`,
  the `prior_period` shorthand with its offset, predicate `scope_mode`/`time_alignment` as
  `over`, a clock id in `order_by` as `period`, and `LIKE '%text%'`, `IS DISTINCT FROM` and
  `BETWEEN` as `contains`, `not`, and `gte` with `lte` (`BETWEEN` includes both ends; the hint
  said `lt`).
