- A `where` filter on a query's own date dimension is now refused with windowed and
  cumulative metrics, like `time.start`, instead of truncating their lookback.
