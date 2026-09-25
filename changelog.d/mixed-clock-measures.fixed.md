- A query whose clock a metric's measure lacks no longer times that measure by the first
  of several clocks it has. For example, a ratio of order-line revenue (order and delivery
  clocks) over orders, queried on the order's delivery clock, divided order-date revenue
  by delivered orders and warned only `REWRITE_APPLIED`. The query now fails with
  `INCOMPATIBLE_TEMPORAL_ROLE`, naming the measure and its clocks; choose one with
  `temporal_role_overrides`. This includes a snapshot measure aligned to a calendar clock
  at month grain or coarser. A measure with a single clock is still aligned by it;
  conversion operands and `metric_predicate` inputs are unchanged. Package validation
  rejects a metric without a conversion whose declared clock would be refused this way.
- Package validation, including `project validate --mode parse`, rejects a metric that
  names a measure or metric the package doesn't define. Before, the package parsed and
  every query of the metric failed with `Unknown measure`.
