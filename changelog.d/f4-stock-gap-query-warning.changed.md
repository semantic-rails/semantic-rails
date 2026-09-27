- A query answered by a `stock` measure whose key doesn't contain its event- or state-time clock,
  including one read only by a metric predicate or segment filter, now carries a
  `STOCK_SNAPSHOT_KEY_MISSING_CLOCK` warning in its `warnings`, not only at parse time. Such a
  stock counts each row as its own series, which is right for a table with one row per series and
  sums the snapshots of a table that keeps several; the answer now says so. Key a snapshot table
  by its series columns plus the snapshot time, and declare that clock `class: as_of_time` so a
  mis-keyed table is refused instead.
