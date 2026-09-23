- The REPL's `author metric` offers recipes over time besides aggregates and ratios: running
  total, period to date (month, quarter or year), and, in a package with a calendar table,
  rolling window, prior period and growth against a prior period (a percent). Each needs the
  measure's model to have a time column, and says so when it doesn't. Switching an existing
  metric to another recipe drops the old recipe's fields.
