- A call can select a coarser total beside its rows: `{"kind": "total", "input": <id>,
  "over": [<group_by id> | "period"]}` is the input computed by the same call without the
  `over` columns (the same `where`, window, entity filters, routes, row filters and policies),
  on each row of the groups that remain. A distinct count, an average or a ratio is computed at
  that grain, never added up from the rows, and across a link each entity counts once. The
  answer states each total as `TOTAL_OVER` in `assumptions`. See
  [Coarser totals](docs/QUERY_CALL.md#coarser-totals).
