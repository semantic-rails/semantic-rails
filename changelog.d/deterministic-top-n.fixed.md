- Limited, ordered queries now break ties deterministically using the remaining
  output columns and warn with `TIES_AT_LIMIT` when a fetched boundary row ties.
  Cutoff comparisons support warehouse alias case folding and reject missing or
  ambiguous sort columns.
