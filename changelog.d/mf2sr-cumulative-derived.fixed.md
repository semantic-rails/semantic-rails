- `mf2sr` translates MetricFlow cumulative metrics into the kind that computes them. A
  `grain_to_date` becomes `kind: period_to_date` and a `window` becomes `kind: rolling`.
  Before, both were written onto `kind: cumulative`, which ignores them and returns the
  all-time running total. A cumulative metric is skipped, with the reason, when the engine
  can't compute it: both options set, a window finer than a day, a day-to-date grain, or a
  measure whose periods don't add up, such as an average or a distinct count. A filter on
  a cumulative metric is kept. Where the translated values can differ from MetricFlow's at
  coarser grains, a warning says how.
- `mf2sr` skips a derived metric whose inputs use `offset_window`, `offset_to_grain` or a
  filter, which it would have computed over the same period or unfiltered, and warns when
  it drops a filter on the derived metric itself.
