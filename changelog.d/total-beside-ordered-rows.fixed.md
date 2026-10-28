- A call that selects a `total` and orders its rows by the period or a `group_by` column runs
  on every warehouse; it used to fail at the warehouse with an ambiguous column.
