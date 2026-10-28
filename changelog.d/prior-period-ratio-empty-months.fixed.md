- A `ratio` over a `prior_period` or `rolling` window, inline or in a metric's `expression:`,
  now reads every period, as `arithmetic` division already did. The month after a month with no
  rows read the ratio over the last month that had any ("revenue vs the previous month" by store
  gave 1.6 for a store's March over its November); it now reads `NULL` there, and the answer
  returns the empty months. On ClickHouse such a ratio is refused, like the other prior-period
  answers there.
