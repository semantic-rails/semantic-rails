- Totals keep every disclosure the call would make without them. A total over the period states
  `PARTIAL_DATA` for its window, a total of a metric built from several measures states
  `NO_DATA_YET` for its unloaded periods, and with total rows `NO_DATA_IN_SCOPE`,
  `EMPTY_RESULT_WINDOW`, `data_diagnostics` and `NULL_PRESERVING_HISTORY` read the call's own
  rows only. A limited call's boundary probe no longer takes a slot of the transport's `max_rows`,
  so it never displaces a total row.
