- An `aggregate_if`, and each operand of a `ratio` or arithmetic, now answers under a
  positive filter on a child table's dimension instead of returning
  `MIXED_GRAIN_INVALID`: for example, the share of orders over an amount among orders
  that have a goods refund. Each leaf keeps the rows of its own entity that have a
  matching child, so several matching children never count a row twice. The rules for
  measures apply to every leaf: one child route or a pinned one, at most one condition
  across a one-to-many hop, no negated child filters, only `count_distinct` grouped by
  a child dimension, and `POLICY_DENIED` under a row policy. An `aggregate_if` over a
  model whose measures declare rows finer than the entity's key is refused, as those
  measures are; on ClickHouse, over a model with no measures, only `count_distinct`,
  `min` and `max` are answered.
