- A sum grouped two hops away reads its empty weeks as the same sum grouped one hop away.
  "Weekly revenue by customer region" (orders, then customers, then regions) read `0` before
  the first order and past the orders' load log, where revenue by customer tier reads `NULL`,
  and refunds and the refund rate by region read `NULL` in every week, all time too, before the
  first refund, where by tier they read `0` under the orders' log. Only the measure's own
  table's edge covers its weeks: a load log declared only on the customers table covers
  signups, so the orders read with no declared edge. An order whose customer the customers
  table doesn't hold yet still reads under no region.
