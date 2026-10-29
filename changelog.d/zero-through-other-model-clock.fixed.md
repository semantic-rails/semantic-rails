- A measure dated by another model's clock (refunds dated by their order's time) is now capped
  by its table's declared edge (`zero_through`) on that clock when asked with no time window.
  "How many goods refunds have we ever issued?" read `0` under an empty load log, which
  certifies nothing; it now reads `NULL` with `NO_DATA_IN_SCOPE`, as every refund count does
  there. A refund on an order after the load log, or whose order is missing, is no longer read.
