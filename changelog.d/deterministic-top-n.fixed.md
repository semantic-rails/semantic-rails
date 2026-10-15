- Limited, ordered queries now break ties deterministically by the remaining `group_by`
  columns, then the period, ascending with NULLs last (never by a measure), and warn with
  `TIES_AT_LIMIT` (`{at, tied}`) when a fetched boundary row ties.
  Cutoff comparisons support warehouse alias case folding and reject missing or
  ambiguous sort columns.
