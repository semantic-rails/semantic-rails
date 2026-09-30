- Empty time buckets use observation outside bounded query windows and remain NULL
  outside loaded base coverage. Coverage gates only zero filling and preserves populated
  values, including NULL time keys and future dates. Its current-time cap compares UTC
  instants independently of the session zone and honors naive columns' storage zones.
- Filled, dense-series and bounded combined queries read base relations so available
  rollups cannot change their coverage answers. Performance guidance includes the
  unbounded coverage and observation reads, which respect policy row filters.
