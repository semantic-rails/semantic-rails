- A `where` filter on a query's own date dimension is now refused with windowed and
  cumulative metrics, like `time.start`, instead of truncating their lookback. This
  includes columns connected to the clock through multiple equality joins and
  dimensions on calendars reached by those joins, in either direction.
