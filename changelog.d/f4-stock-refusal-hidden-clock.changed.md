- A `stock` query refused because its key can't tell apart the snapshots of an as-of clock
  names a clock in the `INVALID_CONFIG` error only when it's the queried one, and never lists the
  key: a `metric_constraint` policy may hide the other clocks from the caller. The error says to
  key the entity by its series and snapshot time, to keep one as-of clock in the key, or to query
  the stock on its as-of clock, and the package's parse warning names the clock.
