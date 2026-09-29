- A `time` block with a `start` and/or `end` window and no `grain` now returns one total over the
  window for each `group_by` group, with no time column, including a window inside one day. It
  used to return one row per raw timestamp. The response names this in `assumptions`, and it
  survives `minimal` verbosity. Set `time.grain` for one row per period. A `time` block with no
  window, and a query that needs a time axis (a rolling or prior-period expression), still groups
  by the raw timestamp and warns `UNGRAINED_TIME_PROJECTION`. See
  [Query IR schema](docs/QUERY_IR_SCHEMA.md#timeblock).
