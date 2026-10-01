- The `null_behavior` key is removed from metrics and expressions (`coalesce_zero` on
  arithmetic, and the `null_if_zero` a ratio carried), from the query IR schemas, the
  capabilities payload, the Ossie export and import, the REPL metric wizard and the
  MetricFlow import. A package that still authors it fails to load with one message:
  delete the line. A ratio always divided by `NULLIF(denominator, 0)`; an empty group is now
  settled by the engine (see [the empty-groups rule](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0)),
  so an operand with data reads `0` without a `coalesce_zero`, in a `metric_predicate` too. Ossie
  SQL written as `COALESCE(x, 0) + COALESCE(y, 0)` is no longer read back as a metric.
