- A `metric_predicate` whose `input` adds or subtracts a literal or multiplies by one, such
  as `rate * 100 < 30`, `100 * rate` or `total - 5 > 0`, now runs instead of failing with
  `PREDICATE_NOT_SUPPORTED`. It keeps the entities the unscaled threshold keeps, and an entity
  with no rows reads exactly what the arithmetic gives in the SQL, so `orders - 3 < 0` keeps a
  customer with no orders as `orders < 3` does. A literal with a fraction runs only on DuckDB,
  MotherDuck, DuckLake and Postgres, over counts. Any other input with a literal, including a
  division by a numeric literal, still fails with `PREDICATE_NOT_SUPPORTED` and the same
  message. An input made only of literals is refused with `PREDICATE_INPUT_REQUIRED`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#metricfilter-expressions).
