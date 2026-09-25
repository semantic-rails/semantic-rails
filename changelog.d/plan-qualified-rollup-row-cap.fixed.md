- `plan` no longer caps a qualified rollup at 5 rows when the question doesn't ask for a top
  N. "Monthly revenue from customers with at least 2 orders" returned only its first 5
  months with `status: ok`; it now returns every month. A "top 3" question still keeps its
  limit of 3.
