- A `metric_predicate` whose `input` combines a metric with a literal, such as `rate * 100 < 30`,
  `100 * rate`, `total - 5 > 0` or `total / 1000`, now runs instead of failing with
  `PREDICATE_NOT_SUPPORTED`. It keeps the entities the unscaled threshold keeps, and an entity
  with no rows reads the arithmetic's value, so `orders - 3 < 0` keeps a customer with no orders
  as `orders < 3` does, and a division by `0` reads `NULL`. An input made only of literals is
  refused with `PREDICATE_INPUT_REQUIRED`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#metricfilter-expressions).
