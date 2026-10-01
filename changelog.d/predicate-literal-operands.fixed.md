- A `metric_predicate` whose `input` adds or subtracts a literal, or multiplies or divides by
  one, such as `rate * 100 < 30`, `100 * rate`, `total - 5 > 0` or `total / 1000`, now runs
  instead of failing with `PREDICATE_NOT_SUPPORTED`. It keeps the entities the unscaled
  threshold keeps, and an entity with no rows reads the arithmetic's value, so `orders - 3 < 0`
  keeps a customer with no orders as `orders < 3` does, and a division by `0` reads `NULL`.
  Any other input with a literal still fails with `PREDICATE_NOT_SUPPORTED`, such as a literal
  in a `ratio` or `comparison`, a text literal, `(orders + 1) * (orders + 1)` or
  `5 / (orders - 2)`. An input made only of literals is refused with
  `PREDICATE_INPUT_REQUIRED`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#metricfilter-expressions).
