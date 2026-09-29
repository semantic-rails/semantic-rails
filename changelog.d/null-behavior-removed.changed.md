- The `null_behavior` key is removed from metrics and expressions (`coalesce_zero` on
  arithmetic, and the `null_if_zero` a ratio carried), from the query IR schemas, the
  capabilities payload, the Ossie export and import, the REPL metric wizard and the
  MetricFlow import. It is removed in place, from the v1 schemas, rather than under a new
  contract version: no released client uses it, and before 1.0 a removed field can ship in
  place (see `docs/CONTRACTS.md`). A ratio always divided by `NULLIF(denominator, 0)`; an empty
  group is now settled by the engine (see the empty-groups entry), so an operand with data
  reads `0` without a `coalesce_zero`, in a `metric_predicate` too. A package that still
  authors `null_behavior` fails validation with `unknown key 'null_behavior'`: delete the
  line. Ossie SQL written as `COALESCE(x, 0) + COALESCE(y, 0)` is no longer read back as a
  metric.
