- `NO_DATA_YET` now says what its edge is: "its last row is dated <date>. Nothing says this data
  is loaded past it, so later periods read NULL, not 0." `details.measures[].edge` is that last
  row's date in the clock's zone, with `edge_source: last_row` (it was the last bucket, or the
  window start, as `last_bucket` and `before_window`).
