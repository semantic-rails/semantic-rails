- A group with no rows now reads `0` or `NULL` by one rule: sometimes there is no data (`NULL`),
  and sometimes there is data of nothing (`0`). A `sum`, `count` or `count_distinct` of an
  additive, event-count or entity-count measure reads `0` where the measure has data in scope
  and `NULL` in every group where it has none; an average, minimum, maximum, stock, distinct
  population or `additive: false` measure stays `NULL`. One settling step now applies this to
  every query, so a count beside a second fact reads `0` for a group the second fact lacks, the
  amounts of a table pivoted by type add up (`goods + shipping` by refund type returns numbers,
  not `NULL`), a group whose amounts are all `NULL` reads `0` when other groups have amounts,
  and a measure beside a `distribution` no longer reads `NULL` for a period without rows. A
  `metric_predicate` follows the same rule, so `orders - returned_orders > 1` keeps a customer
  with 2 orders and no returns, as a metric filter on the same expression does. One answer
  changed the other way: a `sum` or `count` with nothing in scope reads `NULL`, not `0` (an
  ungrouped count of a filter that matches nothing). Arithmetic settles its operands first, so
  `revenue - refunds` is `NULL` if refunds were never recorded.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
- Known limitation: a `time.fill` bucket in a window with no rows reads `NULL` even when the
  measure has data outside the window, where the rule says `0`. It stays until the engine
  checks for data outside the window (tracked in a public issue).
