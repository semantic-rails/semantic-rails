- A group whose rows exist but whose amounts are all `NULL` now reads `NULL` for a `sum`, as
  SQL's `SUM` does, instead of `0`: its amounts are unknown, and a count still counts its rows.
  A sum reads `0` only in a group with no rows while its measure has data elsewhere in scope,
  and a conditional sum (`aggregate_if`, or an aggregate with a `filter`) only where no row
  meets its condition. The period's own value, filled (`time.fill`) or unfilled, and a
  `prior_period` read of it are `NULL`; rolling and cumulative windows skip its unknown
  value. An unknown amount carries through: a ratio over it is `NULL`, and neither a
  `metric_filters` threshold nor a `metric_predicate` threshold keeps it, one that `0`
  passes (`< 5`) included, and
  `goods + shipping` by refund type is `NULL` for a type whose rows leave one of the columns
  `NULL`. A `metric_predicate` threshold that `0` passes on an add or subtract of measures
  still reads an operand's unknown amounts as `0`, so `goods + shipping = 0` keeps the orders
  with no refunds. A sum of a `case` measure with no `else` (or `else: null`), with one
  branch or several, reads `0` in a group where no row meets a branch and is no longer
  answered from a rollup, which can't tell rows that fail its conditions from rows that meet
  one with no amount. All of this covers queries without a `distribution`. A query with a
  `distribution` output keeps the earlier settlement, and its plan and SQL, in every output:
  there a sum whose amounts are all `NULL` still reads `0` where its measure has data in
  scope, and arithmetic beside the distribution settles each operand that way.
  A measure containing a `case` below its expression root, such as a conditional amount
  divided by 100, keeps the earlier settlement individually and never reads a rollup:
  no-match groups read `0` where an amount is known elsewhere in scope; matched-unknown
  groups also read `0` there and stay `NULL` only when no amount is known in scope.
  Multi-hop aggregation preserves these empty-group zeros when a physical join column
  is named `__source_rows`.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
