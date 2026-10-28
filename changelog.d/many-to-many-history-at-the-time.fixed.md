- A history declared `many_to_many` with `temporal_validity` is no longer joined as one version
  per row when a query reads it at its time. Its versions valid at one time can overlap (an
  account with two owners at once), and a plain join counted the row once per version (an
  account's usage read twice under one segment). Such a hop now keeps its cardinality's rule
  and is refused (`MIXED_GRAIN_INVALID`) where it would have double counted. A history declared
  `many_to_one` or `one_to_one` still reads the version valid at each row's time.
