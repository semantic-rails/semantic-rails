- "Net revenue by customer" and "goods refunded by customer" use the customer key on the
  refund's order when the order names one customer and the relationship is declared from
  the order to the customer. For a customer who hasn't signed up, a refund of `1` on an
  order of `2` now gives that customer net revenue of `1`. Previously the customer had net
  revenue of `2` and the refund appeared under a `NULL` key. A relationship declared from
  the customer's side still reads the joined customer key. Grouping, a filter in the call
  and a filter in a metric all use the same key. ClickHouse keeps the inner join to customers
  and still drops refunds without a matching customer row.
- The same key read applies to any entity reached through many-to-one hops, such as an
  order's region through its customer. It includes the named key in totals and distributions
  even when the entity's table has no matching row. In the regional example, order amount
  where the region is missing goes from `96` to `77`, and filtering for `R9` goes from `0`
  to `19`. Grouping by region gives `R9` its own total of `19`. Per-region median goes
  from `11` to `12`, average from `17.8` to `18`, and 25th percentile from `7` to `8`.
  For wholesale orders, both the median and average go from `10` to `13`, and the 25th
  percentile goes from `8.5` to `10`.
