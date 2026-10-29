- A `distribution` over several measures settles each entity's operands before it aggregates,
  as a sum grouped by that entity does. "Median net order value after refunds" (revenue less
  goods refunds per order) left out every order with no refund row, so November read `-1`
  where it is `4.5`, and a month with no refund at all read `NULL`. An order with no refund now
  refunded nothing (`0`) wherever refunds have data in scope, under a `where` and a bounded
  window too; percentiles, `min` and `max` read the same values. An order whose amount is
  unknown still reads `NULL` and is left out, and a distribution over one measure is unchanged.
