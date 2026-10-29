- A `distribution` over several measures settles each entity's operands before it aggregates,
  as a sum grouped by that entity does. "Median net order value after refunds" (revenue less
  goods refunds per order) left out every order with no refund row, so November read `-1`
  where it is `4.5`, and a month with no refund at all read `NULL`. An order with no refund now
  refunded nothing (`0`) wherever refunds have data in scope, under a `where` and a bounded
  window too; percentiles, `min` and `max` read the same values. An order whose amount is
  unknown still reads `NULL` and is left out, and a distribution over one measure is unchanged.
  A sum whose CASE has no ELSE settles beside another measure too: "median revenue excluding
  store b" per repeat customer reads `20`, where a customer with no store b order dropped out
  and it read `12`. Under the `dataset` scope, a distribution that needs settling (revenue less
  refunds) with a `where` beside an entity filter is now refused with `EMPTY_GROUPS_UNSETTLED`,
  naming it, as the call grouped by order is; it read `NULL` in every month. Measures that read
  the same rows, every one of them (the declared average order value), still answer.
