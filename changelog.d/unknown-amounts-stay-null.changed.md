- A group whose rows exist but whose amounts are all `NULL` now reads `NULL` for a `sum`, as
  SQL's `SUM` does, instead of `0`: its amounts are unknown, and a count still counts its rows.
  A sum reads `0` only in a group with no rows while its measure has data elsewhere in scope,
  and a conditional sum (`aggregate_if`, or an aggregate with a `filter`) only where no row
  meets its condition. Filled answers (`time.fill`, `prior_period`, `rolling`) and unfilled
  ones now agree on `NULL` for such a period, where the filled one read `0`. An unknown amount
  carries through: a ratio over it is `NULL`, and neither a `metric_filters` threshold nor a
  `metric_predicate` threshold keeps it, one that `0` passes (`< 5`) included, and
  `goods + shipping` by refund type is `NULL` for a type whose rows leave one of the columns
  `NULL`. A sum of a `case` measure is no longer answered from a rollup, which can't tell rows
  that fail its condition from rows that meet it with no amount.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
