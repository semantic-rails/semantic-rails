- "Net revenue by customer" and "goods refunded by customer" use the customer key on the
  refund's order when the order names one customer. For a customer who hasn't signed up, a
  refund of `1` on an order of `2` now gives that customer net revenue of `1`. Previously
  the customer had net revenue of `2` and the refund appeared under a `NULL` key. Grouping,
  a filter in the call and a filter in a metric all use the same key. ClickHouse keeps the
  inner join to customers and still drops refunds without a matching customer row.
- A distribution per entity counts a key named by a row even when the entity's table has no
  matching row. "Median order amount per region" goes from `11` to `12` in the example where
  a customer names a region missing from the regions table.
