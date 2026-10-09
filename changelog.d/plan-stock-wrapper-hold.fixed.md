- `plan` no longer marks a metric that wraps a balance (`COALESCE(<filtered balance>, 0)`,
  arithmetic over one, a scoped aggregate) ready to execute on a day that isn't complete. Such a
  metric isn't read on one day for you, so "Customer MRR today", "daily Customer MRR" with no
  window, or one running into today, was answered with today's partial balance; it is now held
  with `stock_as_of_unrealized`, as a plain balance is. A window that ends on or before the last
  complete day returns the same values as before.
