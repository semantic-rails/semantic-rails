- A `distribution` with `time.fill: true` returned wrong values: every entity entered every
  period as a `0`, so a monthly median or percentile read 0 or too low (for example
  3.0 instead of 9.0). Fill now only adds the missing periods, which read `NULL`; the other
  periods match the unfilled answer. A `distribution` over a per-entity `rolling` or
  `prior_period` value, which counted entities in periods where they had no rows, is now
  refused. See [docs/QUERY_IR_SCHEMA.md](docs/QUERY_IR_SCHEMA.md).
