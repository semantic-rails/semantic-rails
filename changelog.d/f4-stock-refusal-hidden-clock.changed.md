- A `stock` query refused because its key can't tell apart the snapshots of another of the stock's
  clocks no longer names that clock, or a key holding it, in the `INVALID_CONFIG` error: a
  `metric_constraint` policy may hide that clock from the caller. The error says to query the
  stock on its as-of clock, and the package's parse warning names it. A refusal on the queried
  clock itself still names the clock and the key.
