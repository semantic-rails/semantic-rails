- A `time` block with a `start` and/or `end` window and no `grain` now returns one total over the
  window for each `group_by` group, with no time column, including a window inside one day. It
  used to return one row per raw timestamp. The response names this in `assumptions` and sets
  `time_shape: "window_total"`; both survive `minimal` verbosity and a metric grant. Set
  `time.grain` for one row per period. A `time` block with no window, a query that needs a time
  axis (a rolling or prior-period expression), and a query with a metric predicate still group by
  the raw timestamp and warn `UNGRAINED_TIME_PROJECTION`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#timeblock).
