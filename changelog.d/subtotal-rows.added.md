- A call can ask for subtotal and grand-total rows with `totals`: the `group_by` ids the subtotal
  rows keep, outermost first (`[]` for the grand total alone). Each total row is the same call at
  its grain, so a distinct count, an average or a ratio is computed there, never added up from
  the rows. The rows come after the call's own rows; `having` and `limit` cut only the call's
  own rows; a `total_over` column names what each total row adds up; and the answer states
  `TOTALS`. See [`totals`](docs/QUERY_CALL.md#totals).
