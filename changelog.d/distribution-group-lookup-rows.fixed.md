- A distribution grouped by a lookup reads every group the call does. "Median net order value
  and orders by store name" read `NULL` for the order with no store, beside its count of 1,
  where its net value is `6`, and alone it left that group out. By customer channel, the
  customers with no signup were left out of the `NULL` group, so "median customer revenue by
  channel" read `NULL` there instead of `2`. Per store, an order with no store is no store of
  its own: "median revenue per store" reads `34` (Elm `43`, Oak `25`), where it read `25`,
  counting that order as a third store.
