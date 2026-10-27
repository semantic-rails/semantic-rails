- A conversion rate counts every base event in its denominator. A session with no customer
  (anonymous traffic), or with a missing lookup for a constant property such as its store, now
  counts as a session that didn't convert. Before, such sessions dropped out of the
  denominator and the rate came back too high: one anonymous session among 11 read 1.0
  instead of 0.909. A metric filter on a conversion rate counts the same sessions as the rate
  shown.
- Conversion events match on their own key column (for example `sessions.customer_id` =
  `orders.customer_id`), so a customer the customers table doesn't hold yet still converts.
  A NULL key never matches another NULL key, and an event whose constant-property lookup
  finds no row, on either side, pairs with nothing.
- On ClickHouse, a conversion whose base events need a lookup (a grouping or constant
  property read through another table) is refused with `CONVERSION_NOT_SUPPORTED` instead of
  dropping those events.
