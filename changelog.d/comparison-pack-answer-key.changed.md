- The semantic-layer comparison pack checks every layer, Semantic Rails included, against an
  independent answer key. The key is SQL written against the shared views without seeing any
  layer's models or outputs, and a second agent reviewed it. Each layer's result columns are
  mapped to the answer's fields explicitly instead of being guessed from their names. On all 16
  questions, the five layers checked on the current data (Semantic Rails, MetricFlow, Cube,
  Malloy and KtX) match the key within 1e-6. The stale Snowflake capture matches 14 and differs
  on q07 and q16.
