- A `rolling`, `cumulative` or `period_to_date` window whose rows all have a NULL amount now
  reads `NULL`, as one sum over those rows does ("All NULL reads NULL"). It read `0`, the sum of
  its empty periods: "each store's trailing three-month revenue" (or average order value) read
  `0` for a store whose only order in those months has no amount. A window with no rows still
  reads `0` inside the data, and a window with any amount reads the sum of the amounts it has.
