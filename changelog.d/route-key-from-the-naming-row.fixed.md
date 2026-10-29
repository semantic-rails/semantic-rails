- An entity's key reads where the rows name it. Refunds reach their customer through their
  order, so "net revenue by customer" and "goods refunded by customer" read the refunds'
  customer from the order, as the orders do: a customer the customers table doesn't hold yet
  keeps its refunds under its own key instead of a `NULL` customer (net revenue read `2` for
  such a customer with a `1` refund on a `2` order, and the refund sat in a `NULL` row). The
  same holds filtered by the key ("net revenue of one customer") and per entity in a
  distribution. Grouped by the customer's tier, region or name, that customer stays in the
  `NULL` group.
